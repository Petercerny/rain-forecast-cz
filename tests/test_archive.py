"""The R2 archive: export, read-back verification, and the prune guard.

The database tests run against TEST_DATABASE_URL (see conftest.py); R2 is replaced by an in-memory store
or a fake S3 client, so no network and no credentials are needed.
"""
import io
from datetime import date, datetime, timedelta, timezone

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

import src.archive as archive  # noqa: E402
from src.archive import (  # noqa: E402
    ArchiveError, FORECAST_SCHEMA, Forecasts, LocalStore, MemoryStore, NoStore, Observations, R2Store,
    archive_day, convert_snapshot_rows, prune, prune_day, run, to_parquet_bytes,
)
from src.db import HOURLY_ARRAYS, pack_fetch  # noqa: E402
from src.db_migrate import create_archiver_role, role_url  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)   # exports days <= 07-28, prunes days <= 07-15


def at(day: int, hour: int = 10, minute: int = 23, month: int = 7) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


def add_fetch(conn, source, fetched_at, slug="praha", hours=3):
    """One fetch with `hours` forecast hours starting at the whole hour of fetched_at."""
    first = fetched_at.replace(minute=0, second=0, microsecond=0)
    rows = [{"hour_start": first + timedelta(hours=i), "precip_mm": 1.0 + i, "temp_c": 10.5 + i,
             "precip_prob": 20 if source == "open-meteo" else None,
             "wind_ms": 2.5 if source == "open-meteo" else None,
             "cloud_pct": 40 if source == "open-meteo" else None} for i in range(hours)]
    p = pack_fetch(rows)
    conn.execute("INSERT INTO rain.locations (slug, name, lat, lon) VALUES (%s, %s, 50.0, 14.0) ON CONFLICT DO NOTHING",
                 (slug, slug.title()))
    cols = list(p)
    conn.execute(f"INSERT INTO rain.fetches (source, location_slug, fetched_at, {', '.join(cols)}) "
                 f"VALUES (%s, %s, %s, {', '.join(['%s'] * len(cols))})", (source, slug, fetched_at, *p.values()))
    conn.commit()


def add_observation(conn, observed_at, wsi="0-20000-0-11518", temp=12.5):
    conn.execute("INSERT INTO rain.stations (wsi, name, lat, lon) VALUES (%s, 'Praha', 50.0, 14.0) ON CONFLICT DO NOTHING", (wsi,))
    conn.execute("INSERT INTO rain.observations (station_wsi, observed_at, temp_c, wind_ms) VALUES (%s, %s, %s, 3.0)",
                 (wsi, observed_at, temp))
    conn.commit()


def count(conn, table, where="true"):
    return conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]


def logged(conn):
    return {(k, d): n for k, d, n in conn.execute("SELECT kind, day, rows FROM rain.archive_log")}


def read(store, key):
    return pq.read_table(io.BytesIO(store.objects[key]))


# ---------------------------------------------------------------- no database needed

def test_forecast_file_has_one_column_per_stored_hourly_array():
    assert FORECAST_SCHEMA.names[:5] == ["location_slug", "fetched_at", "source_updated_at", "hour_start", "lead_hours"]
    assert FORECAST_SCHEMA.names[5:] == list(HOURLY_ARRAYS)   # a new array column must be added to the archive too


def test_object_keys_are_hive_partitioned():
    assert Forecasts("met-no").key(date(2026, 10, 25)) == "forecasts/source=met-no/date=2026-10-25/part.parquet"
    assert Observations().key(date(2026, 10, 25)) == "observations/date=2026-10-25/part.parquet"


class FakeS3:
    """Just enough of a boto3 S3 client."""
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            err = Exception("NoSuchKey")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


def test_r2_store_round_trip_and_missing_key():
    store = R2Store(FakeS3(), "bucket")
    assert store.get("a/b") is None
    store.put("a/b", b"data")
    assert store.get("a/b") == b"data"
    assert store.uri("a/b") == "s3://bucket/a/b"


