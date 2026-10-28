"""Tests against a real Postgres (TEST_DATABASE_URL), see tests/conftest.py."""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from helpers import H0, NOW, add_fetch, hourly_rows, write_parquet
from src.db_migrate import MIGRATIONS_DIR, migrate
from src import ingest
from src.db import load_http_state, record_run, save_http_state, store_fetch
from src.legacy_parquet import fetches_from_file, list_files


def count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_migrations_are_applied_once(conn):
    assert migrate(conn) == []   # the fixture already applied them all
    on_disk = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    applied = [v for (v,) in conn.execute("SELECT version FROM rain.schema_migrations ORDER BY version")]
    assert applied == on_disk and len(on_disk) >= 2          # every file recorded exactly once


def test_forecast_view_unpacks_arrays_and_skips_gaps(conn):
    add_fetch(conn, "open-meteo", NOW, "Praha", {0: 1.0, 3: 2.0}, probs={0: 10, 3: 30})  # hours 1 and 2 missing
    rows = conn.execute(
        "SELECT hour_start, precip_mm, precip_prob FROM rain.forecast_hours ORDER BY hour_start").fetchall()
    assert rows == [(H0, 1.0, 10), (H0 + timedelta(hours=3), 2.0, 30)]


def test_the_same_fetch_cannot_be_stored_twice(conn):
    add_fetch(conn, "open-meteo", NOW, "Praha", {0: 1.0})
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_fetch(conn, "open-meteo", NOW, "Praha", {0: 1.0})


def test_arrays_of_different_length_are_rejected(conn):
    add_fetch(conn, "open-meteo", NOW, "Praha", {0: 1.0})   # creates the location
    conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO rain.fetches (source, location_slug, fetched_at, first_hour, precip_mm, precip_prob, temp_c) "
            "VALUES ('met-no', 'praha', %s, %s, '{1,2}', '{NULL}', '{5,6}')", (NOW, H0))


LOC = {"name": "Praha", "lat": 50.088, "lon": 14.4208}


def test_store_fetch_is_all_or_nothing_and_idempotent(conn):
    rows = hourly_rows({0: 1.0, 1: 2.0}, probs={0: 30, 1: 60})
    store_fetch(conn, "open-meteo", LOC, "praha", NOW, None, rows)
    store_fetch(conn, "open-meteo", LOC, "praha", NOW, None, rows)           # the same fetch again: ignored
    assert count(conn, "rain.fetches") == 1
    assert conn.execute("SELECT slug, name FROM rain.locations").fetchall() == [("praha", "Praha")]
    got = conn.execute("SELECT hour_start, precip_mm, precip_prob FROM rain.forecast_hours ORDER BY hour_start").fetchall()
    assert got == [(H0, 1.0, 30), (H0 + timedelta(hours=1), 2.0, 60)]


def test_http_state_round_trips_and_is_separate_per_source(conn):
    save_http_state(conn, "met-no", "50.0880,14.4208", {"expires": "2026-07-01T11:00:00+00:00", "last_modified": "Wed, 01 Jul 2026 10:00:00 GMT"})
    save_http_state(conn, "met-no", "50.0880,14.4208", {"expires": "2026-07-01T12:00:00+00:00"})   # replaced, not duplicated
    save_http_state(conn, "open-meteo", "praha", {"expires": "2026-07-01T10:55:00+00:00"})
    assert load_http_state(conn, "met-no") == {"50.0880,14.4208": {"expires": "2026-07-01T12:00:00+00:00"}}
    assert list(load_http_state(conn, "open-meteo")) == ["praha"]


def test_record_run_writes_one_line_per_source(conn):
    record_run(conn, NOW, "open-meteo", 936, 0, "vercel")
    record_run(conn, NOW, "met-no", 700, 2)
    rows = conn.execute("SELECT source, rows_loaded, failures, note FROM rain.ingest_runs ORDER BY source").fetchall()
    assert rows == [("met-no", 700, 2, None), ("open-meteo", 936, 0, "vercel")]


def test_ingest_run_stores_skips_what_is_not_due_and_logs(conn, monkeypatch):
    monkeypatch.setattr("src.ingest.load_locations", lambda: [LOC])
    monkeypatch.setattr("src.ingest.fetch_open_meteo", lambda loc: {"hourly": {
        "time": ["2026-07-01T11:00"], "precipitation": [0.4], "precipitation_probability": [20], "temperature_2m": [15.0]}})
    monkeypatch.setattr("src.ingest.time.sleep", lambda s: None)
    monkeypatch.setenv("USER_AGENT", "test/0.1 https://example.test")

    class Reply:
        status_code, headers = 304, {}
        def raise_for_status(self): pass
    monkeypatch.setattr("src.ingest.requests.get", lambda *a, **k: Reply())

    first = ingest.run(conn, sources=("open-meteo",), now=NOW)
    assert first["open-meteo"] == {"stored": 1, "skipped": 0, "failures": 0, "rows": 1}
    second = ingest.run(conn, sources=("open-meteo",), now=NOW + timedelta(minutes=10))    # inside the 55 minutes
    assert second["open-meteo"] == {"stored": 0, "skipped": 1, "failures": 0, "rows": 0}
    third = ingest.run(conn, sources=("open-meteo",), now=NOW + timedelta(minutes=56))
    assert third["open-meteo"]["stored"] == 1
    assert count(conn, "rain.fetches") == 2
    assert count(conn, "rain.ingest_runs") == 2                                            # the all-skipped run is not logged

    met = ingest.run(conn, sources=("met-no",), now=NOW)                                   # 304 -> nothing new, no failure
    assert met["met-no"] == {"stored": 0, "skipped": 1, "failures": 0, "rows": 0}


