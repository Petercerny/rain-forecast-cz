"""Daily archive: copy finished days of data to Cloudflare R2, then trim Postgres to the newest 15 days.

Postgres keeps only a rolling window. Everything older lives in R2 as Parquet files (zstd), one file
per source per UTC day, laid out hive style so DuckDB / Spark can prune on source and date:

    forecasts/source=open-meteo/date=2026-10-25/part.parquet
    forecasts/source=met-no/date=2026-10-25/part.parquet
    observations/date=2026-10-25/part.parquet

Forecast files hold LONG rows: one row per forecast hour as predicted by one fetch, with a computed
`lead_hours` (hours from the fetch to the start of the forecast hour). `source` and `date` are not
columns, they come from the path (`date` = UTC day of `fetched_at`). Observation files hold the rows
of rain.observations as they are (`date` = UTC day of `observed_at`).

What a run does (python -m src.archive):
  1. EXPORT every complete UTC day that ended more than 2 days ago and is not in rain.archive_log.
     The Parquet is uploaded, read back, and its row count compared with Postgres. Only then is the
     day written to rain.archive_log. A day that fails is reported and retried the next night.
  2. PRUNE: delete rows from rain.fetches / rain.observations, but only for days that are in
     rain.archive_log, whose object is still in R2, whose row count still matches Postgres, and that
     ended more than 15 days ago.

Usage:
    python -m src.archive                        # export + prune
    python -m src.archive --dry-run              # read Postgres only: print what would be uploaded / deleted
    python -m src.archive --local-dir out        # write the Parquet files to a folder instead of R2;
                                                 # nothing is logged or deleted (a preview of the files)
    python -m src.archive --from-data-branch     # one-off: convert the old Parquet snapshots on the git
                                                 # branch origin/data to the layout above and upload them
                                                 # (needs no database; add --dry-run / --local-dir / --force)

Environment: ARCHIVE_DATABASE_URL (the `archiver` role, see src/db_migrate.py --create-archiver-role),
S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET and optionally S3_REGION: any S3-compatible
storage (Supabase Storage, Cloudflare R2, Backblaze B2, MinIO ...). The old R2_ACCOUNT_ID / R2_ACCESS_KEY_ID /
R2_SECRET_ACCESS_KEY / R2_BUCKET names still work for Cloudflare R2. Needs pyarrow and boto3
(requirements-archive.txt). The `pipeline` role cannot delete; only `archiver` can.
"""
import argparse
import io
import logging
import os
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from src.common import ROOT, slugify
from src.db import HOURLY_ARRAYS, connect

log = logging.getLogger("archive")

ARCHIVE_AFTER_DAYS = 2   # export a day once it ended at least this long ago (late writers are done by then)
KEEP_DAYS = 15           # a day older than this, and verified, is deleted from Postgres
SOURCES = ("open-meteo", "met-no")
UTC = timezone.utc
TS = pa.timestamp("us", tz="UTC")

# The forecast file layout. Types follow the Postgres columns (real -> float32, smallint -> int16).
_F32 = {"precip_mm", "temp_c", "wind_ms", "gust_ms"}
_I32 = {"visibility_m"}
FORECAST_SCHEMA = pa.schema(
    [("location_slug", pa.string()), ("fetched_at", TS), ("source_updated_at", TS),
     ("hour_start", TS), ("lead_hours", pa.float64())]
    + [(c, pa.float32() if c in _F32 else pa.int32() if c in _I32 else pa.int16()) for c in HOURLY_ARRAYS]
)

# Postgres type OIDs -> Arrow types, for tables exported "as they are" (observations).
_PG_TO_ARROW = {
    16: pa.bool_(), 21: pa.int16(), 23: pa.int32(), 20: pa.int64(), 700: pa.float32(), 701: pa.float64(),
    1700: pa.float64(), 25: pa.string(), 1043: pa.string(), 1082: pa.date32(), 1184: TS,
}


class ArchiveError(Exception):
    """One day could not be archived or verified. The day stays in Postgres and is retried next run."""


# ---------------------------------------------------------------- where the files go

class MemoryStore:
    """Objects in a dict: for tests. `durable` stores are the only ones that may lead to deletes."""
    durable = True

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def uri(self, key: str) -> str:
        return f"memory://{key}"


class NoStore:
    """Stands in for the bucket in a dry run without R2 credentials: has nothing, can store nothing."""
    durable = True
    offline = True   # prune_day then cannot check that the object exists and says so

    def put(self, key: str, data: bytes) -> None:
        raise RuntimeError("NoStore cannot store anything")

    def get(self, key: str) -> bytes | None:
        return None

    def uri(self, key: str) -> str:
        return key


