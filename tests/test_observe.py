"""Tests for the station readings: parsing the ČHMÚ file, conditional downloads, and storing them.

The parsing tests need no database. `run` needs a Postgres (TEST_DATABASE_URL), see tests/conftest.py.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import observe
from src.observe import (configured_stations, days_to_fetch, fetch_file, parse_station_file, run)

FIXTURES = Path(__file__).parent / "fixtures"
WSI = "0-203-0-11514"
STATION_PAYLOAD = json.loads((FIXTURES / "chmi_station_sample.json").read_text(encoding="utf-8"))
META1 = json.loads((FIXTURES / "chmi_meta1_sample.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 26, 22, 10, tzinfo=timezone.utc)


def payload(*rows):
    """A minimal ČHMÚ station file: rows are (element, dt, value, quality)."""
    return {"data": {"data": {"header": "STATION,ELEMENT,DT,VAL,FLAG,QUALITY",
                              "values": [[WSI, el, dt, val, "", q] for el, dt, val, q in rows]}}}


# ---------------------------------------------------------------- parsing

def test_long_rows_are_pivoted_by_time_and_use_the_database_columns():
    rows = parse_station_file(STATION_PAYLOAD)
    assert [r["observed_at"].strftime("%H:%M") for r in rows] == ["21:30", "21:40", "21:50"]   # oldest first
    assert rows[-1] == {
        "station_wsi": WSI, "observed_at": datetime(2026, 10, 26, 21, 50, tzinfo=timezone.utc),
        "temp_c": 7.5, "humidity_pct": 72.0, "pressure_hpa": 998.8, "wind_ms": 0.8, "gust_ms": 2.7,
        "wind_dir_deg": 190, "precip_10m_mm": 0.0, "sunshine_s": None,   # night: sunshine has no value
    }


def test_empty_values_and_unusable_quality_are_dropped_and_other_elements_ignored():
    rows = parse_station_file(payload(
        ("T", "2026-10-26T10:00:00Z", 8.1, 5.0),
        ("H", "2026-10-26T10:00:00Z", "", 4.0),          # no value
        ("F", "2026-10-26T10:00:00Z", 3.0, 2.0),         # quality 2 = poor, do not use yet
        ("SRA10M", "2026-10-26T10:00:00Z", 0.2, 0.0),    # good
        ("TMA", "2026-10-26T10:00:00Z", 9.9, 5.0),       # not an element we store
        ("T", "2026-10-26T10:10:00Z", "", 4.0),          # nothing usable at 10:10 -> no row at all
    ))
    assert len(rows) == 1
    assert (rows[0]["temp_c"], rows[0]["humidity_pct"], rows[0]["wind_ms"], rows[0]["precip_10m_mm"]) == (8.1, None, None, 0.2)


def test_wind_direction_is_a_whole_number_of_degrees():
    assert parse_station_file(payload(("D", "2026-10-26T10:00:00Z", 207.0, 5.0)))[0]["wind_dir_deg"] == 207


# ---------------------------------------------------------------- which files

def test_yesterday_is_read_only_in_the_first_hours_of_the_utc_day():
    assert [d.isoformat() for d in days_to_fetch(datetime(2026, 10, 27, 0, 10, tzinfo=timezone.utc))] == ["2026-10-26", "2026-10-27"]
    assert [d.isoformat() for d in days_to_fetch(NOW)] == ["2026-10-26"]


def test_configured_stations_lists_each_station_once_main_and_rain():
    locations = [{"name": "A", "station": "s1"}, {"name": "B", "station": "s2", "rain_station": "s3"},
                 {"name": "C", "station": "s1"}, {"name": "D"}]
    assert configured_stations(locations) == ["s1", "s2", "s3"]


# ---------------------------------------------------------------- conditional download

class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """Answers by URL; remembers the headers each request carried."""

    def __init__(self, responses):
        self.responses, self.requests = responses, []

    def get(self, url, headers=None, timeout=None, **kw):
        self.requests.append((url, dict(headers or {})))
        return self.responses[url]   # an unexpected URL is a test bug: KeyError


def test_fetch_file_sends_the_stored_validators_and_understands_304_and_404():
    url = "https://example.test/f.json"
    http = FakeHttp({url: Response(304)})
    assert fetch_file(http, url, {"last_modified": "Mon, 26 Oct 2026 22:01:04 GMT", "etag": '"abc"'}) == {"status": "not_modified"}
    assert http.requests[0][1] == {"If-Modified-Since": "Mon, 26 Oct 2026 22:01:04 GMT", "If-None-Match": '"abc"'}
    assert fetch_file(FakeHttp({url: Response(404)}), url, None) == {"status": "missing"}
    ok = fetch_file(FakeHttp({url: Response(200, {"x": 1}, {"Last-Modified": "lm", "ETag": "e"})}), url, None)
    assert ok == {"status": "ok", "payload": {"x": 1}, "last_modified": "lm", "etag": "e"}
    with pytest.raises(RuntimeError):
        fetch_file(FakeHttp({url: Response(500)}), url, None)


# ---------------------------------------------------------------- the whole run (needs the test database)

BRNO = "0-203-0-11721"
LOCATIONS = [{"name": "Praha", "station": WSI}, {"name": "Brno", "station": BRNO}]


def station_url(wsi, day="20261026"):
    return observe.DATA_URL.format(wsi=wsi, day=datetime.strptime(day, "%Y%m%d"))


def file_of(wsi, payload=STATION_PAYLOAD):
    """The sample station file, as if it came from another station."""
    copy = json.loads(json.dumps(payload))
    for row in copy["data"]["data"]["values"]:
        row[0] = wsi
    return copy


def http_for(brno=None):
    """Praha gives the sample file; Brno gives whatever `brno` is. meta1 serves the station names."""
    return FakeHttp({
        station_url(WSI): Response(200, STATION_PAYLOAD, {"Last-Modified": "lm1"}),
        station_url(BRNO): brno or Response(200, file_of(BRNO), {"Last-Modified": "lm2"}),
        "https://opendata.chmi.cz/meteorology/climate/now/metadata/meta1-20261026.json": Response(200, META1),
    })


def test_run_stores_readings_and_names_the_stations(conn):
    result = run(conn, NOW, LOCATIONS, http_for())
    assert (result["files"], result["rows"], result["failures"], result["newest"]) == (2, 6, 0, "2026-10-26T21:50:00Z")
    assert conn.execute("SELECT name FROM rain.stations WHERE wsi = %s", (WSI,)).fetchone()[0] == "Praha, Klementinum"
    row = conn.execute("SELECT temp_c, wind_dir_deg FROM rain.observations WHERE station_wsi = %s "
                       "ORDER BY observed_at DESC LIMIT 1", (WSI,)).fetchone()
    assert row == (pytest.approx(7.5), 190)


def test_second_run_asks_with_if_modified_since_and_stores_nothing_new(conn):
    run(conn, NOW, LOCATIONS, http_for())
    http = http_for(brno=Response(304))
    http.responses[station_url(WSI)] = Response(304)
    result = run(conn, NOW, LOCATIONS, http)
    assert (result["files"], result["not_modified"], result["rows"]) == (0, 2, 0)
    sent = dict(http.requests)
    assert sent[station_url(WSI)] == {"If-Modified-Since": "lm1"}
    assert sent[station_url(BRNO)] == {"If-Modified-Since": "lm2"}
    assert conn.execute("SELECT count(*) FROM rain.observations").fetchone()[0] == 6


def test_one_failing_station_does_not_stop_the_others(conn):
    result = run(conn, NOW, LOCATIONS, http_for(brno=Response(500)))
    assert (result["files"], result["failures"]) == (1, 1)
    assert "11721" in result["errors"][0]
    assert conn.execute("SELECT count(DISTINCT station_wsi) FROM rain.observations").fetchone()[0] == 1


def test_a_corrected_value_replaces_the_stored_one(conn):
    run(conn, NOW, LOCATIONS[:1], http_for())
    changed = json.loads(json.dumps(STATION_PAYLOAD))
    for r in changed["data"]["data"]["values"]:
        if r[1] == "T" and r[2].endswith("21:50:00Z"):
            r[3] = 9.9
    http = http_for()
    http.responses[station_url(WSI)] = Response(200, changed, {"Last-Modified": "lm3"})
    run(conn, NOW, LOCATIONS[:1], http)
    assert conn.execute("SELECT temp_c FROM rain.observations WHERE observed_at = '2026-10-26T21:50:00Z'").fetchone()[0] == pytest.approx(9.9)
