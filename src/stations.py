"""ČHMÚ weather stations: read the station lists and suggest the nearest station for each city.

    python -m src.stations --suggest      # print the nearest suitable station for every city

Facts about the ČHMÚ station metadata (checked against Klimatologicka_data_popis.pdf and the live
files on 2026-10-26):

  * https://opendata.chmi.cz/meteorology/climate/now/metadata/meta1-<YYYYMMDD>.json  (~75 kB)
    header WSI, GH_ID, FULL_NAME, GEOGR1, GEOGR2, ELEVATION, BEGIN_DATE. GEOGR1 is the LONGITUDE and
    GEOGR2 the LATITUDE (decimal degrees); ELEVATION is in metres. The list also contains a few
    foreign stations (Reykjavik) and many stations that do not report on the 10-minute feed.
  * .../meta2-<YYYYMMDD>.json (~450 kB) lists, per station, which elements it sends. We use it to
    keep only stations with 10-minute temperature (T) and wind (F); rain gauges are the ones with
    10-minute SRA10M. Units: T deg C, H %, P hPa, F and Fmax m/s, D degrees, SRA10M mm, SSV10M seconds.
  * The files are rewritten every night, so only today's (or yesterday's) is requested. Never read
    the directory listings of the /data/ folder: they hold thousands of files.

The suggestion is printed for you to review. Copy what you accept into config/locations.yaml as
`station:` (and `rain_station:` when the nearest rain gauge is a different station).
"""
import argparse
import math
import sys
from datetime import datetime, timedelta, timezone

import requests

from src.common import load_locations

META_BASE = "https://opendata.chmi.cz/meteorology/climate/now/metadata"
# A station must send these on the 10-minute feed to be a candidate for the main "now" readings.
MAIN_ELEMENTS = ("T", "F")
RAIN_ELEMENT = "SRA10M"
# Only suggest stations in or next to Czechia (meta1 also lists e.g. Reykjavik).
CZ_BOUNDS = (47.5, 51.5, 11.5, 19.5)  # lat min, lat max, lon min, lon max
FAR_WARNING_KM = 25


def _table(payload: dict) -> list[dict]:
    """ČHMÚ JSON ({"data": {"data": {"header": "A,B", "values": [[..]]}}}) -> list of dicts."""
    block = payload["data"]["data"]
    header = block["header"].split(",")
    return [dict(zip(header, row)) for row in block["values"]]


def parse_meta1(payload: dict) -> list[dict]:
    """meta1 -> [{wsi, name, lat, lon, elevation}] for stations inside CZ_BOUNDS."""
    lat0, lat1, lon0, lon1 = CZ_BOUNDS
    out = []
    for r in _table(payload):
        lat, lon = r.get("GEOGR2"), r.get("GEOGR1")
        if lat is None or lon is None or not (lat0 <= lat <= lat1 and lon0 <= lon <= lon1):
            continue
        out.append({"wsi": r["WSI"], "name": r["FULL_NAME"], "lat": lat, "lon": lon, "elevation": r.get("ELEVATION")})
    return out


def parse_meta2(payload: dict) -> dict[str, set]:
    """meta2 -> {wsi: {10-minute elements the station sends}}."""
    out: dict[str, set] = {}
    for r in _table(payload):
        if r.get("OBS_TYPE") == "10M":
            out.setdefault(r["WSI"], set()).add(r["EG_EL_ABBREVIATION"])
    return out


def distance_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance (haversine), kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _nearest(loc: dict, stations: list[dict]):
    best = min(stations, key=lambda s: distance_km(loc["lat"], loc["lon"], s["lat"], s["lon"]), default=None)
    if best is None:
        return None
    return {**best, "km": round(distance_km(loc["lat"], loc["lon"], best["lat"], best["lon"]), 1)}


def suggest(locations: list[dict], stations: list[dict], elements: dict[str, set]) -> list[dict]:
    """Nearest station with temperature + wind per location, and the nearest rain gauge.

    `rain` is None when the main station already has a rain gauge (or no gauge exists at all).
    """
    mains = [s for s in stations if set(MAIN_ELEMENTS) <= elements.get(s["wsi"], set())]
    gauges = [s for s in stations if RAIN_ELEMENT in elements.get(s["wsi"], set())]
    out = []
    for loc in locations:
        main = _nearest(loc, mains)
        rain = None
        if main and RAIN_ELEMENT not in elements.get(main["wsi"], set()):
            rain = _nearest(loc, gauges)
        out.append({"location": loc["name"], "main": main, "rain": rain})
    return out


def fetch_json(name: str, day: datetime | None = None, session=None) -> dict:
    """Download metaN for today (UTC), or yesterday when today's file is not there yet."""
    http = session or requests
    day = day or datetime.now(timezone.utc)
    for d in (day, day - timedelta(days=1)):
        resp = http.get(f"{META_BASE}/{name}-{d:%Y%m%d}.json", timeout=30)
        if resp.status_code != 404:
            resp.raise_for_status()
            return resp.json()
    raise RuntimeError(f"{name} not found on opendata.chmi.cz for {day:%Y-%m-%d}")


def format_suggestions(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        main, rain = r["main"], r["rain"]
        lines.append(f"# {r['location']}")
        if main is None:
            lines.append("#   no station found")
            continue
        far = f"   <-- {main['km']} km away, check it" if main["km"] > FAR_WARNING_KM else ""
        lines.append(f"#   station: {main['wsi']}  {main['name']}  ({main['km']} km, {main['elevation']} m){far}")
        if rain:
            lines.append(f"#   rain_station: {rain['wsi']}  {rain['name']}  ({rain['km']} km)")
    lines.append("")
    lines.append("# YAML to paste into config/locations.yaml, under each city:")
    for r in rows:
        if r["main"]:
            lines.append(f"{r['location']}:  station: {r['main']['wsi']}" + (f"  rain_station: {r['rain']['wsi']}" if r["rain"] else ""))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--suggest", action="store_true", help="print the nearest station for every city")
    args = parser.parse_args()
    if not args.suggest:
        parser.print_help()
        return 0
    stations = parse_meta1(fetch_json("meta1"))
    elements = parse_meta2(fetch_json("meta2"))
    print(format_suggestions(suggest(load_locations(), stations, elements)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
