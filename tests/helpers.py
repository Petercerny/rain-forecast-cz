"""Small builders used by several test files."""
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from src.db import pack_fetch
from src.ingest import SCHEMA

NOW = datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc)  # summer: Prague = UTC+2
H0 = datetime(2026, 7, 1, 10, tzinfo=timezone.utc)       # start of the current hour = window start


def hourly_rows(hours: dict, temps=None, probs=None) -> list[dict]:
    """hours = {hour offset from H0: precip_mm} -> parsed hourly rows (what the parsers return)."""
    temps, probs = temps or {}, probs or {}
    return [
        {"hour_start": H0 + timedelta(hours=h), "precip_mm": mm, "precip_prob": probs.get(h),
         "temp_c": temps.get(h, 10.0)}
        for h, mm in hours.items()
    ]


def add_fetch(conn, source, fetched_at, name, hours, temps=None, probs=None, slug=None):
    """Insert one fetch (and its location) straight into the database."""
    slug = slug or name.lower()
    conn.execute(
        "INSERT INTO rain.locations (slug, name, lat, lon) VALUES (%s, %s, 50.0, 14.0) ON CONFLICT DO NOTHING",
        (slug, name),
    )
    p = pack_fetch(hourly_rows(hours, temps, probs))
    conn.execute(
        "INSERT INTO rain.fetches (source, location_slug, fetched_at, first_hour, precip_mm, precip_prob, temp_c) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (source, slug, fetched_at, p["first_hour"], p["precip_mm"], p["precip_prob"], p["temp_c"]),
    )


def write_parquet(folder, source, fetched_at, name, hours, temps=None, probs=None):
    """Write one snapshot file like ingest does (folder = the data/snapshots root)."""
    rows = [
        {"fetched_at": fetched_at, "location": name, "lat": 50.0, "lon": 14.0,
         "valid_time": r["hour_start"], "hour_start": r["hour_start"], "precip_mm": r["precip_mm"],
         "precip_prob": r["precip_prob"], "temp_c": r["temp_c"], "source_updated_at": None}
        for r in hourly_rows(hours, temps, probs)
    ]
    d = folder / f"source={source}" / f"date={fetched_at:%Y-%m-%d}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"part-{fetched_at:%Y%m%dT%H%M%SZ}.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), path)
    return path
