"""Parsers are tested against small saved API responses (tests/fixtures)."""
import json
from datetime import datetime, timezone
from pathlib import Path

from src.parsers import met_updated_at, parse_met_no, parse_open_meteo

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_open_meteo_values_and_missing_hour_skipped():
    rows = parse_open_meteo(load("open_meteo_sample.json"))
    # 6 hours in the file, one has precipitation = null -> dropped
    assert len(rows) == 5
    assert [r["precip_mm"] for r in rows] == [0.0, 0.2, 0.0, 1.4, 0.0]
    assert [r["precip_prob"] for r in rows] == [0, 40, 0, 80, 0]
    assert rows[1]["temp_c"] == 5.1


def test_open_meteo_hour_start_is_the_preceding_hour():
    # Open-Meteo labels a value with the END of its hour, so 01:00 means 00:00-01:00.
    row = parse_open_meteo(load("open_meteo_sample.json"))[1]
    assert row["valid_time"] == utc(2026, 10, 25, 1)
    assert row["hour_start"] == utc(2026, 10, 25, 0)


def test_met_no_values():
    rows = parse_met_no(load("met_no_sample.json"))
    assert [r["precip_mm"] for r in rows] == [0.0, 0.5, 1.2, 0.0]
    assert rows[0]["temp_c"] == 10.5
    assert all(r["precip_prob"] is None for r in rows)


def test_met_no_skips_steps_without_hourly_amount():
    # The fixture has 6 timeseries entries; the last two only have next_6_hours.
    assert len(load("met_no_sample.json")["properties"]["timeseries"]) == 6
    assert len(parse_met_no(load("met_no_sample.json"))) == 4


def test_met_no_hour_start_is_the_following_hour():
    # MET labels a value with the START of its hour, so 18:00 means 18:00-19:00.
    row = parse_met_no(load("met_no_sample.json"))[1]
    assert row["valid_time"] == row["hour_start"] == utc(2026, 10, 25, 18)


def test_met_updated_at():
    assert met_updated_at(load("met_no_sample.json")) == utc(2026, 10, 25, 17, 17, 20)
    assert met_updated_at({}) is None


# ---- the sky, light and wind values (Open-Meteo only)

def test_open_meteo_sky_light_and_wind_values():
    rows = parse_open_meteo(load("open_meteo_sample.json"))     # hours 0, 1, 2, 4, 5 (hour 3 had no rain value)
    r = rows[1]                                                  # valid 01:00
    assert (r["cloud_pct"], r["cloud_low_pct"], r["cloud_mid_pct"], r["cloud_high_pct"]) == (90, 80, 50, 10)
    assert (r["is_day"], r["sunshine_s"], r["weather_code"]) == (0, 0, 3)
    assert (r["visibility_m"], r["humidity_pct"]) == (30000, 88)
    assert (r["wind_ms"], r["gust_ms"], r["wind_dir_deg"]) == (3.0, 7.5, 255)
    last = rows[-1]                                              # valid 05:00 in the fixture: day, some sunshine
    assert (last["is_day"], last["sunshine_s"]) == (1, 612)
    assert last["visibility_m"] == 40240


def test_open_meteo_rounds_float_seconds_and_metres_to_whole_numbers():
    payload = load("open_meteo_sample.json")
    payload["hourly"]["sunshine_duration"][0] = 612.6
    payload["hourly"]["visibility"][0] = 12000.5
    row = parse_open_meteo(payload)[0]
    assert (row["sunshine_s"], row["visibility_m"]) == (613, 12000)


def test_open_meteo_attaches_each_utc_days_sunrise_and_sunset():
    rows = parse_open_meteo(load("open_meteo_sample.json"))
    assert {(r["sunrise"], r["sunset"]) for r in rows} == {(utc(2026, 10, 25, 5, 40), utc(2026, 10, 25, 15, 51))}


def test_open_meteo_without_the_new_variables_still_parses():
    # An older saved response (or a request with fewer variables) gives None for the new values.
    payload = load("open_meteo_sample.json")
    for name in ("cloud_cover", "wind_speed_10m", "weather_code", "is_day"):
        del payload["hourly"][name]
    del payload["daily"]
    row = parse_open_meteo(payload)[0]
    assert row["cloud_pct"] is None and row["wind_ms"] is None and row["weather_code"] is None
    assert row["sunrise"] is None and row["sunset"] is None
    assert row["precip_mm"] == 0.0 and row["temp_c"] == 5.0     # the old values are untouched


def test_met_no_rows_have_no_sky_values():
    # MET Norway is only used for rain and temperature, so those keys are simply absent.
    row = parse_met_no(load("met_no_sample.json"))[0]
    assert "cloud_pct" not in row and "wind_ms" not in row
