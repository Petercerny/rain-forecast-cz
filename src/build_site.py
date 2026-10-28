"""Assemble the static site in site/.

    web/index.html          the map homepage
    web/city.html           template, rendered once per location -> site/<slug>/index.html
    web/*.css, *.js         copied as they are

The numbers are NOT part of the build any more: the pages fetch them at runtime from /api/data (see
src/web_app.py), so this needs no database and runs on Vercel's build step. For a local preview without the
app, `--with-data` also writes site/data.json from build/forecast.json + summaries.json (made by
`python -m src.transform` and `python -m src.summarize`); the pages fall back to that file.

Placeholders in the HTML files: {{REPO_URL}}, and in city.html also {{CITY_NAME}}.

Usage:  python -m src.build_site [--with-data]
"""
import argparse
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from src.common import BUILD_DIR, SITE_DIR, WEB_DIR, load_locations, load_settings, slugify

PRAGUE = ZoneInfo("Europe/Prague")
DEFAULT_REPO_URL = "https://github.com/YOUR-USER/YOUR-REPO"
CITY_TEMPLATE = "city.html"


def _num(*values):
    """Largest of the values that are not None (None if there are none)."""
    present = [v for v in values if v is not None]
    return max(present) if present else None


def period_labels(today: date) -> list[dict]:
    """The tabs above the map: the current hour, then up to three calendar days (Prague time)."""
    labels = [{"key": "now", "label": "Now"}, {"key": "d0", "label": "Today"}, {"key": "d1", "label": "Tomorrow"}]
    labels.append({"key": "d2", "label": (today + timedelta(days=2)).strftime("%A")})
    return labels


def build_periods(loc: dict, today: date) -> dict:
    """What a map marker shows for each tab: rain chance, temperature, and rain amount.

    now      what is happening right now. When a weather station reported recently (loc["observed"]) this is
             the measurement: its temperature and the rain of the last hour, with the forecast's rain chance
             for the hour, and "source": "measured". Otherwise it is the first forecast hour, "source": "forecast".
    d0..d2   today, tomorrow, the day after: highest rain chance of that day, its highest
             temperature (of the two-source average), and the larger of the two rain totals.
    A value is None when no source has data for it.
    """
    periods = {}
    h = loc["hourly"][0] if loc["hourly"] else None
    obs = loc.get("observed")
    if obs and obs.get("temp_c") is not None:
        rain = obs.get("precip_1h_mm") if obs.get("precip_1h_mm") is not None else obs.get("precip_10m_mm")
        periods["now"] = {"prob": h["prob_pct"] if h else None, "temp": obs["temp_c"], "rain_mm": rain,
                          "source": "measured"}
    elif h:
        periods["now"] = {"prob": h["prob_pct"], "temp": h["avg_temp_c"], "rain_mm": _num(h["om_mm"], h["met_mm"]),
                          "source": "forecast"}
    by_date = {d["date"]: d for d in loc["daily"]}
    for i in range(3):
        d = by_date.get((today + timedelta(days=i)).isoformat())
        periods[f"d{i}"] = None if d is None else {
            "prob": d["max_prob_pct"],
            "temp": d["avg_tmax_c"],
            "rain_mm": _num(d["open_meteo_mm"], d["met_no_mm"]),
        }
    periods.setdefault("now", None)
    return periods


def build_observed(obs: dict | None) -> dict | None:
    """The `observed` object of data.json (None when no station reading is fresh enough)."""
    if not obs:
        return None
    return {"t": obs["t"], "station": obs["station"], "temp": obs["temp_c"], "hum": obs["humidity_pct"],
            "pressure": obs["pressure_hpa"], "wind": obs["wind_ms"], "gust": obs["gust_ms"],
            "dir": obs["wind_dir_deg"], "sun_s": obs["sunshine_s"], "rain_10m": obs["precip_10m_mm"],
            "rain_1h": obs["precip_1h_mm"], "rain_station": obs.get("rain_station")}


