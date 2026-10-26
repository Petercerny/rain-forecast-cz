"""Database helpers: connecting, and packing forecast rows into the array columns.

The database stores one row per fetch with hourly values in arrays (see
db/migrations/001_init.sql). `pack_fetch` builds those arrays from parsed hourly rows and
`unpack_fetch` turns them back into rows. Both are plain functions, no database needed.
"""
import os
from datetime import datetime, timedelta

import psycopg
from psycopg.conninfo import conninfo_to_dict

HOUR = timedelta(hours=1)

# The hourly values stored per fetch, one Postgres array column each (same names as the Parquet
# columns). The first three always exist; the rest come from Open-Meteo only, so they are NULL for
# MET Norway and for fetches made before they were added. This list is the one place to extend.
REQUIRED_ARRAYS = ("precip_mm", "precip_prob", "temp_c")
OPTIONAL_ARRAYS = (
    "cloud_pct", "cloud_low_pct", "cloud_mid_pct", "cloud_high_pct",
    "is_day", "sunshine_s", "weather_code", "visibility_m", "humidity_pct",
    "wind_ms", "gust_ms", "wind_dir_deg",
)
HOURLY_ARRAYS = REQUIRED_ARRAYS + OPTIONAL_ARRAYS
# Daily values (one element per day, a handful per fetch).
DAILY_ARRAYS = ("sunrise", "sunset")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def connect(url: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    """Open one connection. Uses $DATABASE_URL unless a URL is given.

    On Supabase use the SESSION POOLER string (port 5432, user "role.project-ref"): the direct
    connection is IPv6-only on the free plan and GitHub Actions only has IPv4.
    """
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    kwargs = {}
    host = conninfo_to_dict(url).get("host", "")
    if "sslmode" not in url and host not in LOCAL_HOSTS:
        kwargs["sslmode"] = "require"  # never fall back to an unencrypted connection to a remote database
    conn = psycopg.connect(url, autocommit=autocommit, **kwargs)
    conn.execute("SET TIME ZONE 'UTC'")  # so timestamps come back as UTC and date_trunc works on UTC hours
    return conn


def pack_fetch(rows: list[dict]) -> dict:
    """Parsed hourly rows -> {first_hour, <one array per HOURLY_ARRAYS column>, sunrise, sunset}.

    Each row needs hour_start (a whole hour) plus the HOURLY_ARRAYS keys (the optional ones may be
    missing). The list may be unsorted. An hour missing between the first and the last becomes None
    in every array. An optional array that is None for every hour is returned as None (stored as
    NULL, which is what every MET Norway fetch gets). sunrise / sunset are the distinct daily
    values found in the rows, oldest first.
    """
    if not rows:
        raise ValueError("cannot pack an empty fetch")
    by_hour = {}
    for r in rows:
        h = r["hour_start"]
        if (h.minute, h.second, h.microsecond) != (0, 0, 0):
            raise ValueError(f"hour_start must be a whole hour, got {h}")
        by_hour[h] = r  # if an hour appears twice the last one wins
    first, last = min(by_hour), max(by_hour)
    n = int((last - first) / HOUR) + 1
    packed = {"first_hour": first}
    for col in HOURLY_ARRAYS:
        values = [None] * n
        for h, r in by_hour.items():
            values[int((h - first) / HOUR)] = r.get(col)
        packed[col] = None if col in OPTIONAL_ARRAYS and all(v is None for v in values) else values
    days = sorted({(r["sunrise"], r["sunset"]) for r in rows if r.get("sunrise") and r.get("sunset")})
    packed["sunrise"] = [d[0] for d in days] or None
    packed["sunset"] = [d[1] for d in days] or None
    return packed


def unpack_fetch(first_hour: datetime, precip_mm: list, precip_prob: list, temp_c: list, **others) -> list[dict]:
    """The reverse of pack_fetch (the daily sunrise / sunset values are ignored).

    Hours without a precipitation value (gaps) are left out. An optional array that is None gives
    None for that value in every row.
    """
    arrays = {"precip_mm": precip_mm, "precip_prob": precip_prob, "temp_c": temp_c}
    arrays.update({k: v for k, v in others.items() if k in OPTIONAL_ARRAYS})
    rows = []
    for i, p in enumerate(precip_mm):
        if p is None:
            continue
        row = {"hour_start": first_hour + i * HOUR}
        for col in HOURLY_ARRAYS:
            values = arrays.get(col)
            row[col] = None if values is None else values[i]
        rows.append(row)
    return rows
