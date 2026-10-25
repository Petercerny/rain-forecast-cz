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
