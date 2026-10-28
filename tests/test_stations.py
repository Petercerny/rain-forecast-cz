"""Tests for the ČHMÚ station list and the nearest-station suggestion (no network)."""
import json
from pathlib import Path

from src.stations import distance_km, format_suggestions, parse_meta1, parse_meta2, suggest

FIXTURES = Path(__file__).parent / "fixtures"
META1 = json.loads((FIXTURES / "chmi_meta1_sample.json").read_text(encoding="utf-8"))
META2 = json.loads((FIXTURES / "chmi_meta2_sample.json").read_text(encoding="utf-8"))
PRAHA = {"name": "Praha", "lat": 50.0880, "lon": 14.4208}


def test_meta1_gives_positions_with_longitude_first_and_drops_foreign_stations():
    stations = {s["wsi"]: s for s in parse_meta1(META1)}
    assert "0-20000-0-04030" not in stations                      # Reykjavik
    s = stations["0-203-0-11514"]
    assert s["name"] == "Praha, Klementinum"
    assert 50.0 < s["lat"] < 50.2 and 14.3 < s["lon"] < 14.5      # GEOGR1 is the longitude, GEOGR2 the latitude


def test_meta2_lists_the_ten_minute_elements_per_station():
    assert parse_meta2(META2)["0-203-0-11514"] == {"T", "F", "H", "SRA10M"}


def test_distance_between_prague_and_brno():
    assert 183 < distance_km(50.088, 14.4208, 49.1952, 16.608) < 187


def test_nearest_station_needs_temperature_and_wind_and_a_rain_gauge_is_added_when_missing():
    stations = parse_meta1(META1)
    elements = parse_meta2(META2)
    elements["0-203-0-11514"] = {"T", "F", "H"}                   # the nearest station has no rain gauge
    elements["0-20000-0-11518"] = {"T", "H", "SRA10M"}            # Ruzyně: no wind, so no main station
    [row] = suggest([PRAHA], stations, elements)
    assert row["main"]["wsi"] == "0-203-0-11514" and row["main"]["km"] < 2
    assert row["rain"]["wsi"] == "0-20000-0-11518"                # nearest station WITH a gauge


def test_no_rain_suggestion_when_the_main_station_has_a_gauge():
    [row] = suggest([PRAHA], parse_meta1(META1), parse_meta2(META2))
    assert row["main"]["wsi"] == "0-203-0-11514" and row["rain"] is None
    assert "station: 0-203-0-11514" in format_suggestions([row])
