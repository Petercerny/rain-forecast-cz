"""Database helpers: connecting, storing fetches, and packing forecast rows into the array columns.

The database stores one row per fetch with hourly values in arrays (see
db/migrations/001_init.sql). `pack_fetch` builds those arrays from parsed hourly rows and
`unpack_fetch` turns them back into rows. Both are plain functions, no database needed.
`store_fetch` writes one fetch (and its location); `load_http_state` / `save_http_state` keep the
"do not ask again before" bookkeeping of the polled APIs (migration 003).
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

    On Supabase use a POOLER string (user "role.project-ref"), not the direct connection, which is
    IPv6-only on the free plan (GitHub Actions and Vercel only have IPv4). The session pooler (port
    5432) behaves like a normal connection; the transaction pooler (port 6543) works too because
    prepared statements are switched off below.
    """
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    kwargs = {}
    host = conninfo_to_dict(url).get("host", "")
    if "sslmode" not in url and host not in LOCAL_HOSTS:
        kwargs["sslmode"] = "require"  # never fall back to an unencrypted connection to a remote database
    # prepare_threshold=None: no server-side prepared statements, which the transaction pooler cannot keep.
    conn = psycopg.connect(url, autocommit=autocommit, prepare_threshold=None, **kwargs)
    conn.execute("SET TIME ZONE 'UTC'")  # so timestamps come back as UTC and date_trunc works on UTC hours
    return conn


# ---------------------------------------------------------------- writing

# The array columns come from the lists above, so a new column is added in one place.
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


def store_fetch(conn, source: str, loc: dict, slug: str, fetched_at: datetime, source_updated_at, rows: list[dict]) -> None:
    """Store one parsed forecast (hourly rows) and its location. All or nothing; safe to repeat
    (the same source, place and fetch time is ignored the second time)."""
    packed = pack_fetch(rows)
    with conn.transaction():
        conn.execute(UPSERT_LOCATION, (slug, loc["name"], loc["lat"], loc["lon"]))
        conn.execute(INSERT_FETCH, {"source": source, "slug": slug, "fetched_at": fetched_at,
                                    "source_updated_at": source_updated_at, **packed})


def record_run(conn, fetched_at: datetime, source: str, rows: int, failures: int, note: str | None = None) -> None:
    """One line in rain.ingest_runs: how many hourly rows a run stored and how many places failed."""
    conn.execute(
        "INSERT INTO rain.ingest_runs (fetched_at, source, rows_loaded, failures, note) VALUES (%s, %s, %s, %s, %s)",
        (fetched_at, source, rows, failures, note),
    )


def load_http_state(conn, source: str) -> dict:
    """{key: {"expires": ISO text, "last_modified": text}} for one source (keys without the "<source>:" prefix)."""
    cut = len(source) + 1
    cur = conn.execute("SELECT key, expires, last_modified FROM rain.http_state WHERE key LIKE %s", (source + ":%",))
    state = {}
    for key, expires, last_modified in cur:
        entry = {}
        if expires is not None:
            entry["expires"] = expires.isoformat()
        if last_modified is not None:
            entry["last_modified"] = last_modified
        state[key[cut:]] = entry
    return state


def save_http_state(conn, source: str, key: str, entry: dict) -> None:
    conn.execute(
        "INSERT INTO rain.http_state (key, expires, last_modified, updated_at) VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET expires = EXCLUDED.expires, last_modified = EXCLUDED.last_modified, "
        "updated_at = now()",
        (f"{source}:{key}", entry.get("expires"), entry.get("last_modified")),
    )


# ---------------------------------------------------------------- packing


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
