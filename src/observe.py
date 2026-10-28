"""Measured weather: copy the 10-minute readings of ČHMÚ's automatic stations into the database.

    python -m src.observe                   # every station used in config/locations.yaml
    python -m src.observe --location Brno   # one city (for testing)

`run(conn)` is also what the Vercel `/api/ingest` endpoint calls after the forecast fetch.

Facts about the data (checked against Klimatologicka_data_popis.pdf and the live files, 2026-10-26):

  * One file per station and UTC day:
        https://opendata.chmi.cz/meteorology/climate/now/data/10m-<WSI>-<YYYYMMDD>.json
    ČHMÚ rewrites it ONCE AN HOUR, at about HH:01-HH:05 (Last-Modified shows this), and at HH:01 it holds
    the readings up to HH-1:50. So the newest reading is between ~11 and ~70 minutes old, and polling
    more often than hourly only earns 304 Not Modified answers (cheap, and we send If-Modified-Since).
    The previous day's file gets its last rewrite at 00:02, so just after midnight UTC we read both.
    Files older than 24 hours are deleted by ČHMÚ. Never list the directory: it holds thousands of files.
  * Rows are LONG: [STATION, ELEMENT, DT (UTC), VAL, FLAG, QUALITY], one row per element and 10 minutes.
    We pivot them by DT. Elements and units: T deg C, H %, P hPa (at the station, not sea level),
    F wind m/s, Fmax gust m/s, D wind direction degrees, SRA10M rain mm per 10 minutes, SSV10M sunshine
    seconds per 10 minutes. The other elements in the file (TMA, Dprum, T05...) are ignored.
  * VAL is "" when there is no value. QUALITY (meta4): 0 good, 1 suspect, 2 poor (do not use yet),
    3 estimated, 4 missing, 5 unknown (the usual value in the live files). We drop 2 and 4.
  * CORS: not relevant here, this module runs on the server.
"""
import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from src.common import load_locations
from src.stations import fetch_json as fetch_meta, parse_meta1

log = logging.getLogger("observe")

DATA_URL = "https://opendata.chmi.cz/meteorology/climate/now/data/10m-{wsi}-{day:%Y%m%d}.json"
# ČHMÚ element -> column of rain.observations
ELEMENT_COLUMNS = {
    "T": "temp_c", "H": "humidity_pct", "P": "pressure_hpa", "F": "wind_ms", "Fmax": "gust_ms",
    "D": "wind_dir_deg", "SRA10M": "precip_10m_mm", "SSV10M": "sunshine_s",
}
COLUMNS = tuple(ELEMENT_COLUMNS.values())
UNUSABLE_QUALITY = (2, 4)
# Also read yesterday's file in the first hours of the UTC day (its last rewrite is at 00:02).
YESTERDAY_UNTIL_HOUR = 2
WORKERS = 6

UPSERT = f"""
INSERT INTO rain.observations (station_wsi, observed_at, {', '.join(COLUMNS)})
VALUES (%(station_wsi)s, %(observed_at)s, {', '.join(f'%({c})s' for c in COLUMNS)})
ON CONFLICT (station_wsi, observed_at) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in COLUMNS)}
WHERE ({', '.join(f'rain.observations.{c}' for c in COLUMNS)})
      IS DISTINCT FROM ({', '.join(f'EXCLUDED.{c}' for c in COLUMNS)})
"""


# ---------------------------------------------------------------- parsing (no network, no database)


def parse_station_file(payload: dict) -> list[dict]:
    """A 10m-<WSI>-<day>.json payload -> one dict per DT: station_wsi, observed_at and the COLUMNS.

    Unusable values are left out; a DT where nothing usable remains gives no row. Oldest first.
    """
    block = payload["data"]["data"]
    header = block["header"].split(",")
    i_station, i_el, i_dt = header.index("STATION"), header.index("ELEMENT"), header.index("DT")
    i_val, i_quality = header.index("VAL"), header.index("QUALITY")
    by_time: dict[str, dict] = {}
    for row in block["values"]:
        column = ELEMENT_COLUMNS.get(row[i_el])
        value = row[i_val]
        if column is None or value in ("", None) or row[i_quality] in UNUSABLE_QUALITY:
            continue
        entry = by_time.setdefault(row[i_dt], {"station_wsi": row[i_station]})
        entry[column] = round(float(value)) if column == "wind_dir_deg" else float(value)
    out = []
    for dt, entry in sorted(by_time.items()):
        out.append({"observed_at": datetime.fromisoformat(dt.replace("Z", "+00:00")),
                    **{c: entry.get(c) for c in COLUMNS}, "station_wsi": entry["station_wsi"]})
    return out


def days_to_fetch(now: datetime) -> list:
    """UTC days whose file to read: today, plus yesterday during the first hours of the day."""
    today = now.astimezone(timezone.utc).date()
    return [today - timedelta(days=1), today] if now.astimezone(timezone.utc).hour < YESTERDAY_UNTIL_HOUR else [today]


def configured_stations(locations: list[dict]) -> list[str]:
    """Every WSI named in the locations (`station` and `rain_station`), once each, in config order."""
    wsis = []
    for loc in locations:
        for key in ("station", "rain_station"):
            if loc.get(key) and loc[key] not in wsis:
                wsis.append(loc[key])
    return wsis


# ---------------------------------------------------------------- network


