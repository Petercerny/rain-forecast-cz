"""Measured values in the summaries and in the data the site loads (no database needed)."""
from datetime import date

from src import summarize as sm
from src.build_site import build_data, build_periods

OBS = {"t": "2026-10-26T21:50:00Z", "station": "Praha, Klementinum", "station_wsi": "0-203-0-11514",
       "temp_c": 7.5, "humidity_pct": 72.0, "pressure_hpa": 998.8, "wind_ms": 0.8, "gust_ms": 2.7,
       "wind_dir_deg": 190, "sunshine_s": None, "precip_10m_mm": 0.0, "precip_1h_mm": 0.4,
       "rain_t": "2026-10-26T21:50:00Z", "rain_station": None}


def hour(t, om, met, prob, temp):
    return {"t": t, "om_mm": om, "met_mm": met, "prob_pct": prob, "om_temp_c": temp, "met_temp_c": temp,
            "avg_temp_c": temp, "diff_mm": None}


def loc(observed):
    return {
        "name": "Praha", "lat": 50.088, "lon": 14.4208, "has_data": True, "observed": observed,
        "totals": {"hours": 48, "open_meteo_mm": 3.2, "met_no_mm": 2.0, "diff_mm": 1.2, "max_prob_pct": 70,
                   "temp_min_c": -2.5, "temp_max_c": 9.0},
        "sources": {"open-meteo": "2026-10-26T21:00:00Z", "met-no": "2026-10-26T21:00:00Z"},
        "hourly": [hour("2026-10-26T21:00:00Z", 0.2, 0.0, 35, 11.0)],
        "daily": [{"date": "2026-10-26", "hours": 24, "open_meteo_mm": 2.0, "met_no_mm": 1.5, "diff_mm": 0.5,
                   "avg_tmax_c": 12.0, "max_prob_pct": 70}],
        "peaks": [{"t": "2026-10-26T22:00:00Z", "local_date": "2026-10-26", "local_time": "23:00",
                   "open_meteo_mm": 1.2, "met_no_mm": 0.8}],
    }


TODAY = date(2026, 10, 26)


# ---- map periods

def test_now_uses_the_measurement_when_there_is_one():
    now = build_periods(loc(OBS), TODAY)["now"]
    assert now == {"prob": 35, "temp": 7.5, "rain_mm": 0.4, "source": "measured"}   # rain of the last hour


def test_now_falls_back_to_the_forecast_hour_without_a_measurement():
    now = build_periods(loc(None), TODAY)["now"]
    assert now == {"prob": 35, "temp": 11.0, "rain_mm": 0.2, "source": "forecast"}


def test_measured_now_works_even_without_any_forecast_hour():
    bare = loc(OBS)
    bare["hourly"] = []
    assert build_periods(bare, TODAY)["now"] == {"prob": None, "temp": 7.5, "rain_mm": 0.4, "source": "measured"}


def test_rain_of_the_last_hour_can_be_missing_then_the_ten_minute_value_is_used():
    obs = {**OBS, "precip_1h_mm": None, "precip_10m_mm": 0.1}
    assert build_periods(loc(obs), TODAY)["now"]["rain_mm"] == 0.1


def test_data_json_carries_observed_with_short_keys_and_null_without_a_station():
    forecast = {"generated_at": "2026-10-26T22:00:00Z", "window_hours": 48, "locations": [loc(OBS), {**loc(None), "name": "Brno"}]}
    praha, brno = build_data(forecast, {})["locations"]
    assert praha["observed"] == {"t": "2026-10-26T21:50:00Z", "station": "Praha, Klementinum", "temp": 7.5, "hum": 72.0,
                                 "pressure": 998.8, "wind": 0.8, "gust": 2.7, "dir": 190, "sun_s": None,
                                 "rain_10m": 0.0, "rain_1h": 0.4, "rain_station": None}
    assert brno["observed"] is None


# ---- summaries

def test_facts_contain_the_measurement_with_prague_local_time():
    facts = sm.build_facts(loc(OBS), TODAY)
    assert facts["measured"] == {"station": "Praha, Klementinum", "time": "22:50",   # 21:50 UTC = 22:50 CET
                                 "temperature_c": 7.5, "wind_ms": 0.8, "humidity_pct": 72.0, "rain_last_hour_mm": 0.4}


def test_template_summary_starts_with_the_measurement_and_stays_faithful():
    facts = sm.build_facts(loc(OBS), TODAY)
    text = sm.template_summary(facts)
    assert text.startswith("Measured at Praha, Klementinum at 22:50: 7.5 °C, wind 0.8 m/s, 0.4 mm of rain in the last hour.")
    assert sm.check_faithfulness(text, facts) == []


def test_summary_is_unchanged_without_a_measurement():
    facts = sm.build_facts(loc(None), TODAY)
    assert "measured" not in facts
    assert "Measured" not in sm.template_summary(facts)