def test_other_errors_from_r2_are_not_swallowed():
    class Broken(FakeS3):
        def get_object(self, Bucket, Key):
            err = Exception("denied")
            err.response = {"Error": {"Code": "AccessDenied"}}
            raise err
    with pytest.raises(Exception, match="denied"):
        R2Store(Broken(), "b").get("k")


def test_local_store_writes_files(tmp_path):
    store = LocalStore(tmp_path)
    store.put("forecasts/x/part.parquet", b"1")
    assert (tmp_path / "forecasts" / "x" / "part.parquet").read_bytes() == b"1"
    assert store.get("nope") is None and not store.durable


def test_role_url_swaps_user_and_password():
    url = role_url("postgresql://postgres.abcd:secret@host.pooler.supabase.com:5432/postgres?sslmode=require", "archiver", "p@ss/w")
    assert url == "postgresql://archiver.abcd:p%40ss%2Fw@host.pooler.supabase.com:5432/postgres?sslmode=require"


def test_snapshot_rows_become_long_rows_with_lead_hours():
    fetched = at(10)
    row = {"fetched_at": fetched, "location": "České Budějovice", "lat": 49.0, "lon": 14.0,
           "valid_time": at(10, 11, 0), "hour_start": at(10, 11, 0), "precip_mm": 0.1, "precip_prob": None,
           "temp_c": 9.9, "source_updated_at": None}
    gap = {**row, "hour_start": at(10, 12, 0), "precip_mm": None}
    table = convert_snapshot_rows([row, row, gap])   # a duplicate and a gap
    assert table.schema == FORECAST_SCHEMA and table.num_rows == 1
    r = table.to_pylist()[0]
    assert r["location_slug"] == "ceske-budejovice" and r["lead_hours"] == pytest.approx(37 / 60)
    assert r["precip_mm"] == pytest.approx(0.1) and r["wind_ms"] is None   # column missing in old files


def test_backfill_converts_the_data_branch_and_skips_today(monkeypatch):
    def snapshot(fetched):
        rows = [{"fetched_at": fetched, "location": "Praha", "lat": 50.0, "lon": 14.0, "valid_time": fetched,
                 "hour_start": fetched.replace(minute=0), "precip_mm": 0.0, "precip_prob": 5, "temp_c": 8.0}]
        buf = io.BytesIO()
        pq.write_table(pa.Table.from_pylist(rows), buf)
        return buf.getvalue()

    files = {
        "snapshots/source=met-no/date=2026-07-29/part-20260729T102300Z.parquet": snapshot(at(29)),
        "snapshots/source=met-no/date=2026-07-29/part-20260729T112300Z.parquet": snapshot(at(29, 11)),
        "snapshots/source=met-no/date=2026-07-31/part-20260731T102300Z.parquet": snapshot(at(31)),   # today
        "DATA_LICENSES.md": b"x",
    }

    def fake_git(*args):
        if args[0] == "ls-tree":
            return "\n".join(files).encode()
        return files[args[1].split(":", 1)[1]]

    monkeypatch.setattr(archive, "_git", fake_git)
    store = MemoryStore()
    dry = archive.backfill_from_data_branch(store, today=date(2026, 7, 31), dry_run=True)
    assert [r["uploaded"] for r in dry] == [False] and not store.objects
    res = archive.backfill_from_data_branch(store, today=date(2026, 7, 31))
    assert [r["key"] for r in res] == ["forecasts/source=met-no/date=2026-07-29/part.parquet"]
    assert read(store, res[0]["key"]).num_rows == 2
    assert archive.backfill_from_data_branch(store, today=date(2026, 7, 31)) == []   # already there: skipped


# ---------------------------------------------------------------- export (needs Postgres)