def fetch_file(http, url: str, state: dict | None) -> dict:
    """Download one station file, asking only for it if changed. Returns {"status": ...}:

    "ok" (+ payload, last_modified, etag), "not_modified", or "missing" (404: the day's file does not exist
    yet). Raises for any other HTTP problem.
    """
    headers = {}
    if state and state.get("last_modified"):
        headers["If-Modified-Since"] = state["last_modified"]
    if state and state.get("etag"):
        headers["If-None-Match"] = state["etag"]
    resp = http.get(url, headers=headers, timeout=30)
    if resp.status_code == 304:
        return {"status": "not_modified"}
    if resp.status_code == 404:
        return {"status": "missing"}
    resp.raise_for_status()
    return {"status": "ok", "payload": resp.json(),
            "last_modified": resp.headers.get("Last-Modified"), "etag": resp.headers.get("ETag")}


def _session() -> requests.Session:
    s = requests.Session()
    if os.environ.get("USER_AGENT"):
        s.headers["User-Agent"] = os.environ["USER_AGENT"]
    return s


# ---------------------------------------------------------------- database


def load_state(conn, urls: list[str]) -> dict:
    rows = conn.execute("SELECT url, last_modified, etag FROM rain.observation_sources WHERE url = ANY(%s)", (urls,))
    return {url: {"last_modified": lm, "etag": etag} for url, lm, etag in rows}


def ensure_stations(conn, wsis: list[str], http=None, now: datetime | None = None) -> list[str]:
    """Make sure every WSI is in rain.stations (name and position come from meta1). Returns unknown WSIs."""
    known = {r[0] for r in conn.execute("SELECT wsi FROM rain.stations WHERE wsi = ANY(%s)", (wsis,))}
    missing = [w for w in wsis if w not in known]
    if not missing:
        return []
    meta = {s["wsi"]: s for s in parse_meta1(fetch_meta("meta1", now, session=http))}
    unknown = [w for w in missing if w not in meta]
    for w in missing:
        if w in meta:
            s = meta[w]
            conn.execute(
                "INSERT INTO rain.stations (wsi, name, lat, lon, elevation) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (wsi) DO NOTHING", (w, s["name"], s["lat"], s["lon"], s["elevation"]))
    return unknown


def store(conn, url: str, rows: list[dict], fetched: dict) -> None:
    """Upsert the rows and remember the file's Last-Modified / ETag, in one transaction."""
    with conn.transaction():
        if rows:
            with conn.cursor() as cur:
                cur.executemany(UPSERT, rows)
        conn.execute(
            "INSERT INTO rain.observation_sources (url, last_modified, etag, checked_at) VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (url) DO UPDATE SET last_modified = EXCLUDED.last_modified, etag = EXCLUDED.etag, "
            "checked_at = EXCLUDED.checked_at", (url, fetched.get("last_modified"), fetched.get("etag")))


def touch(conn, url: str) -> None:
    conn.execute("UPDATE rain.observation_sources SET checked_at = now() WHERE url = %s", (url,))


# ---------------------------------------------------------------- the run


def run(conn, now: datetime | None = None, locations: list[dict] | None = None, http=None) -> dict:
    """Fetch the station files that changed and store their readings. Never raises for one bad station.

    Returns {"stations", "files", "not_modified", "missing", "rows", "failures", "newest", "errors"}.
    """
    now = now or datetime.now(timezone.utc)
    wsis = configured_stations(locations if locations is not None else load_locations())
    result = {"stations": len(wsis), "files": 0, "not_modified": 0, "missing": 0, "rows": 0,
              "failures": 0, "newest": None, "errors": []}
    if not wsis:
        return result
    http = http or _session()

    try:
        for w in ensure_stations(conn, wsis, http, now):
            result["failures"] += 1
            result["errors"].append(f"{w}: not in the ČHMÚ station list")
            wsis.remove(w)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - the station list is optional once the stations are stored
        conn.rollback()
        result["failures"] += 1
        result["errors"].append(f"station list: {exc}")
        known = {r[0] for r in conn.execute("SELECT wsi FROM rain.stations")}
        wsis = [w for w in wsis if w in known]

    jobs = [(w, DATA_URL.format(wsi=w, day=d)) for w in wsis for d in days_to_fetch(now)]
    state = load_state(conn, [url for _, url in jobs])

    def download(job):
        wsi, url = job
        try:
            return job, fetch_file(http, url, state.get(url)), None
        except Exception as exc:  # noqa: BLE001 - one station must not stop the others
            return job, None, exc

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        downloads = list(pool.map(download, jobs))

    for (wsi, url), fetched, error in downloads:
        try:
            if error:
                raise error
            if fetched["status"] == "not_modified":
                result["not_modified"] += 1
                touch(conn, url)
            elif fetched["status"] == "missing":
                result["missing"] += 1
            else:
                rows = parse_station_file(fetched["payload"])
                store(conn, url, rows, fetched)
                result["files"] += 1
                result["rows"] += len(rows)
                if rows and (result["newest"] is None or rows[-1]["observed_at"] > result["newest"]):
                    result["newest"] = rows[-1]["observed_at"]
        except Exception as exc:  # noqa: BLE001
            result["failures"] += 1
            result["errors"].append(f"{wsi}: {exc}")
            log.error("observe %s FAILED: %s", wsi, exc)
    conn.commit()
    result["newest"] = result["newest"] and result["newest"].strftime("%Y-%m-%dT%H:%M:%SZ")
    result["errors"] = result["errors"][:10]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--location", help="only this location name (for testing)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from src.db import connect

    locations = load_locations()
    if args.location:
        locations = [l for l in locations if l["name"].lower() == args.location.lower()]
        if not locations:
            raise SystemExit(f"No location named {args.location!r} in config/locations.yaml")
    with connect() as conn:
        result = run(conn, locations=locations)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if result["failures"] and not result["files"] and not result["not_modified"] else 0


if __name__ == "__main__":
    sys.exit(main())
