"""Small builders used by several test files."""
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from src.db import pack_fetch
from src.legacy_parquet import SCHEMA

NOW = datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc)  # summer: Prague = UTC+2
H0 = datetime(2026, 7, 1, 10, tzinfo=timezone.utc)       # start of the current hour = window start


def hourly_rows(hours: dict, temps=None, probs=None, extras=None, sun=None) -> list[dict]:
    """hours = {hour offset from H0: precip_mm} -> parsed hourly rows (what the parsers return).

    extras = {column: {hour offset: value}} for the Open-Meteo-only columns (cloud_pct, wind_ms, ...).
    sun = (sunrise, sunset) attached to every row, like the parser does.
    """
    temps, probs, extras = temps or {}, probs or {}, extras or {}
    rows = []
    for h, mm in hours.items():
        row = {"hour_start": H0 + timedelta(hours=h), "precip_mm": mm, "precip_prob": probs.get(h),
               "temp_c": temps.get(h, 10.0)}
        for col, by_hour in extras.items():
            row[col] = by_hour.get(h)
        if sun:
            row["sunrise"], row["sunset"] = sun
        rows.append(row)
    return rows


def add_fetch(conn, source, fetched_at, name, hours, temps=None, probs=None, slug=None, extras=None, sun=None):
    """Insert one fetch (and its location) straight into the database."""
    slug = slug or name.lower()
    conn.execute(
        "INSERT INTO rain.locations (slug, name, lat, lon) VALUES (%s, %s, 50.0, 14.0) ON CONFLICT DO NOTHING",
        (slug, name),
    )
    p = pack_fetch(hourly_rows(hours, temps, probs, extras, sun))
    columns = list(p)  # first_hour + every array column
    conn.execute(
        f"INSERT INTO rain.fetches (source, location_slug, fetched_at, {', '.join(columns)}) "
        f"VALUES (%s, %s, %s, {', '.join(['%s'] * len(columns))})",
        (source, slug, fetched_at, *p.values()),
    )


def write_parquet(folder, source, fetched_at, name, hours, temps=None, probs=None, extras=None, sun=None):
    """Write one old-style snapshot file (see src/legacy_parquet.py); folder = the data/snapshots root."""
    rows = [
        {"fetched_at": fetched_at, "location": name, "lat": 50.0, "lon": 14.0,
         "valid_time": r["hour_start"], "hour_start": r["hour_start"], "source_updated_at": None,
         **{k: v for k, v in r.items() if k != "hour_start"}}
        for r in hourly_rows(hours, temps, probs, extras, sun)
    ]
    d = folder / f"source={source}" / f"date={fetched_at:%Y-%m-%d}"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"part-{fetched_at:%Y%m%dT%H%M%SZ}.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), path)
    return path
