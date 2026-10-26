"""Step 1: fetch both forecast APIs and append the results as Parquet snapshots.

Layout (hive-style partitions, so DuckDB / Spark can prune on source and date):

    data/snapshots/source=open-meteo/date=2026-10-25/part-20261025T175400Z.parquet
    data/snapshots/source=met-no/date=2026-10-25/part-20261025T175400Z.parquet

`date` is the UTC date of the fetch. Every run writes NEW files; old snapshots are never
touched. That history (what did each source predict, and when?) is what a later job can
join against observed rainfall to score the forecasts.

Usage:
    python -m src.ingest                                  # everything
    python -m src.ingest --source met-no --location Praha # one source, one place
"""
import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import pyarrow as pa
import pyarrow.parquet as pq
import requests

from src.common import BUILD_DIR, CACHE_DIR, SNAPSHOT_DIR, load_locations
from src.parsers import met_updated_at, parse_met_no, parse_open_meteo

log = logging.getLogger("ingest")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
MET_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
MET_STATE_FILE = CACHE_DIR / "met_state.json"
SOURCES = ("open-meteo", "met-no")

# Explicit schema = the contract between ingest and everything downstream (SQL, Spark...).
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
        # See src/parsers.py for what each one means.
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


def trunc4(x: float) -> float:
    """Truncate (not round) to 4 decimals, as MET Norway's terms of service require."""
    # round(x * 1e6) first so float noise (50.088 * 10000 = 500879.99999...) cannot lose a digit
    return math.trunc(round(x * 1_000_000) / 100) / 10_000


# ---------------------------------------------------------------- Open-Meteo


# Everything we ask Open-Meteo for in ONE request per place (above 10 variables a request may count
# as more than one call against the free limit, which is still tiny for our number of places).
OPEN_METEO_HOURLY = (
    "precipitation,precipitation_probability,temperature_2m,"
    "cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
    "is_day,sunshine_duration,weather_code,visibility,"
    "relative_humidity_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m"
)


def fetch_open_meteo(loc: dict) -> dict:
    params = {
        "latitude": trunc4(loc["lat"]),
        "longitude": trunc4(loc["lon"]),
        "hourly": OPEN_METEO_HOURLY,
        "daily": "sunrise,sunset",
        "wind_speed_unit": "ms",  # m/s, the same unit as MET Norway
        "forecast_days": 3,
        "timezone": "UTC",  # keeps timestamps unambiguous
    }
    resp = requests.get(OPEN_METEO_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------- MET Norway


def _load_met_state() -> dict:
    try:
        return json.loads(MET_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _save_met_state(state: dict) -> None:
    MET_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MET_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def fetch_met_no(loc: dict, state: dict, now: datetime) -> dict | None:
    """Fetch one MET Norway forecast, honouring their caching rules.

    Returns the JSON payload, or None when there is nothing new (still cached, or 304).
    `state` is updated in place with the new Expires / Last-Modified values.
    """
    user_agent = os.environ.get("USER_AGENT")
    if not user_agent:
        # MET blocks (403) generic user agents, so refuse to send a made-up one.
        raise RuntimeError("USER_AGENT is not set (see .env.example)")

    lat, lon = trunc4(loc["lat"]), trunc4(loc["lon"])
    key = f"{lat:.4f},{lon:.4f}"
    cached = state.get(key, {})

    # Rule 1: do not ask again before the Expires time of the previous response.
    if cached.get("expires") and now < datetime.fromisoformat(cached["expires"]):
        log.info("met-no %s: still cached until %s, skipping", loc["name"], cached["expires"])
        return None

    headers = {"User-Agent": user_agent}
    # Rule 2: send the previous Last-Modified back as If-Modified-Since.
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]

    resp = requests.get(MET_URL, params={"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"},
                        headers=headers, timeout=30)
    if resp.status_code == 203:
        log.warning("met-no returned 203: this API version is deprecated, check the docs")

    entry = dict(cached)
    if "Expires" in resp.headers:
        entry["expires"] = parsedate_to_datetime(resp.headers["Expires"]).isoformat()
    if resp.status_code == 304:
        state[key] = entry
        log.info("met-no %s: 304 not modified", loc["name"])
        return None
    resp.raise_for_status()
    if "Last-Modified" in resp.headers:
        entry["last_modified"] = resp.headers["Last-Modified"]
    state[key] = entry
    return resp.json()


# ---------------------------------------------------------------- Parquet output


def to_table(rows: list[dict], loc: dict, fetched_at: datetime, updated_at) -> list[dict]:
    """Attach the bookkeeping columns to parsed rows."""
    return [
        {"fetched_at": fetched_at, "location": loc["name"], "lat": loc["lat"], "lon": loc["lon"],
         "source_updated_at": updated_at, **r}
        for r in rows  # keys a source does not have (MET Norway: clouds, wind...) become null in Parquet
    ]


def write_snapshot(source: str, all_rows: list[dict], fetched_at: datetime) -> str | None:
    """Write one new Parquet file for this source. Never overwrites an existing file."""
    if not all_rows:
        return None
    folder = SNAPSHOT_DIR / f"source={source}" / f"date={fetched_at:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"part-{fetched_at:%Y%m%dT%H%M%SZ}.parquet"
    if path.exists():  # two runs in the same second: refuse rather than overwrite
        raise FileExistsError(path)
    pq.write_table(pa.Table.from_pylist(all_rows, schema=SCHEMA), path, compression="zstd")
    return str(path)


# ---------------------------------------------------------------- main


def run(sources=SOURCES, only_location: str | None = None) -> dict:
    locations = load_locations()
    if only_location:
        locations = [l for l in locations if l["name"].lower() == only_location.lower()]
        if not locations:
            raise SystemExit(f"No location named {only_location!r} in config/locations.yaml")

    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    results = {}

    for source in sources:
        rows, failures = [], 0
        met_state = _load_met_state() if source == "met-no" else None
        for loc in locations:
            # One failing location or source must never break the whole run.
            try:
                if source == "open-meteo":
                    payload = fetch_open_meteo(loc)
                    parsed, updated = parse_open_meteo(payload), None
                else:
                    payload = fetch_met_no(loc, met_state, fetched_at)
                    if payload is None:
                        continue
                    parsed, updated = parse_met_no(payload), met_updated_at(payload)
                    time.sleep(0.3)  # MET asks clients to spread requests out
                rows += to_table(parsed, loc, fetched_at, updated)
                log.info("%s %s: %d hourly rows", source, loc["name"], len(parsed))
            except Exception as exc:  # noqa: BLE001 - deliberately broad, see comment above
                failures += 1
                log.error("%s %s FAILED: %s", source, loc["name"], exc)
                print(f"::warning::{source} {loc['name']} failed: {exc}")  # shows up in GitHub Actions
        if met_state is not None:
            _save_met_state(met_state)
        path = write_snapshot(source, rows, fetched_at)
        results[source] = {"rows": len(rows), "failures": failures, "file": path}
        log.info("%s: wrote %s", source, path or "nothing")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    parser.add_argument("--location", help="only this location name (for testing)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sources = SOURCES if args.source == "all" else (args.source,)
    results = run(sources, args.location)
    print(json.dumps(results, indent=2))
    # load_db records this in the database's run log (rain.ingest_runs).
    BUILD_DIR.mkdir(exist_ok=True)
    (BUILD_DIR / "ingest_result.json").write_text(
        json.dumps({"recorded_at": datetime.now(timezone.utc).isoformat(), "sources": results}, indent=1),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
