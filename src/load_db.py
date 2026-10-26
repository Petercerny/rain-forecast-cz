"""Step 1b: copy the Parquet snapshots into the Postgres database.

Parquet files stay the raw, append-only log (they are also the backup). This step loads them
into the database, which is what the rest of the pipeline reads. It is safe to run any number
of times: a fetch that is already in the database is skipped.

    python -m src.load_db            # files of the last 7 days that are not in the database yet
    python -m src.load_db --days 30  # look further back
    python -m src.load_db --all      # every file: first-time load, or restore into an empty database

Because it looks back 7 days, a run that could not reach the database is repaired by the next one.
"""
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pyarrow.parquet as pq

from src.common import BUILD_DIR, SNAPSHOT_DIR, slugify
from src.db import DAILY_ARRAYS, HOURLY_ARRAYS, connect, pack_fetch

log = logging.getLogger("load_db")

FILE_RE = re.compile(r"source=(?P<source>[^/\\]+)[/\\]date=(?P<date>[\d-]+)[/\\]part-(?P<stamp>\d{8}T\d{6}Z)\.parquet$")

# The array columns come from the lists in src/db.py, so a new column is added in one place.
_ARRAY_COLUMNS = HOURLY_ARRAYS + DAILY_ARRAYS
INSERT_FETCH = (
    "INSERT INTO rain.fetches (source, location_slug, fetched_at, source_updated_at, first_hour, "
    + ", ".join(_ARRAY_COLUMNS) + ") "
    "VALUES (%(source)s, %(slug)s, %(fetched_at)s, %(source_updated_at)s, %(first_hour)s, "
    + ", ".join(f"%({c})s" for c in _ARRAY_COLUMNS) + ") "
    "ON CONFLICT (source, location_slug, fetched_at) DO NOTHING"
)
UPSERT_LOCATION = """
INSERT INTO rain.locations (slug, name, lat, lon) VALUES (%s, %s, %s, %s)
ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, lat = EXCLUDED.lat, lon = EXCLUDED.lon
"""


def list_files(snapshot_dir=SNAPSHOT_DIR, since_date=None) -> list[dict]:
    """Parquet snapshot files as [{path, source, stamp, fetched_at}], oldest first."""
    files = []
    for path in sorted(snapshot_dir.glob("source=*/date=*/part-*.parquet")):
        m = FILE_RE.search(str(path))
        if not m or (since_date and m["date"] < since_date.isoformat()):
            continue
        fetched_at = datetime.strptime(m["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        files.append({"path": path, "source": m["source"], "fetched_at": fetched_at})
    return sorted(files, key=lambda f: f["fetched_at"])


def fetches_from_file(path, source: str) -> tuple[list[dict], list[tuple]]:
    """One Parquet file -> (rows for rain.fetches, locations seen in it)."""
    rows = pq.read_table(path).to_pylist()
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["location"], r["fetched_at"], r["source_updated_at"]), []).append(r)
    fetches, locations = [], {}
    for (name, fetched_at, updated_at), group in groups.items():
        slug = slugify(name)
        locations[slug] = (slug, name, group[0]["lat"], group[0]["lon"])
        fetches.append({"source": source, "slug": slug, "fetched_at": fetched_at,
                        "source_updated_at": updated_at, **pack_fetch(group)})
    return fetches, list(locations.values())


def already_loaded(conn, since: datetime) -> set:
    """(source, fetched_at) pairs the database already has, since the given time."""
    cur = conn.execute("SELECT DISTINCT source, fetched_at FROM rain.fetches WHERE fetched_at >= %s", (since,))
    return {(s, t) for s, t in cur}


def load_files(conn, files: list[dict]) -> dict:
    """Insert the files that are not in the database yet. One transaction per file."""
    if not files:
        return {"files": 0, "fetches": 0}
    have = already_loaded(conn, min(f["fetched_at"] for f in files))
    n_files = n_fetches = 0
    for f in files:
        if (f["source"], f["fetched_at"]) in have:
            continue
        fetches, locations = fetches_from_file(f["path"], f["source"])
        with conn.transaction():  # a file is loaded completely or not at all
            conn.cursor().executemany(UPSERT_LOCATION, locations)
            conn.cursor().executemany(INSERT_FETCH, fetches)
        n_files += 1
        n_fetches += len(fetches)
        log.info("loaded %s (%d fetches)", f["path"].name, len(fetches))
    return {"files": n_files, "fetches": n_fetches}


def record_run(conn, result_file=BUILD_DIR / "ingest_result.json") -> int:
    """Copy build/ingest_result.json (written by ingest) into rain.ingest_runs."""
    if not result_file.exists():
        return 0
    result = json.loads(result_file.read_text(encoding="utf-8"))
    url = None
    if os.environ.get("GITHUB_RUN_ID"):
        url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    n = 0
    for source, r in result["sources"].items():
        conn.execute(
            "INSERT INTO rain.ingest_runs (fetched_at, source, rows_loaded, failures, github_run_url) "
            "VALUES (%s, %s, %s, %s, %s)",
            (datetime.fromisoformat(result["recorded_at"]), source, r["rows"], r["failures"], url),
        )
        n += 1
    conn.commit()
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--all", action="store_true", help="load every snapshot file")
    parser.add_argument("--days", type=int, default=7, help="how many days back to look (default 7)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    since = None if args.all else (datetime.now(timezone.utc) - timedelta(days=args.days)).date()
    files = list_files(since_date=since)
    with connect() as conn:
        result = load_files(conn, files)
        runs = record_run(conn)
    print(f"checked {len(files)} files, loaded {result['files']} files ({result['fetches']} fetches), "
          f"recorded {runs} run rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
