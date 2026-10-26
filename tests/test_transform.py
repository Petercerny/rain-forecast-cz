"""Run the real SQL file on a few hand-made forecasts and check the result tables.

These tests need a Postgres (TEST_DATABASE_URL), see tests/conftest.py.
"""
from datetime import timedelta

from helpers import NOW, add_fetch
from src.transform import run_sql


def query(conn, sql):
    return conn.execute(sql).fetchall()


def test_newest_fetch_wins_and_sources_are_joined(conn):
    old, new = NOW - timedelta(hours=3), NOW - timedelta(hours=1)
    add_fetch(conn, "open-meteo", old, "Praha", {0: 9.0, 1: 9.0})   # superseded
    add_fetch(conn, "open-meteo", new, "Praha", {0: 1.0, 1: 2.0, 2: 0.0})
    add_fetch(conn, "met-no", new, "Praha", {0: 0.5, 1: 2.5})        # no data for hour 2
    run_sql(conn, NOW)

    rows = query(conn, "SELECT om_mm, met_mm, diff_mm FROM hourly ORDER BY hour_start")
    assert rows == [(1.0, 0.5, 0.5), (2.0, 2.5, -0.5), (0.0, None, None)]


def test_window_is_48_hours_from_start_of_current_hour(conn):
    fetched = NOW - timedelta(hours=1)
    add_fetch(conn, "open-meteo", fetched, "Praha", {-1: 5.0, 0: 1.0, 47: 1.0, 48: 5.0})
    run_sql(conn, NOW)
    # hour -1 (already past) and hour 48 (outside the window) are excluded
    assert query(conn, "SELECT count(*), sum(om_mm) FROM hourly") == [(2, 2.0)]


def test_stale_source_is_ignored(conn):
    add_fetch(conn, "open-meteo", NOW - timedelta(hours=1), "Praha", {0: 1.0})
    add_fetch(conn, "met-no", NOW - timedelta(hours=20), "Praha", {0: 3.0})  # older than stale_after_hours
    run_sql(conn, NOW)
    assert query(conn, "SELECT om_mm, met_mm FROM hourly") == [(1.0, None)]
    assert query(conn, "SELECT met_fetched_at FROM location_totals") == [(None,)]


def test_daily_totals_use_prague_calendar_days(conn):
    # 21:00 UTC = 23:00 CEST (1 July); 22:00 UTC = 00:00 CEST (2 July)
    add_fetch(conn, "open-meteo", NOW - timedelta(hours=1), "Praha", {11: 1.0, 12: 2.0})
    run_sql(conn, NOW)
    days = query(conn, "SELECT local_date::text, om_mm FROM daily ORDER BY local_date")
    assert days == [("2026-07-01", 1.0), ("2026-07-02", 2.0)]


def test_peak_hours_top_three_by_wettest_source(conn):
    fetched = NOW - timedelta(hours=1)
    add_fetch(conn, "open-meteo", fetched, "Praha", {0: 1.0, 1: 0.2, 2: 0.0, 3: 4.0, 4: 0.0})
    add_fetch(conn, "met-no", fetched, "Praha", {0: 0.0, 1: 3.0, 2: 0.0, 3: 0.0, 4: 0.5})
    run_sql(conn, NOW)
    peaks = query(conn, "SELECT rank, peak_mm FROM peak_hours ORDER BY rank")
    assert peaks == [(1, 4.0), (2, 3.0), (3, 1.0)]  # dry hours and the 4th place are left out


def test_average_temperature_uses_both_sources_or_falls_back_to_one(conn):
    fetched = NOW - timedelta(hours=1)
    add_fetch(conn, "open-meteo", fetched, "Praha", {0: 0.0, 1: 0.0, 2: 0.0}, temps={0: 10.0, 1: 12.0, 2: 14.0})
    add_fetch(conn, "met-no", fetched, "Praha", {0: 0.0, 1: 0.0}, temps={0: 8.0, 1: 11.0})  # no hour 2
    run_sql(conn, NOW)
    rows = query(conn, "SELECT avg_temp_c FROM hourly ORDER BY hour_start")
    assert rows == [(9.0,), (11.5,), (14.0,)]  # average, average, Open-Meteo alone