class LocalStore:
    """A folder on this computer. NOT durable (it is not a backup), so nothing is logged or deleted."""
    durable = False

    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes | None:
        path = self.root / key
        return path.read_bytes() if path.exists() else None

    def uri(self, key: str) -> str:
        return (self.root / key).resolve().as_uri()


class R2Store:
    """A bucket on any S3-compatible storage (Cloudflare R2, Supabase Storage ...). `client` is a boto3 S3 client."""
    durable = True

    def __init__(self, client, bucket: str):
        self.client, self.bucket = client, bucket

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType="application/vnd.apache.parquet")

    def get(self, key: str) -> bytes | None:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as e:  # botocore's ClientError; matched by code so this file imports without boto3
            code = getattr(e, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"


def _env(*names: str) -> str | None:
    """The first of these environment variables that is set and not empty."""
    return next((os.environ[n] for n in names if os.environ.get(n)), None)


def s3_settings_from_env() -> dict:
    """Endpoint, keys, bucket and region of the archive storage, from S3_* (or the older R2_*) variables."""
    endpoint = _env("S3_ENDPOINT_URL")
    if not endpoint and os.environ.get("R2_ACCOUNT_ID"):
        endpoint = f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    settings = {
        "endpoint": endpoint,
        "key": _env("S3_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID"),
        "secret": _env("S3_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY"),
        "bucket": _env("S3_BUCKET", "R2_BUCKET"),
        "region": _env("S3_REGION") or "auto",
    }
    missing = [name for name, value in (("S3_ENDPOINT_URL", settings["endpoint"]), ("S3_ACCESS_KEY_ID", settings["key"]),
                                        ("S3_SECRET_ACCESS_KEY", settings["secret"]), ("S3_BUCKET", settings["bucket"])) if not value]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} not set (use --dry-run or --local-dir to run without storage)")
    return settings


def r2_store_from_env() -> R2Store:
    import boto3
    from botocore.config import Config

    cfg = s3_settings_from_env()
    client = boto3.client(
        "s3",
        endpoint_url=cfg["endpoint"],
        aws_access_key_id=cfg["key"],
        aws_secret_access_key=cfg["secret"],
        region_name=cfg["region"],
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "standard"},
            # Recent boto3 adds CRC checksums by default, which R2 and some other S3 servers do not accept.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return R2Store(client, cfg["bucket"])


# ---------------------------------------------------------------- the datasets

def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(), tzinfo=UTC)
    return start, start + timedelta(days=1)


