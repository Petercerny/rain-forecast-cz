"""Step 1: fetch both forecast APIs and store the results in Postgres.

Every call stores ONE row per source and place in rain.fetches (hourly values in arrays). Rows are only
ever added; old fetches are never touched. That history (what did each source predict, and when?) is
what a later job can join against observed weather to score the forecasts.

The job is meant to be called often (every ~10 minutes), so each source is polled politely:

  * MET Norway: not before the `Expires` time of the previous answer, and with If-Modified-Since.
  * Open-Meteo: not more often than `open_meteo_min_interval_minutes` (config/settings.yaml) per place.

That bookkeeping lives in rain.http_state (migration 003), because the function that runs this has no
disk. A call in which nothing is due stores nothing and costs no API requests.

Usage:
    python -m src.ingest                                  # everything that is due
    python -m src.ingest --force                          # ignore the "not before" times
    python -m src.ingest --source met-no --location Praha # one source, one place
Needs DATABASE_URL (see .env.example).
"""
import argparse
import copy
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from src.common import load_locations, load_settings, slugify
from src.db import connect, load_http_state, record_run, save_http_state, store_fetch
from src.parsers import met_updated_at, parse_met_no, parse_open_meteo

log = logging.getLogger("ingest")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
MET_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
SOURCES = ("open-meteo", "met-no")
DEFAULT_OPEN_METEO_INTERVAL_MIN = 55


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


def met_key(loc: dict) -> str:
    """The key of a place in the MET state: its truncated coordinates, as sent to the API."""
    return f"{trunc4(loc['lat']):.4f},{trunc4(loc['lon']):.4f}"


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
    key = met_key(loc)
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


# ---------------------------------------------------------------- polling rules


def is_due(entry: dict, now: datetime) -> bool:
    """False while the previous answer says "do not ask again before <expires>"."""
    return not (entry.get("expires") and now < datetime.fromisoformat(entry["expires"]))


def fetch_open_meteo_if_due(loc: dict, state: dict, now: datetime, interval: timedelta, force: bool = False) -> dict | None:
    """Fetch one Open-Meteo forecast unless this place was fetched less than `interval` ago (None then).

    `state` is updated in place; it is keyed by the place's slug.
    """
    key = slugify(loc["name"])
    if not force and not is_due(state.get(key, {}), now):
        log.info("open-meteo %s: fetched recently, skipping", loc["name"])
        return None
    payload = fetch_open_meteo(loc)
    state[key] = {"expires": (now + interval).isoformat()}  # only after a successful answer, so errors are retried
    return payload


# ---------------------------------------------------------------- main


def run(conn, sources=SOURCES, only_location: str | None = None, now: datetime | None = None,
        force: bool = False, note: str | None = None) -> dict:
    """Fetch what is due, store it, log the run. Returns {source: {stored, skipped, failures, rows}}."""
    locations = load_locations()
    if only_location:
        locations = [l for l in locations if l["name"].lower() == only_location.lower()]
        if not locations:
            raise SystemExit(f"No location named {only_location!r} in config/locations.yaml")

    fetched_at = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    interval = timedelta(minutes=load_settings().get("open_meteo_min_interval_minutes", DEFAULT_OPEN_METEO_INTERVAL_MIN))
    results = {}

    for source in sources:
        stored = skipped = failures = rows = 0
        state = load_http_state(conn, source)
        before = copy.deepcopy(state)
        for loc in locations:
            # One failing location or source must never break the whole run.
            try:
                if source == "open-meteo":
                    payload = fetch_open_meteo_if_due(loc, state, fetched_at, interval, force)
                    updated = None
                    if payload is not None:
                        parsed = parse_open_meteo(payload)
                else:
                    key = met_key(loc)
                    if force:  # drop the cached Expires / Last-Modified of this place
                        state.pop(key, None)
                    requested = is_due(state.get(key, {}), fetched_at)
                    payload = fetch_met_no(loc, state, fetched_at)
                    if payload is not None:
                        parsed, updated = parse_met_no(payload), met_updated_at(payload)
                    if requested:
                        time.sleep(0.3)  # MET asks clients to spread requests out
                if payload is None:
                    skipped += 1
                    continue
                store_fetch(conn, source, loc, slugify(loc["name"]), fetched_at, updated, parsed)
                stored += 1
                rows += len(parsed)
                log.info("%s %s: %d hourly rows", source, loc["name"], len(parsed))
            except Exception as exc:  # noqa: BLE001 - deliberately broad, see comment above
                failures += 1
                log.error("%s %s FAILED: %s", source, loc["name"], exc)
                print(f"::warning::{source} {loc['name']} failed: {exc}")  # shows up in GitHub Actions logs
        for key, entry in state.items():
            if before.get(key) != entry:
                save_http_state(conn, source, key, entry)
        if stored or failures:  # a run in which nothing was due is not worth a log line
            record_run(conn, fetched_at, source, rows, failures, note)
        conn.commit()
        results[source] = {"stored": stored, "skipped": skipped, "failures": failures, "rows": rows}
        log.info("%s: stored %d, skipped %d, failed %d", source, stored, skipped, failures)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    parser.add_argument("--location", help="only this location name (for testing)")
    parser.add_argument("--force", action="store_true", help="ignore the 'not before' times")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sources = SOURCES if args.source == "all" else (args.source,)
    with connect() as conn:
        results = run(conn, sources, args.location, force=args.force, note="cli")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
