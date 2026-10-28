"""Step 2: run sql/forecast_summary.sql in the Postgres database and write build/forecast.json.

The SQL does the real work (joining the sources, daily totals, peak hours). This file only
feeds it parameters and turns the resulting tables into a JSON file for the next steps.
Needs DATABASE_URL (see .env.example).

Usage:  python -m src.transform
"""
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal

from psycopg.rows import dict_row

from src.common import BUILD_DIR, SQL_DIR, load_locations, load_settings
from src.db import connect


def _iso(ts) -> str | None:
    return None if ts is None else ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(conn, table: str) -> list[dict]:
    """Read a whole (temp) table as a list of dicts (column name -> value)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT * FROM {table}")
        # Postgres returns exact decimals for numeric columns; the JSON files want plain floats.
        return [{k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()} for row in cur]


def run_sql(conn, now: datetime | None = None, locations: list[dict] | None = None):
    """Execute the SQL file on this connection. The result tables are TEMP tables of the session.

    `locations` (default: config/locations.yaml) tells the SQL which weather station belongs to which city.
    """
    settings = load_settings()
    now = now or datetime.now(timezone.utc)
    # Settings are read inside the SQL with current_setting('rain.name').
    for name, value in (("now_utc", now.isoformat()), ("window_hours", settings["window_hours"]),
                        ("stale_after_hours", settings["stale_after_hours"]),
                        ("observation_stale_minutes", settings.get("observation_stale_minutes", 120))):
        conn.execute("SELECT set_config(%s, %s, false)", (f"rain.{name}", str(value)))
    # location -> station, from the config (the database does not know which station serves which city).
    conn.execute("DROP TABLE IF EXISTS location_stations")
    conn.execute("CREATE TEMP TABLE location_stations (location text PRIMARY KEY, station_wsi text, rain_station_wsi text)")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO location_stations VALUES (%s, %s, %s)",
            [(l["name"], l["station"], l.get("rain_station"))
             for l in (locations if locations is not None else load_locations()) if l.get("station")])
    conn.execute((SQL_DIR / "forecast_summary.sql").read_text(encoding="utf-8"))
    return conn


def _observed(r: dict | None) -> dict | None:
    """One latest_obs row -> the `observed` object of a location (None = no fresh measurement)."""
    if r is None:
        return None
    return {
        "t": _iso(r["observed_at"]), "station": r["station_name"], "station_wsi": r["station_wsi"],
        "temp_c": r["temp_c"], "humidity_pct": r["humidity_pct"], "pressure_hpa": r["pressure_hpa"],
        "wind_ms": r["wind_ms"], "gust_ms": r["gust_ms"], "wind_dir_deg": r["wind_dir_deg"],
        "sunshine_s": r["sunshine_s"], "precip_10m_mm": r["precip_10m_mm"], "precip_1h_mm": r["precip_1h_mm"],
        "rain_t": _iso(r["rain_observed_at"]), "rain_station": r["rain_station_name"],
    }


def build_forecast(con, now: datetime) -> dict:
    """Collect the SQL result tables into one JSON-friendly dict, grouped by location."""
    hourly = _rows(con, "hourly")
    daily = _rows(con, "daily")
    peaks = _rows(con, "peak_hours")
    sun_times = _rows(con, "sun_times")
    observed = {r["location"]: r for r in _rows(con, "latest_obs")}
    totals = {r["location"]: r for r in _rows(con, "location_totals")}

    locations = []
    for loc in load_locations():  # config order = site order
        name = loc["name"]
        t = totals.get(name)
        locations.append(
            {
                "name": name,
                "lat": loc["lat"],
                "lon": loc["lon"],
                "has_data": t is not None,
                "observed": _observed(observed.get(name)),   # what the weather station measured last
                "totals": None if t is None else {
                    "hours": t["hours"],
                    "open_meteo_mm": t["om_total_mm"],
                    "met_no_mm": t["met_total_mm"],
                    "diff_mm": t["diff_total_mm"],
                    "max_prob_pct": t["max_prob_pct"],
                    "temp_min_c": t["temp_min_c"],
                    "temp_max_c": t["temp_max_c"],
                },
                "sources": {  # when each source was last fetched; null = no usable data
                    "open-meteo": None if t is None else _iso(t["om_fetched_at"]),
                    "met-no": None if t is None else _iso(t["met_fetched_at"]),
                },
                "hourly": [
                    {"t": _iso(r["hour_start"]), "local_date": r["local_date"].isoformat(),
                     "local_time": r["local_time"], "om_mm": r["om_mm"], "met_mm": r["met_mm"],
                     "prob_pct": r["om_prob_pct"], "om_temp_c": r["om_temp_c"],
                     "met_temp_c": r["met_temp_c"], "avg_temp_c": r["avg_temp_c"],
                     "diff_mm": r["diff_mm"],
                     # Open-Meteo only: sky, light and wind (None when not available)
                     "cloud_pct": r["om_cloud_pct"], "cloud_low_pct": r["om_cloud_low_pct"],
                     "cloud_mid_pct": r["om_cloud_mid_pct"], "cloud_high_pct": r["om_cloud_high_pct"],
                     "is_day": r["om_is_day"], "sunshine_s": r["om_sunshine_s"],
                     "weather_code": r["om_weather_code"], "visibility_m": r["om_visibility_m"],
                     "humidity_pct": r["om_humidity_pct"], "wind_ms": r["om_wind_ms"],
                     "gust_ms": r["om_gust_ms"], "wind_dir_deg": r["om_wind_dir_deg"]}
                    for r in sorted(hourly, key=lambda r: r["hour_start"]) if r["location"] == name
                ],
                "daily": [
                    {"date": r["local_date"].isoformat(), "hours": r["hours_in_window"],
                     "open_meteo_mm": r["om_mm"], "met_no_mm": r["met_mm"], "diff_mm": r["diff_mm"],
                     "om_tmin_c": r["om_tmin_c"], "om_tmax_c": r["om_tmax_c"],
                     "met_tmin_c": r["met_tmin_c"], "met_tmax_c": r["met_tmax_c"],
                     "avg_tmin_c": r["avg_tmin_c"], "avg_tmax_c": r["avg_tmax_c"],
                     "max_prob_pct": r["max_prob_pct"],
                     "cloud_avg_pct": r["cloud_avg_pct"], "sunshine_h": r["sunshine_h"],
                     "wind_max_ms": r["wind_max_ms"], "gust_max_ms": r["gust_max_ms"],
                     "humidity_avg_pct": r["humidity_avg_pct"], "visibility_min_m": r["visibility_min_m"],
                     "worst_weather_code": r["worst_weather_code"]}
                    for r in daily if r["location"] == name
                ],
                "peaks": [
                    {"t": _iso(r["hour_start"]), "local_date": r["local_date"].isoformat(),
                     "local_time": r["local_time"], "open_meteo_mm": r["om_mm"], "met_no_mm": r["met_mm"]}
                    for r in peaks if r["location"] == name
                ],
                "sun": [  # sunrise / sunset per Prague day (Open-Meteo)
                    {"date": r["local_date"].isoformat(), "sunrise": _iso(r["sunrise"]), "sunset": _iso(r["sunset"]),
                     "sunrise_local": r["sunrise_local"], "sunset_local": r["sunset_local"]}
                    for r in sun_times if r["location"] == name
                ],
            }
        )
    return {
        "generated_at": _iso(now),
        "window_hours": load_settings()["window_hours"],
        "locations": locations,
    }


def main() -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with connect() as conn:
        run_sql(conn, now)
        forecast = build_forecast(conn, now)
    BUILD_DIR.mkdir(exist_ok=True)
    out = BUILD_DIR / "forecast.json"
    out.write_text(json.dumps(forecast, ensure_ascii=False, indent=1), encoding="utf-8")
    for loc in forecast["locations"]:
        t = loc["totals"] or {}
        print(f"{loc['name']:<14} {len(loc['hourly']):>3} h   open-meteo {t.get('open_meteo_mm')} mm   "
              f"met-no {t.get('met_no_mm')} mm")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