def test_a_day_is_exported_as_long_rows_and_logged(conn):
    add_fetch(conn, "open-meteo", at(10))
    store = MemoryStore()
    res = archive_day(conn, store, Forecasts("open-meteo"), date(2026, 7, 10))
    assert res["rows"] == 3 and res["uploaded"]
    t = read(store, "forecasts/source=open-meteo/date=2026-07-10/part.parquet")
    assert t.schema == FORECAST_SCHEMA
    rows = t.to_pylist()
    assert [r["lead_hours"] for r in rows] == pytest.approx([-23 / 60, 37 / 60, 97 / 60])
    assert rows[0]["location_slug"] == "praha" and rows[0]["precip_mm"] == 1.0 and rows[0]["wind_ms"] == 2.5
    assert rows[0]["precip_prob"] == 20 and rows[0]["fetched_at"] == at(10)
    assert logged(conn) == {("forecasts:open-meteo", date(2026, 7, 10)): 3}
    assert conn.execute("SELECT uri FROM rain.archive_log").fetchone()[0].endswith("date=2026-07-10/part.parquet")
    assert count(conn, "rain.fetches") == 1   # exporting never deletes


def test_only_complete_days_older_than_two_days_are_exported(conn):
    for day in (27, 28, 29, 30):   # NOW = 07-31 12:00: 27 and 28 are old enough, 29 ended <2 days ago
        add_fetch(conn, "open-meteo", at(day))
    store = MemoryStore()
    out = run(conn, store, now=NOW)
    assert sorted(a["day"] for a in out["archived"]) == [date(2026, 7, 27), date(2026, 7, 28)]
    assert sorted(store.objects) == [f"forecasts/source=open-meteo/date=2026-07-{d}/part.parquet" for d in (27, 28)]
    assert run(conn, store, now=NOW)["archived"] == []   # second run: nothing new


def test_each_source_gets_its_own_file(conn):
    add_fetch(conn, "open-meteo", at(10))
    add_fetch(conn, "met-no", at(10))
    store = MemoryStore()
    run(conn, store, now=NOW)
    assert sorted(store.objects) == ["forecasts/source=met-no/date=2026-07-10/part.parquet",
                                     "forecasts/source=open-meteo/date=2026-07-10/part.parquet"]
    assert read(store, "forecasts/source=met-no/date=2026-07-10/part.parquet").num_rows == 3


def test_the_day_is_the_utc_day_of_fetched_at(conn):
    add_fetch(conn, "met-no", at(10, 23, 50))
    add_fetch(conn, "met-no", at(11, 0, 5))
    run(conn, MemoryStore(), now=NOW)
    assert logged(conn) == {("forecasts:met-no", date(2026, 7, 10)): 3, ("forecasts:met-no", date(2026, 7, 11)): 3}


def test_a_bad_upload_is_not_logged_and_does_not_stop_other_days(conn):
    add_fetch(conn, "met-no", at(10))
    add_fetch(conn, "met-no", at(11))

    class Truncating(MemoryStore):
        def put(self, key, data):
            super().put(key, data[:-5] if "07-10" in key else data)

    out = run(conn, Truncating(), now=NOW)
    assert [f["day"] for f in out["failures"]] == [date(2026, 7, 10)]
    assert logged(conn) == {("forecasts:met-no", date(2026, 7, 11)): 3}   # only the verified day


def test_a_smaller_export_never_replaces_a_bigger_archive_object(conn):
    add_fetch(conn, "met-no", at(10), hours=2)
    store = MemoryStore()
    key = Forecasts("met-no").key(date(2026, 7, 10))
    bigger = [{"location_slug": "praha", "fetched_at": at(10), "hour_start": at(10, 10, 0), "lead_hours": 0.0}] * 5
    store.put(key, to_parquet_bytes(pa.Table.from_pylist(bigger, schema=FORECAST_SCHEMA)))
    with pytest.raises(ArchiveError, match="not replacing"):
        archive_day(conn, store, Forecasts("met-no"), date(2026, 7, 10))
    assert logged(conn) == {}


def test_a_day_whose_hours_are_all_gaps_is_marked_done(conn):
    add_fetch(conn, "met-no", at(10), hours=1)
    conn.execute("UPDATE rain.fetches SET precip_mm = ARRAY[NULL]::real[], precip_prob = ARRAY[NULL]::smallint[], "
                 "temp_c = ARRAY[NULL]::real[], wind_ms = NULL, cloud_pct = NULL")
    conn.commit()
    store = MemoryStore()
    run(conn, store, now=NOW)
    assert not store.objects and logged(conn) == {("forecasts:met-no", date(2026, 7, 10)): 0}
    assert conn.execute("SELECT uri FROM rain.archive_log").fetchone()[0] is None


