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


def run_sql(conn, now: datetime | None = None):
    """Execute the SQL file on this connection. The result tables are TEMP tables of the session."""
    settings = load_settings()
    now = now or datetime.now(timezone.utc)
    # Settings are read inside the SQL with current_setting('rain.name').
    for name, value in (("now_utc", now.isoformat()), ("window_hours", settings["window_hours"]),
                        ("stale_after_hours", settings["stale_after_hours"])):
        conn.execute("SELECT set_config(%s, %s, false)", (f"rain.{name}", str(value)))
    conn.execute((SQL_DIR / "forecast_summary.sql").read_text(encoding="utf-8"))
    return conn


def build_forecast(con, now: datetime) -> dict:
    """Collect the SQL result tables into one JSON-friendly dict, grouped by location."""
    hourly = _rows(con, "hourly")
    daily = _rows(con, "daily")
    peaks = _rows(con, "peak_hours")
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
                     "diff_mm": r["diff_mm"]}
                    for r in sorted(hourly, key=lambda r: r["hour_start"]) if r["location"] == name
                ],
                "daily": [
                    {"date": r["local_date"].isoformat(), "hours": r["hours_in_window"],
                     "open_meteo_mm": r["om_mm"], "met_no_mm": r["met_mm"], "diff_mm": r["diff_mm"],
                     "om_tmin_c": r["om_tmin_c"], "om_tmax_c": r["om_tmax_c"],
                     "met_tmin_c": r["met_tmin_c"], "met_tmax_c": r["met_tmax_c"],
                     "avg_tmin_c": r["avg_tmin_c"], "avg_tmax_c": r["avg_tmax_c"],
                     "max_prob_pct": r["max_prob_pct"]}
                    for r in daily if r["location"] == name
                ],
                "peaks": [
                    {"t": _iso(r["hour_start"]), "local_date": r["local_date"].isoformat(),
                     "local_time": r["local_time"], "open_meteo_mm": r["om_mm"], "met_no_mm": r["met_mm"]}
                    for r in peaks if r["location"] == name
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
