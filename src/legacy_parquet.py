"""Reading the old Parquet snapshots (the `data` branch from before the pipeline wrote straight to Postgres).

The hourly pipeline no longer writes Parquet. This module only keeps the file layout and the reader, for
one-off jobs such as the backfill into the R2 archive:

    data/snapshots/source=open-meteo/date=2026-10-25/part-20261025T175400Z.parquet

Needs pyarrow (requirements-dev.txt / requirements-archive.txt), which the serverless app does not install.
"""
import re
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from src.common import slugify
from src.db import pack_fetch

# The schema of the snapshot files, with the columns that were added later last.
SCHEMA = pa.schema(
    [
        ("fetched_at", pa.timestamp("us", tz="UTC")),  # when WE downloaded it
        ("location", pa.string()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("valid_time", pa.timestamp("us", tz="UTC")),  # source's own timestamp
        ("hour_start", pa.timestamp("us", tz="UTC")),  # start of the hour the rain covers
        ("precip_mm", pa.float64()),
        ("precip_prob", pa.int32()),  # % ; null for MET Norway
        ("temp_c", pa.float64()),
        ("source_updated_at", pa.timestamp("us", tz="UTC")),  # model run time, if given
        # Added later, Open-Meteo only (null for MET Norway and in files written before they existed).
        ("cloud_pct", pa.int16()),
        ("cloud_low_pct", pa.int16()),
        ("cloud_mid_pct", pa.int16()),
        ("cloud_high_pct", pa.int16()),
        ("is_day", pa.int16()),
        ("sunshine_s", pa.int16()),
        ("weather_code", pa.int16()),
        ("visibility_m", pa.int32()),
        ("humidity_pct", pa.int16()),
        ("wind_ms", pa.float32()),
        ("gust_ms", pa.float32()),
        ("wind_dir_deg", pa.int16()),
        ("sunrise", pa.timestamp("us", tz="UTC")),
        ("sunset", pa.timestamp("us", tz="UTC")),
    ]
)

FILE_RE = re.compile(r"source=(?P<source>[^/\\]+)[/\\]date=(?P<date>[\d-]+)[/\\]part-(?P<stamp>\d{8}T\d{6}Z)\.parquet$")


def list_files(snapshot_dir: Path, since_date=None) -> list[dict]:
    """Parquet snapshot files as [{path, source, fetched_at}], oldest first."""
    files = []
    for path in sorted(Path(snapshot_dir).glob("source=*/date=*/part-*.parquet")):
        m = FILE_RE.search(str(path))
        if not m or (since_date and m["date"] < since_date.isoformat()):
            continue
        fetched_at = datetime.strptime(m["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        files.append({"path": path, "source": m["source"], "fetched_at": fetched_at})
    return sorted(files, key=lambda f: f["fetched_at"])


def fetches_from_file(path, source: str) -> tuple[list[dict], list[tuple]]:
    """One Parquet file -> (rows shaped like rain.fetches, locations seen in it)."""
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