def to_parquet_bytes(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def parquet_rows(data: bytes) -> int:
    """Row count of a Parquet file after really decoding it (not just reading the footer)."""
    return pq.read_table(io.BytesIO(data)).num_rows


class Forecasts:
    """The hourly forecasts of one source, one file per UTC day of `fetched_at`."""

    def __init__(self, source: str):
        self.source = source
        self.kind = f"forecasts:{source}"

    def key(self, day: date) -> str:
        return f"forecasts/source={self.source}/date={day:%Y-%m-%d}/part.parquet"

    def available(self, conn) -> bool:
        return True

    def days(self, conn, before: date) -> list[date]:
        """UTC days that have data and are before `before`."""
        rows = conn.execute(
            "SELECT DISTINCT (fetched_at AT TIME ZONE 'UTC')::date FROM rain.fetches "
            "WHERE source = %s AND fetched_at < %s ORDER BY 1",
            (self.source, day_bounds(before)[0])).fetchall()
        return [r[0] for r in rows]

    def count(self, conn, day: date) -> int:
        start, end = day_bounds(day)
        return conn.execute(
            "SELECT count(*) FROM rain.forecast_hours WHERE source = %s AND fetched_at >= %s AND fetched_at < %s",
            (self.source, start, end)).fetchone()[0]

    def export(self, conn, day: date) -> pa.Table:
        start, end = day_bounds(day)
        columns = ["location_slug", "fetched_at", "source_updated_at", "hour_start"]
        rows = conn.execute(
            "SELECT location_slug, fetched_at, source_updated_at, hour_start, "
            "       extract(epoch FROM hour_start - fetched_at)::float8 / 3600 AS lead_hours, "
            + ", ".join(HOURLY_ARRAYS) +
            " FROM rain.forecast_hours WHERE source = %s AND fetched_at >= %s AND fetched_at < %s "
            "ORDER BY location_slug, fetched_at, hour_start",
            (self.source, start, end)).fetchall()
        names = FORECAST_SCHEMA.names
        assert names[:4] == columns and names[4] == "lead_hours"
        return pa.Table.from_arrays(
            [pa.array([r[i] for r in rows], type=f.type) for i, f in enumerate(FORECAST_SCHEMA)],
            schema=FORECAST_SCHEMA)

    def delete(self, conn, day: date) -> int:
        start, end = day_bounds(day)
        return conn.execute(
            "DELETE FROM rain.fetches WHERE source = %s AND fetched_at >= %s AND fetched_at < %s",
            (self.source, start, end)).rowcount


class Observations:
    """Station readings (migration 004), one file per UTC day of `observed_at`, all columns as they are."""
    kind = "observations"

    def key(self, day: date) -> str:
        return f"observations/date={day:%Y-%m-%d}/part.parquet"

    def available(self, conn) -> bool:
        """False when the table does not exist (migration 004 not applied) or this role cannot read it."""
        return bool(conn.execute(
            "SELECT to_regclass('rain.observations') IS NOT NULL "
            "AND has_table_privilege('rain.observations', 'SELECT')").fetchone()[0])

    def days(self, conn, before: date) -> list[date]:
        rows = conn.execute(
            "SELECT DISTINCT (observed_at AT TIME ZONE 'UTC')::date FROM rain.observations "
            "WHERE observed_at < %s ORDER BY 1", (day_bounds(before)[0],)).fetchall()
        return [r[0] for r in rows]

    def count(self, conn, day: date) -> int:
        start, end = day_bounds(day)
        return conn.execute("SELECT count(*) FROM rain.observations WHERE observed_at >= %s AND observed_at < %s",
                            (start, end)).fetchone()[0]

    def export(self, conn, day: date) -> pa.Table:
        start, end = day_bounds(day)
        cur = conn.execute(
            "SELECT * FROM rain.observations WHERE observed_at >= %s AND observed_at < %s "
            "ORDER BY station_wsi, observed_at", (start, end))
        columns = [c.name for c in cur.description]
        types = []
        for c in cur.description:
            if c.type_code not in _PG_TO_ARROW:
                raise ArchiveError(f"rain.observations.{c.name} has a type (oid {c.type_code}) src/archive.py cannot write")
            types.append(_PG_TO_ARROW[c.type_code])
        rows = cur.fetchall()
        return pa.Table.from_arrays([pa.array([r[i] for r in rows], type=t) for i, t in enumerate(types)], names=columns)

    def delete(self, conn, day: date) -> int:
        start, end = day_bounds(day)
        return conn.execute("DELETE FROM rain.observations WHERE observed_at >= %s AND observed_at < %s",
                            (start, end)).rowcount


def all_datasets() -> list:
    return [Forecasts(s) for s in SOURCES] + [Observations()]


# ---------------------------------------------------------------- export

def archive_day(conn, store, ds, day: date, *, dry_run: bool = False) -> dict:
    """Export one day, upload it, read it back and compare it with Postgres. Returns a result dict.

    Writes rain.archive_log only after the read-back matched (and only for durable stores).
    Raises ArchiveError when anything does not match; then nothing is logged.
    """
    key = ds.key(day)
    n_pg = ds.count(conn, day)
    result = {"kind": ds.kind, "day": day, "rows": n_pg, "key": key, "uploaded": False}
    if n_pg == 0:   # fetches whose hours are all gaps: nothing to keep, but record that the day is done
        if not dry_run and store.durable:
            _log_day(conn, ds.kind, day, 0, None)
        return result
    table = ds.export(conn, day)
    if table.num_rows != n_pg:
        raise ArchiveError(f"{key}: exported {table.num_rows} rows but Postgres counted {n_pg}")
    data = to_parquet_bytes(table)
    result["bytes"] = len(data)
    if dry_run:
        return result

    old = store.get(key)   # never replace a bigger archive object (for example the backfill) with a smaller one
    if old is not None and parquet_rows(old) > n_pg:
        raise ArchiveError(f"{key}: the archive already holds {parquet_rows(old)} rows, Postgres only {n_pg}; not replacing it")
    store.put(key, data)
    back = store.get(key)
    if back is None:
        raise ArchiveError(f"{key}: not found after uploading it")
    n_back, n_now = parquet_rows(back), ds.count(conn, day)
    if back != data or n_back != n_pg or n_now != n_pg:
        raise ArchiveError(f"{key}: verification failed (exported {n_pg}, read back {n_back}, Postgres now {n_now})")
    result["uploaded"] = True
    if store.durable:
        _log_day(conn, ds.kind, day, n_pg, store.uri(key))
    return result


def _log_day(conn, kind: str, day: date, rows: int, uri: str | None) -> None:
    conn.execute(
        "INSERT INTO rain.archive_log (kind, day, rows, uri) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (kind, day) DO UPDATE SET rows = EXCLUDED.rows, uri = EXCLUDED.uri, archived_at = now()",
        (kind, day, rows, uri))
    conn.commit()


# ---------------------------------------------------------------- prune

def prune_day(conn, store, ds, day: date, *, now: datetime, dry_run: bool = False) -> int | None:
    """Delete one verified day from Postgres. Returns the rows deleted, or None if it was refused.

    Refuses unless ALL of these hold: the day ended more than KEEP_DAYS ago; rain.archive_log has it;
    its object is still in the store (not checked by a dry run without R2 credentials); Postgres still holds exactly the logged number of rows.
    """
    if day >= (now - timedelta(days=KEEP_DAYS)).date():
        raise ValueError(f"{day} is not older than {KEEP_DAYS} days, refusing to delete it")
    logged = conn.execute("SELECT rows, uri FROM rain.archive_log WHERE kind = %s AND day = %s",
                          (ds.kind, day)).fetchone()
    if logged is None:
        log.warning("%s %s: not in archive_log, keeping it", ds.kind, day)
        return None
    rows, uri = logged
    if rows > 0 and not getattr(store, "offline", False):
        data = store.get(ds.key(day))
        if data is None or parquet_rows(data) != rows:
            log.warning("%s %s: the archive object is missing or has a different row count, keeping it", ds.kind, day)
            return None
    n_pg = ds.count(conn, day)
    if n_pg != rows:
        log.warning("%s %s: Postgres has %d rows but %d were archived, keeping it", ds.kind, day, n_pg, rows)
        return None
    if dry_run:
        return n_pg
    with conn.transaction():
        deleted = ds.delete(conn, day)
    conn.commit()
    return deleted


def prune(conn, store, datasets, *, now: datetime, dry_run: bool = False) -> list[dict]:
    """Prune every logged day older than KEEP_DAYS. Returns [{kind, day, deleted}] for the days that went."""
    by_kind = {ds.kind: ds for ds in datasets if ds.available(conn)}
    cutoff = (now - timedelta(days=KEEP_DAYS)).date()
    logged = conn.execute("SELECT kind, day FROM rain.archive_log WHERE day < %s ORDER BY day, kind", (cutoff,)).fetchall()
    out = []
    for kind, day in logged:
        ds = by_kind.get(kind)
        if ds is None:
            continue
        deleted = prune_day(conn, store, ds, day, now=now, dry_run=dry_run)
        if deleted:
            out.append({"kind": kind, "day": day, "deleted": deleted})
    return out


# ---------------------------------------------------------------- the daily run

def run(conn, store, *, now: datetime | None = None, dry_run: bool = False) -> dict:
    """Export every finished day not yet archived, then prune. Returns {archived, pruned, failures}."""
    now = now or datetime.now(UTC)
    before = (now - timedelta(days=ARCHIVE_AFTER_DAYS)).date()   # days strictly before this are old enough
    datasets = all_datasets()
    archived, failures = [], []
    for ds in datasets:
        if not ds.available(conn):
            log.info("%s: table not available, skipping", ds.kind)
            continue
        done = {d for (d,) in conn.execute("SELECT day FROM rain.archive_log WHERE kind = %s", (ds.kind,))}
        for day in ds.days(conn, before):
            if day in done:
                continue
            try:
                res = archive_day(conn, store, ds, day, dry_run=dry_run)
            except Exception as e:
                conn.rollback()
                log.error("%s %s: %s", ds.kind, day, e)
                failures.append({"kind": ds.kind, "day": day, "error": str(e)})
                continue
            archived.append(res)
            log.info("%s %s: %d rows %s", ds.kind, day, res["rows"],
                     "(dry run, not uploaded)" if dry_run else "archived" if res["uploaded"] else "empty day")
    pruned = []
    if store.durable or dry_run:
        pruned = prune(conn, store, datasets, now=now, dry_run=dry_run)
        for p in pruned:
            log.info("%s %s: %s %d rows from Postgres", p["kind"], p["day"], "would delete" if dry_run else "deleted", p["deleted"])
    else:
        log.info("local folder store: nothing is logged or deleted")
    return {"archived": archived, "pruned": pruned, "failures": failures}


# ---------------------------------------------------------------- backfill from the old data branch

SNAPSHOT_RE = re.compile(r"^snapshots/source=(?P<source>[^/]+)/date=(?P<date>\d{4}-\d{2}-\d{2})/part-[^/]+\.parquet$")


def convert_snapshot_rows(rows: list[dict]) -> pa.Table:
    """Rows of the old per-run snapshot files (see src/legacy_parquet.py) -> the forecast layout above.

    Like the database view, hours without a precipitation value are dropped. The same hour of the same
    fetch appearing twice is kept once.
    """
    out = {}
    for r in rows:
        if r.get("precip_mm") is None:
            continue
        slug, fetched, hour = slugify(r["location"]), r["fetched_at"], r["hour_start"]
        out[(slug, fetched, hour)] = {
            "location_slug": slug, "fetched_at": fetched, "source_updated_at": r.get("source_updated_at"),
            "hour_start": hour, "lead_hours": (hour - fetched).total_seconds() / 3600,
            **{c: r.get(c) for c in HOURLY_ARRAYS},
        }
    ordered = [out[k] for k in sorted(out)]
    return pa.Table.from_pylist(ordered, schema=FORECAST_SCHEMA)


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def backfill_from_data_branch(store, *, ref: str = "origin/data", today: date | None = None,
                              dry_run: bool = False, force: bool = False) -> list[dict]:
    """Convert the Parquet snapshots committed on the `data` branch into the archive layout.

    One file per source and day; only days before today (UTC), because the files of today are still
    growing. A day that already exists in the store is skipped unless `force`. The daily export later
    replaces a day with the Postgres version (the same data) unless that would be smaller.
    """
    today = today or datetime.now(UTC).date()
    try:
        names = _git("ls-tree", "-r", "--name-only", ref, "--", "snapshots/").decode().split()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"cannot read git ref {ref!r} (run `git fetch origin data` first): {e.stderr.decode().strip()}") from e
    groups: dict[tuple[str, str], list[str]] = {}
    for name in names:
        m = SNAPSHOT_RE.match(name)
        if m:
            groups.setdefault((m["source"], m["date"]), []).append(name)
    results = []
    for (source, day_text), files in sorted(groups.items()):
        day = date.fromisoformat(day_text)
        if day >= today:
            continue
        key = Forecasts(source).key(day)
        if not force and store.get(key) is not None:
            log.info("%s: already there, skipping", key)
            continue
        rows = []
        for name in files:
            rows += pq.read_table(io.BytesIO(_git("show", f"{ref}:{name}"))).to_pylist()
        table = convert_snapshot_rows(rows)
        data = to_parquet_bytes(table)
        res = {"key": key, "files": len(files), "rows": table.num_rows, "uploaded": False}
        if not dry_run:
            store.put(key, data)
            if store.get(key) != data or parquet_rows(store.get(key)) != table.num_rows:
                raise ArchiveError(f"{key}: verification after upload failed")
            res["uploaded"] = True
        log.info("%s: %d files -> %d rows%s", key, len(files), table.num_rows, "" if res["uploaded"] else " (not uploaded)")
        results.append(res)
    return results


