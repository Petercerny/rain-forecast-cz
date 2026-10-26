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
    """Parsed hourly rows -> {first_hour, precip_mm, precip_prob, temp_c} with NULL for gaps.

    Each row needs hour_start (a whole hour), precip_mm, precip_prob and temp_c. The list may be
    unsorted. An hour that is missing between the first and the last becomes None in all three arrays.
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
    precip, prob, temp = [None] * n, [None] * n, [None] * n
    for h, r in by_hour.items():
        i = int((h - first) / HOUR)
        precip[i], prob[i], temp[i] = r["precip_mm"], r["precip_prob"], r["temp_c"]
    return {"first_hour": first, "precip_mm": precip, "precip_prob": prob, "temp_c": temp}


def unpack_fetch(first_hour: datetime, precip_mm: list, precip_prob: list, temp_c: list) -> list[dict]:
    """The reverse of pack_fetch. Hours without a precipitation value (gaps) are left out."""
    return [
        {"hour_start": first_hour + i * HOUR, "precip_mm": p, "precip_prob": pr, "temp_c": t}
        for i, (p, pr, t) in enumerate(zip(precip_mm, precip_prob, temp_c))
        if p is not None
    ]