def test_dry_run_changes_nothing(conn):
    add_fetch(conn, "met-no", at(1))
    store = MemoryStore()
    out = run(conn, store, now=NOW, dry_run=True)
    assert len(out["archived"]) == 1 and out["archived"][0]["bytes"] > 0
    assert not store.objects and logged(conn) == {} and count(conn, "rain.fetches") == 1
    out = run(conn, NoStore(), now=NOW, dry_run=True)   # without any R2 credentials
    assert out["failures"] == [] and count(conn, "rain.fetches") == 1


def test_a_local_folder_is_a_preview_only(conn, tmp_path):
    add_fetch(conn, "met-no", at(1))
    run(conn, LocalStore(tmp_path), now=NOW)
    assert (tmp_path / "forecasts/source=met-no/date=2026-07-01/part.parquet").exists()
    assert logged(conn) == {} and count(conn, "rain.fetches") == 1   # not logged, so never pruned


# ---------------------------------------------------------------- prune guard (needs Postgres)

def test_prune_deletes_only_verified_days_older_than_15_days(conn):
    add_fetch(conn, "met-no", at(1))    # verified by the run below, 30 days old -> deleted
    add_fetch(conn, "met-no", at(20))   # verified, but only 11 days old -> kept
    add_fetch(conn, "open-meteo", at(2))
    store = MemoryStore()
    # Archive everything except open-meteo's day 2 (pretend that export failed).
    for ds, day in ((Forecasts("met-no"), 1), (Forecasts("met-no"), 20)):
        archive_day(conn, store, ds, date(2026, 7, day))
    out = prune(conn, store, archive.all_datasets(), now=NOW)
    assert out == [{"kind": "forecasts:met-no", "day": date(2026, 7, 1), "deleted": 1}]
    left = sorted((s, f.day) for s, f in conn.execute("SELECT source, fetched_at FROM rain.fetches"))
    assert left == [("met-no", 20), ("open-meteo", 2)]


def test_prune_keeps_a_day_when_the_archive_object_is_gone(conn):
    add_fetch(conn, "met-no", at(1))
    store = MemoryStore()
    archive_day(conn, store, Forecasts("met-no"), date(2026, 7, 1))
    store.objects.clear()
    assert prune(conn, store, archive.all_datasets(), now=NOW) == []
    assert count(conn, "rain.fetches") == 1


def test_prune_keeps_a_day_whose_row_count_changed(conn):
    add_fetch(conn, "met-no", at(1))
    store = MemoryStore()
    archive_day(conn, store, Forecasts("met-no"), date(2026, 7, 1))
    add_fetch(conn, "met-no", at(1, 14))   # a late fetch that the archive does not contain
    assert prune(conn, store, archive.all_datasets(), now=NOW) == []
    assert count(conn, "rain.fetches") == 2


def test_prune_refuses_a_day_inside_the_keep_window(conn):
    add_fetch(conn, "met-no", at(20))
    store = MemoryStore()
    archive_day(conn, store, Forecasts("met-no"), date(2026, 7, 20))
    with pytest.raises(ValueError, match="refusing"):
        prune_day(conn, store, Forecasts("met-no"), date(2026, 7, 20), now=NOW)
    assert count(conn, "rain.fetches") == 1


def test_prune_never_touches_a_day_that_is_not_logged(conn):
    add_fetch(conn, "met-no", at(1))
    assert prune_day(conn, MemoryStore(), Forecasts("met-no"), date(2026, 7, 1), now=NOW) is None
    assert count(conn, "rain.fetches") == 1


def test_dry_run_prune_reports_but_keeps_the_rows(conn):
    add_fetch(conn, "met-no", at(1))
    store = MemoryStore()
    archive_day(conn, store, Forecasts("met-no"), date(2026, 7, 1))
    out = prune(conn, store, archive.all_datasets(), now=NOW, dry_run=True)
    assert out and count(conn, "rain.fetches") == 1