def test_ingest_run_counts_a_failing_place_and_keeps_going(conn, monkeypatch):
    other = {"name": "Brno", "lat": 49.1952, "lon": 16.608}
    monkeypatch.setattr("src.ingest.load_locations", lambda: [LOC, other])

    def fetch(loc):
        if loc["name"] == "Praha":
            raise RuntimeError("API down")
        return {"hourly": {"time": ["2026-07-01T11:00"], "precipitation": [0.0],
                           "precipitation_probability": [0], "temperature_2m": [15.0]}}
    monkeypatch.setattr("src.ingest.fetch_open_meteo", fetch)
    result = ingest.run(conn, sources=("open-meteo",), now=NOW)
    assert result["open-meteo"] == {"stored": 1, "skipped": 0, "failures": 1, "rows": 1}
    assert [r[0] for r in conn.execute("SELECT location_slug FROM rain.fetches")] == ["brno"]
    # The failed place is not marked as fetched, so the next call tries it again.
    assert list(load_http_state(conn, "open-meteo")) == ["brno"]


def test_old_parquet_snapshots_can_still_be_read_and_stored(tmp_path, conn):
    write_parquet(tmp_path, "open-meteo", NOW, "Brno", {0: 1.0, 1: 2.0, 2: 0.0}, temps={0: 5.0, 1: 6.0, 2: 7.0})
    for f in list_files(tmp_path):
        fetches, locations = fetches_from_file(f["path"], f["source"])
        conn.execute("INSERT INTO rain.locations (slug, name, lat, lon) VALUES (%s, %s, %s, %s)", locations[0])
        conn.execute(
            "INSERT INTO rain.fetches (source, location_slug, fetched_at, first_hour, precip_mm, precip_prob, temp_c) "
            "VALUES (%(source)s, %(slug)s, %(fetched_at)s, %(first_hour)s, %(precip_mm)s, %(precip_prob)s, %(temp_c)s)",
            fetches[0])
    rows = conn.execute("SELECT hour_start, precip_mm, temp_c FROM rain.forecast_hours ORDER BY hour_start").fetchall()
    assert rows == [(H0, 1.0, 5.0), (H0 + timedelta(hours=1), 2.0, 6.0), (H0 + timedelta(hours=2), 0.0, 7.0)]


def test_pipeline_role_can_read_but_not_change_the_structure_or_delete_history(conn):
    add_fetch(conn, "open-meteo", NOW, "Praha", {0: 1.0})
    conn.commit()
    try:
        conn.execute("SET ROLE pipeline")
    except psycopg.Error:
        conn.rollback()
        pytest.skip("this connection cannot switch to the pipeline role")
    assert count(conn, "rain.forecast_hours") == 1                      # may read (through the view)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):           # may not create tables
        conn.execute("CREATE TABLE rain.evil (x int)")
    conn.rollback()
    conn.execute("SET ROLE pipeline")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):           # may not delete history
        conn.execute("DELETE FROM rain.fetches")
    conn.rollback()


# ---- migration 002: sky, light and wind arrays

def test_old_style_and_new_style_fetches_both_show_up_in_the_view(conn):
    add_fetch(conn, "met-no", NOW, "Praha", {0: 1.0, 1: 2.0})                         # no extra arrays (NULL)
    add_fetch(conn, "open-meteo", NOW, "Praha", {0: 0.0, 1: 0.5},
              extras={"cloud_pct": {0: 30, 1: 80}, "wind_ms": {0: 2.5, 1: 4.0}, "weather_code": {0: 2, 1: 61}})
    rows = conn.execute("SELECT source, precip_mm, cloud_pct, wind_ms, weather_code FROM rain.forecast_hours "
                        "ORDER BY source, hour_start").fetchall()
    assert rows == [("met-no", 1.0, None, None, None), ("met-no", 2.0, None, None, None),
                    ("open-meteo", 0.0, 30, 2.5, 2), ("open-meteo", 0.5, 80, 4.0, 61)]


def test_an_extra_array_of_the_wrong_length_is_rejected(conn):
    add_fetch(conn, "open-meteo", NOW, "Praha", {0: 1.0})
    conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO rain.fetches (source, location_slug, fetched_at, first_hour, precip_mm, precip_prob, temp_c, cloud_pct) "
            "VALUES ('open-meteo', 'praha', %s, %s, '{1,2}', '{NULL,NULL}', '{5,6}', '{50}')", (NOW + timedelta(hours=1), H0))


def test_store_fetch_keeps_the_new_columns_and_leaves_them_null_for_met(conn):
    sun = (datetime(2026, 7, 1, 3, 15, tzinfo=timezone.utc), datetime(2026, 7, 1, 19, 45, tzinfo=timezone.utc))
    store_fetch(conn, "open-meteo", LOC, "praha", NOW - timedelta(hours=1), None,
                hourly_rows({0: 0.0, 1: 0.5}, extras={"cloud_pct": {0: 30, 1: 80}, "visibility_m": {0: 20000, 1: 9000}}, sun=sun))
    store_fetch(conn, "met-no", LOC, "praha", NOW - timedelta(hours=1), None, hourly_rows({0: 1.0}))
    row = conn.execute("SELECT cloud_pct, visibility_m, sunrise, sunset FROM rain.fetches WHERE source = 'open-meteo'").fetchone()
    assert row == ([30, 80], [20000, 9000], [sun[0]], [sun[1]])
    assert conn.execute("SELECT cloud_pct, sunrise FROM rain.fetches WHERE source = 'met-no'").fetchone() == (None, None)


def test_http_state_table_is_closed_to_the_api_roles(conn):
    assert conn.execute("SELECT to_regclass('rain.http_state')").fetchone()[0] is not None
    assert conn.execute("SELECT relrowsecurity FROM pg_class WHERE oid = 'rain.http_state'::regclass").fetchone()[0]
