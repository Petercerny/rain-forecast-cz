"""Tests for the map data (periods), city slugs and the files build_site writes."""
import json
from datetime import date

from src.build_site import build_data, build_periods, render_site
from src.common import WEB_DIR, slugify


def test_slugify_removes_diacritics_and_spaces():
    assert slugify("České Budějovice") == "ceske-budejovice"
    assert slugify("Plzeň") == "plzen"
    assert slugify("Ústí nad Labem") == "usti-nad-labem"
    assert slugify("Praha") == "praha"


def hour(t, om, met, prob, temp):
    return {"t": t, "om_mm": om, "met_mm": met, "prob_pct": prob, "om_temp_c": temp, "met_temp_c": temp,
            "avg_temp_c": temp, "diff_mm": None}


def day(d, om, met, prob, tmax):
    return {"date": d, "hours": 24, "open_meteo_mm": om, "met_no_mm": met, "diff_mm": None,
            "om_tmin_c": 1.0, "om_tmax_c": tmax, "met_tmin_c": 1.0, "met_tmax_c": tmax,
            "avg_tmin_c": 1.0, "avg_tmax_c": tmax, "max_prob_pct": prob}


LOC = {
    "name": "České Budějovice", "lat": 48.9744, "lon": 14.4743, "has_data": True,
    "totals": {"hours": 48},
    "sources": {"open-meteo": "2026-10-26T08:00:00Z", "met-no": "2026-10-26T08:00:00Z"},
    "hourly": [hour("2026-10-26T08:00:00Z", 0.2, 0.0, 35, 7.5)],
    "daily": [day("2026-10-26", 0.2, 0.0, 35, 11.0), day("2026-10-27", 3.0, 4.5, 90, 9.0)],
    "peaks": [],
}
FORECAST = {"generated_at": "2026-10-26T08:30:00Z", "window_hours": 48, "locations": [LOC]}
SUMMARIES = {"České Budějovice": {"text": "Some text.", "ai_generated": False, "generated_by": "template"}}


def test_periods_for_map_markers():
    p = build_periods(LOC, date(2026, 10, 26))
    assert p["now"] == {"prob": 35, "temp": 7.5, "rain_mm": 0.2}
    assert p["d0"] == {"prob": 35, "temp": 11.0, "rain_mm": 0.2}
    assert p["d1"] == {"prob": 90, "temp": 9.0, "rain_mm": 4.5}   # larger of the two rain totals
    assert p["d2"] is None                                        # no data for that day


def test_data_json_has_slug_periods_and_tab_labels():
    data = build_data(FORECAST, SUMMARIES, carto_key="abc")
    loc = data["locations"][0]
    assert loc["slug"] == "ceske-budejovice"
    assert loc["periods"]["d1"]["prob"] == 90
    assert loc["hourly"][0]["temp"] == 7.5
    assert [p["label"] for p in data["periods"]][:3] == ["Now", "Today", "Tomorrow"]
    assert data["map"]["carto_key"] == "abc"
    assert data["generated_at_local"].endswith("CET")  # 26 Oct 2026 is after the clocks went back


def test_render_site_writes_map_page_city_pages_and_data(tmp_path):
    data = build_data(FORECAST, SUMMARIES)
    render_site(data, "https://github.com/example/repo", web_dir=WEB_DIR, site_dir=tmp_path / "site")
    site = tmp_path / "site"

    assert (site / "index.html").exists() and (site / "map.js").exists()
    assert not (site / "city.html").exists()                       # the template is not published
    page = (site / "ceske-budejovice" / "index.html").read_text(encoding="utf-8")
    assert "České Budějovice" in page and "{{" not in page         # all placeholders filled in
    assert "https://github.com/example/repo" in page
    assert "{{" not in (site / "index.html").read_text(encoding="utf-8")
    assert json.loads((site / "data.json").read_text(encoding="utf-8"))["locations"][0]["name"] == "České Budějovice"


def test_data_json_carries_the_open_meteo_sky_values_and_sun_times():
    loc = {**LOC, "sun": [{"date": "2026-10-26", "sunrise": "2026-10-26T04:42:00Z", "sunset": "2026-10-26T14:49:00Z",
                           "sunrise_local": "05:42", "sunset_local": "15:49"}]}
    loc["hourly"] = [{**hour("2026-10-26T08:00:00Z", 0.2, 0.0, 35, 7.5), "cloud_pct": 80, "cloud_low_pct": 60,
                      "cloud_mid_pct": 40, "cloud_high_pct": 10, "is_day": 1, "sunshine_s": 900, "weather_code": 3,
                      "visibility_m": 20000, "humidity_pct": 70, "wind_ms": 3.5, "gust_ms": 8.0, "wind_dir_deg": 250}]
    data = build_data({**FORECAST, "locations": [loc]}, SUMMARIES)
    h = data["locations"][0]["hourly"][0]
    assert (h["cloud"], h["cloud_l"], h["cloud_m"], h["cloud_h"]) == (80, 60, 40, 10)
    assert (h["day"], h["sun_s"], h["code"], h["vis"], h["hum"]) == (1, 900, 3, 20000, 70)
    assert (h["wind"], h["gust"], h["dir"]) == (3.5, 8.0, 250)
    assert data["locations"][0]["sun"][0]["sunrise_local"] == "05:42"


def test_data_json_still_works_for_a_forecast_without_the_new_fields():
    # Forecast files from before the sky values existed have none of the new keys.
    h = build_data(FORECAST, SUMMARIES)["locations"][0]["hourly"][0]
    assert h["cloud"] is None and h["wind"] is None and h["code"] is None
    assert build_data(FORECAST, SUMMARIES)["locations"][0]["sun"] == []