# ---------------------------------------------------------------- observations

def test_observations_are_exported_and_pruned_like_forecasts(conn):
    for when in (at(1, 8, 0), at(1, 8, 10), at(20, 8, 0)):
        add_observation(conn, when)
    store = MemoryStore()
    # day 20 is old enough to export but only 11 days old, so it stays in Postgres
    out = run(conn, store, now=NOW)
    assert not out["failures"]
    t = read(store, "observations/date=2026-07-01/part.parquet")
    assert t.num_rows == 2 and {"station_wsi", "observed_at", "temp_c", "wind_ms"} <= set(t.schema.names)
    assert t.schema.field("observed_at").type == pa.timestamp("us", tz="UTC")
    assert [d["kind"] for d in out["pruned"]] == ["observations"]
    assert count(conn, "rain.observations") == 1   # only day 20 is left


def test_observations_are_skipped_when_the_table_does_not_exist(conn):
    conn.execute("DROP TABLE rain.observations CASCADE")   # as if migration 004 had not been applied
    conn.commit()
    add_fetch(conn, "met-no", at(10))
    out = run(conn, MemoryStore(), now=NOW)
    assert out["failures"] == [] and len(out["archived"]) == 1


# ---------------------------------------------------------------- the archiver role

def test_archiver_role_can_read_delete_and_log_but_not_insert(conn):
    add_fetch(conn, "met-no", at(1))
    add_observation(conn, at(1, 8, 0))
    store = MemoryStore()
    conn.execute("SET ROLE archiver")
    conn.commit()   # SET ROLE is transactional: without this the first rollback would undo it
    try:
        out = run(conn, store, now=NOW)       # export, log and prune, all as the archiver
        assert not out["failures"] and len(out["pruned"]) == 2
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("INSERT INTO rain.locations (slug, name, lat, lon) VALUES ('x', 'x', 1, 1)")
        conn.rollback()
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("UPDATE rain.fetches SET source = 'met-no'")
        conn.rollback()
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()
    assert count(conn, "rain.fetches") == 0 and count(conn, "rain.observations") == 0


def test_pipeline_role_still_cannot_delete(conn):
    add_fetch(conn, "met-no", at(1))
    conn.execute("SET ROLE pipeline")
    conn.commit()
    try:
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("DELETE FROM rain.fetches")
        conn.rollback()
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("INSERT INTO rain.archive_log (kind, day, rows) VALUES ('x', '2026-01-01', 0)")
        conn.rollback()
    finally:
        conn.rollback()
        conn.execute("RESET ROLE")
        conn.commit()
    assert count(conn, "rain.fetches") == 1


def test_create_archiver_role_returns_a_connection_string(conn):
    url = create_archiver_role(conn, "postgresql://postgres.abcd:pw@host:5432/postgres")
    assert url.startswith("postgresql://archiver.abcd:") and url.endswith("@host:5432/postgres")
    assert conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'archiver'").fetchone()[0] is True
    conn.execute("ALTER ROLE archiver NOLOGIN")   # roles live outside the schema: put it back
    conn.commit()


def test_storage_settings_accept_the_generic_and_the_old_r2_names(monkeypatch):
    for name in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_BUCKET", "S3_REGION",
                 "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="S3_ENDPOINT_URL"):
        archive.s3_settings_from_env()

    monkeypatch.setenv("S3_ENDPOINT_URL", "https://abc.storage.supabase.co/storage/v1/s3")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("S3_BUCKET", "rain-archive")
    monkeypatch.setenv("S3_REGION", "eu-central-1")
    assert archive.s3_settings_from_env() == {"endpoint": "https://abc.storage.supabase.co/storage/v1/s3", "key": "k",
                                              "secret": "s", "bucket": "rain-archive", "region": "eu-central-1"}

    for name in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_BUCKET", "S3_REGION"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "k2")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "s2")
    monkeypatch.setenv("R2_BUCKET", "b2")
    assert archive.s3_settings_from_env() == {"endpoint": "https://acct.r2.cloudflarestorage.com", "key": "k2",
                                              "secret": "s2", "bucket": "b2", "region": "auto"}
