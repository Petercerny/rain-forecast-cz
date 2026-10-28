"""The latest_obs table of sql/forecast_summary.sql: which station reading counts as "now".

These tests need a Postgres (TEST_DATABASE_URL), see tests/conftest.py.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.transform import run_sql

NOW = datetime(2026, 10, 26, 22, 10, tzinfo=timezone.utc)


def station(conn, wsi, name):
    conn.execute("INSERT INTO rain.stations (wsi, name, lat, lon, elevation) VALUES (%s, %s, 50, 14, 200)", (wsi, name))


def reading(conn, wsi, minutes_ago, **values):
    cols = ["station_wsi", "observed_at", *values]
    conn.execute(f"INSERT INTO rain.observations ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                 (wsi, NOW - timedelta(minutes=minutes_ago), *values.values()))


def latest(conn, locations):
    run_sql(conn, NOW, locations)
    rows = conn.execute("SELECT location, station_name, observed_at, temp_c, wind_ms, precip_10m_mm, precip_1h_mm, "
                        "rain_station_name FROM latest_obs ORDER BY location").fetchall()
    # numeric columns come back as Decimal, which does not compare equal to a float like 0.8
    return [tuple(float(v) if isinstance(v, Decimal) else v for v in row) for row in rows]


def test_newest_reading_with_a_temperature_wins(conn):
    station(conn, "s1", "Praha, Klementinum")
    reading(conn, "s1", 80, temp_c=5.0, wind_ms=1.0)
    reading(conn, "s1", 30, temp_c=6.0, wind_ms=2.0, precip_10m_mm=0.0)
    reading(conn, "s1", 20, wind_ms=9.0)                                   # newest, but no temperature: skipped
    [row] = latest(conn, [{"name": "Praha", "station": "s1"}])
    assert row[:6] == ("Praha", "Praha, Klementinum", NOW - timedelta(minutes=30), 6.0, 2.0, 0.0)


def test_a_reading_older_than_the_stale_limit_is_ignored(conn):
    station(conn, "s1", "Praha, Klementinum")
    reading(conn, "s1", 125, temp_c=5.0)                                   # limit is 120 minutes
    assert latest(conn, [{"name": "Praha", "station": "s1"}]) == []


def test_readings_from_the_future_and_unconfigured_cities_are_ignored(conn):
    station(conn, "s1", "A")
    reading(conn, "s1", -30, temp_c=99.0)
    reading(conn, "s1", 10, temp_c=4.0)
    assert latest(conn, [{"name": "Praha", "station": "s1"}, {"name": "Brno"}])[0][3] == 4.0
    assert len(latest(conn, [{"name": "Praha", "station": "s1"}, {"name": "Brno"}])) == 1


def test_rain_of_the_last_hour_is_the_sum_of_six_ten_minute_readings(conn):
    station(conn, "s1", "A")
    for i, mm in enumerate([0.1, 0.2, 0.0, 0.3, 0.1, 0.1, 5.0]):          # 10 .. 70 minutes ago
        reading(conn, "s1", 10 + 10 * i, temp_c=5.0, precip_10m_mm=mm)
    [row] = latest(conn, [{"name": "Praha", "station": "s1"}])
    assert (row[5], row[6]) == (0.1, 0.8)                                   # the 70-minute-old 5.0 is outside the hour


def test_a_separate_rain_station_supplies_the_rain(conn):
    station(conn, "s1", "Main")
    station(conn, "s2", "Gauge")
    reading(conn, "s1", 10, temp_c=5.0, precip_10m_mm=0.0)
    reading(conn, "s2", 20, precip_10m_mm=1.5)
    reading(conn, "s2", 30, precip_10m_mm=0.5)
    [row] = latest(conn, [{"name": "Praha", "station": "s1", "rain_station": "s2"}])
    assert (row[5], row[6], row[7]) == (1.5, 2.0, "Gauge")