def build_data(forecast: dict, summaries: dict, carto_key: str = "") -> dict:
    """Merge forecast + summaries into the JSON file the pages load."""
    generated = datetime.fromisoformat(forecast["generated_at"].replace("Z", "+00:00"))
    today = generated.astimezone(PRAGUE).date()
    locations = []
    for loc in forecast["locations"]:
        s = summaries.get(loc["name"], {})
        locations.append(
            {
                "name": loc["name"],
                "slug": slugify(loc["name"]),
                "lat": loc["lat"],
                "lon": loc["lon"],
                "summary": {
                    "text": s.get("text", ""),
                    # The page shows an "AI-generated" label only when this is true.
                    "ai_generated": bool(s.get("ai_generated")),
                    "generated_by": s.get("generated_by", "template"),
                },
                "sources": loc["sources"],
                "totals": loc["totals"],
                "daily": loc["daily"],
                "peaks": loc["peaks"],
                "sun": loc.get("sun", []),
                # What the nearest weather station measured last (null = none recent). Short keys, like hourly.
                "observed": build_observed(loc.get("observed")),
                "periods": build_periods(loc, today),
                "hourly": [
                    {"t": h["t"], "om": h["om_mm"], "met": h["met_mm"], "prob": h["prob_pct"],
                     "om_temp": h["om_temp_c"], "met_temp": h["met_temp_c"], "temp": h["avg_temp_c"],
                     # Open-Meteo only, null when not available. Short keys keep data.json small.
                     "cloud": h.get("cloud_pct"), "cloud_l": h.get("cloud_low_pct"),
                     "cloud_m": h.get("cloud_mid_pct"), "cloud_h": h.get("cloud_high_pct"),
                     "day": h.get("is_day"), "sun_s": h.get("sunshine_s"), "code": h.get("weather_code"),
                     "vis": h.get("visibility_m"), "hum": h.get("humidity_pct"),
                     "wind": h.get("wind_ms"), "gust": h.get("gust_ms"), "dir": h.get("wind_dir_deg")}
                    for h in loc["hourly"]
                ],
            }
        )
    return {
        "generated_at": forecast["generated_at"],
        "generated_at_local": generated.astimezone(PRAGUE).strftime("%d %b %Y, %H:%M %Z"),
        "window_hours": forecast["window_hours"],
        "stale_banner_after_hours": load_settings()["stale_banner_after_hours"],
        # The CARTO basemap key is not secret (it ends up in the page), but it comes from
        # config so it is not hardcoded. Empty = the map falls back to OpenStreetMap tiles.
        "map": {"carto_key": carto_key},
        "periods": period_labels(today),
        "locations": locations,
    }


def render_site(data: dict, repo_url: str, web_dir=WEB_DIR, site_dir=SITE_DIR) -> None:
    """Write the whole site folder: static files, the map page, one page per city, and data.json if `data`
    carries the numbers (a bare {"locations": [{"slug", "name"}]} gives the shell only)."""
    if site_dir.exists():
        shutil.rmtree(site_dir)  # site/ is fully rebuilt every run
    site_dir.mkdir()

    for f in web_dir.iterdir():
        if f.name == CITY_TEMPLATE:
            continue
        text = f.read_text(encoding="utf-8").replace("{{REPO_URL}}", repo_url)
        (site_dir / f.name).write_text(text, encoding="utf-8")

    template = (web_dir / CITY_TEMPLATE).read_text(encoding="utf-8").replace("{{REPO_URL}}", repo_url)
    for loc in data["locations"]:
        page = site_dir / loc["slug"]
        page.mkdir()
        html = template.replace("{{CITY_NAME}}", loc["name"])
        (page / "index.html").write_text(html, encoding="utf-8")

    if "generated_at" in data:
        (site_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--with-data", action="store_true", help="also write site/data.json from build/*.json")
    args = parser.parse_args()
    repo_url = os.environ.get("REPO_URL", DEFAULT_REPO_URL).rstrip("/")

    if args.with_data:
        forecast = json.loads((BUILD_DIR / "forecast.json").read_text(encoding="utf-8"))
        summaries = json.loads((BUILD_DIR / "summaries.json").read_text(encoding="utf-8"))
        data = build_data(forecast, summaries, os.environ.get("CARTO_KEY", ""))
        render_site(data, repo_url)
        print(f"wrote {SITE_DIR} ({len(data['locations'])} locations, data from {data['generated_at_local']})")
    else:  # the shell only: pages for every place in config/locations.yaml, numbers come from /api/data
        shell = {"locations": [{"name": l["name"], "slug": slugify(l["name"])} for l in load_locations()]}
        render_site(shell, repo_url)
        print(f"wrote {SITE_DIR} (page shell for {len(shell['locations'])} locations, no data)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