def test_daily_temperature_range_and_highest_rain_chance(conn):
    fetched = NOW - timedelta(hours=1)
    add_fetch(conn, "open-meteo", fetched, "Praha", {0: 0.0, 1: 0.0, 2: 0.0},
              temps={0: 10.0, 1: 15.0, 2: 12.0}, probs={0: 20, 1: 70, 2: 40})
    add_fetch(conn, "met-no", fetched, "Praha", {0: 0.0, 1: 0.0, 2: 0.0}, temps={0: 9.0, 1: 13.0, 2: 11.0})
    run_sql(conn, NOW)
    row = query(conn, "SELECT om_tmin_c, om_tmax_c, met_tmin_c, met_tmax_c, avg_tmax_c, max_prob_pct FROM daily")
    assert row == [(10.0, 15.0, 9.0, 13.0, 14.0, 70)]
    assert query(conn, "SELECT temp_min_c, temp_max_c FROM location_totals") == [(9.5, 14.0)]


def test_two_locations_do_not_mix(conn):
    fetched = NOW - timedelta(hours=1)
    add_fetch(conn, "open-meteo", fetched, "Praha", {0: 1.0})
    add_fetch(conn, "open-meteo", fetched, "Brno", {0: 7.0})
    add_fetch(conn, "met-no", fetched, "Brno", {0: 6.0})
    run_sql(conn, NOW)
    assert query(conn, "SELECT location, om_total_mm, met_total_mm FROM location_totals ORDER BY location") == [
        ("Brno", 7.0, 6.0), ("Praha", 1.0, None)]


# ---- sky, light and wind (Open-Meteo only)

def test_hourly_carries_the_open_meteo_only_values_and_met_rows_have_none(conn):
    fetched = NOW - timedelta(hours=1)
    add_fetch(conn, "open-meteo", fetched, "Praha", {0: 0.0, 1: 0.0},
              extras={"cloud_pct": {0: 40, 1: 90}, "weather_code": {0: 2, 1: 61}, "wind_ms": {0: 3.0, 1: 5.5},
                      "is_day": {0: 1, 1: 1}})
    add_fetch(conn, "met-no", fetched, "Brno", {0: 0.0})                       # a MET-only place
    run_sql(conn, NOW)
    rows = query(conn, "SELECT om_cloud_pct, om_weather_code, om_wind_ms, om_is_day FROM hourly "
                       "WHERE location = 'Praha' ORDER BY hour_start")
    assert rows == [(40, 2, 3.0, 1), (90, 61, 5.5, 1)]
    assert query(conn, "SELECT om_cloud_pct, om_wind_ms FROM hourly WHERE location = 'Brno'") == [(None, None)]


def test_daily_summarises_sky_and_wind_and_picks_the_worst_weather(conn):
    fetched = NOW - timedelta(hours=1)
    add_fetch(conn, "open-meteo", fetched, "Praha", {0: 0.0, 1: 0.0, 2: 0.0},
              extras={"cloud_pct": {0: 20, 1: 60, 2: 100}, "sunshine_s": {0: 3600, 1: 1800, 2: 0},
                      "weather_code": {0: 1, 1: 3, 2: 71}, "wind_ms": {0: 2.0, 1: 6.5, 2: 4.0},
                      "gust_ms": {0: 5.0, 1: 12.5, 2: 9.0}, "humidity_pct": {0: 50, 1: 60, 2: 70},
                      "visibility_m": {0: 20000, 1: 800, 2: 5000}})
    run_sql(conn, NOW)
    row = query(conn, "SELECT cloud_avg_pct, sunshine_h, wind_max_ms, gust_max_ms, humidity_avg_pct, "
                      "visibility_min_m, worst_weather_code FROM daily")
    assert row == [(60, 1.5, 6.5, 12.5, 60, 800, 71)]      # snow (71) beats clear (1) and overcast (3)


def test_sun_times_come_from_the_newest_open_meteo_fetch(conn):
    from datetime import datetime, timezone
    old_sun = (datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc), datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc))
    new_sun = (datetime(2026, 7, 1, 3, 15, tzinfo=timezone.utc), datetime(2026, 7, 1, 19, 45, tzinfo=timezone.utc))
    add_fetch(conn, "open-meteo", NOW - timedelta(hours=3), "Praha", {0: 0.0}, sun=old_sun)
    add_fetch(conn, "open-meteo", NOW - timedelta(hours=1), "Praha", {0: 0.0}, sun=new_sun)
    add_fetch(conn, "open-meteo", NOW - timedelta(hours=1), "Brno", {0: 0.0})   # no sun data (old style fetch)
    run_sql(conn, NOW)
    rows = query(conn, "SELECT location, local_date::text, sunrise_local, sunset_local FROM sun_times")
    assert rows == [("Praha", "2026-07-01", "05:15", "21:45")]                  # UTC+2 in July