# ---------------------------------------------------------------- command line

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="upload nothing, delete nothing, just report")
    parser.add_argument("--local-dir", type=Path, help="write the files to this folder instead of R2 (nothing is logged or deleted)")
    parser.add_argument("--from-data-branch", action="store_true", help="convert the old Parquet snapshots on origin/data")
    parser.add_argument("--git-ref", default="origin/data", help="with --from-data-branch: the branch to read")
    parser.add_argument("--force", action="store_true", help="with --from-data-branch: overwrite days that already exist")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.local_dir:
        store = LocalStore(args.local_dir)
    elif args.dry_run and not _env("S3_BUCKET", "R2_BUCKET"):
        store = NoStore()
    else:
        try:
            store = r2_store_from_env()
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 1

    if args.from_data_branch:
        results = backfill_from_data_branch(store, ref=args.git_ref, dry_run=args.dry_run, force=args.force)
        print(f"{len(results)} day(s) converted" + (" (dry run)" if args.dry_run else ""))
        return 0

    url = os.environ.get("ARCHIVE_DATABASE_URL")
    if not url:
        print("ARCHIVE_DATABASE_URL is not set (see README, 'Long-term archive').", file=sys.stderr)
        return 1
    with connect(url) as conn:
        result = run(conn, store, dry_run=args.dry_run)
    print(f"archived {len(result['archived'])} day file(s), pruned {len(result['pruned'])} day(s), "
          f"{len(result['failures'])} failure(s)" + (" (dry run)" if args.dry_run else ""))
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
