"""Run the real SQL file on a few hand-made snapshots and check the result tables."""
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from src.ingest import SCHEMA
from src.transform import run_sql

NOW = datetime(2026, 7, 1, 10, 30, tzinfo=timezone.utc)  # summer: Prague = UTC+2
H0 = datetime(2026, 7, 1, 10, tzinfo=timezone.utc)       # start of the current hour = window start


def write(tmp_path, source, fetched_at, location, hours, temps=None, probs=None):
    """hours = {hour offset from H0: precip_mm}. Writes one snapshot file.

    temps / probs = optional {hour offset: value}; temperature defaults to 10.0, probability to NULL.
    """
    temps, probs = temps or {}, probs or {}
    rows = [
        {"fetched_at": fetched_at, "location": location, "lat": 50.0, "lon": 14.0,
         "valid_time": H0 + timedelta(hours=h), "hour_start": H0 + timedelta(hours=h),
         "precip_mm": mm, "precip_prob": probs.get(h), "temp_c": temps.get(h, 10.0), "source_updated_at": None}
        for h, mm in hours.items()
    ]
    folder = tmp_path / f"source={source}" / f"date={fetched_at:%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), folder / f"part-{fetched_at:%H%M%S}.parquet")


def query(con, sql):
    return con.execute(sql).fetchall()


def test_newest_snapshot_wins_and_sources_are_joined(tmp_path):
    old, new = NOW - timedelta(hours=3), NOW - timedelta(hours=1)
    write(tmp_path, "open-meteo", old, "Praha", {0: 9.0, 1: 9.0})   # superseded
    write(tmp_path, "open-meteo", new, "Praha", {0: 1.0, 1: 2.0, 2: 0.0})
    write(tmp_path, "met-no", new, "Praha", {0: 0.5, 1: 2.5})        # no data for hour 2
    con = run_sql(tmp_path, NOW)

    rows = query(con, "SELECT hour_start, om_mm, met_mm, diff_mm FROM hourly ORDER BY hour_start")
    assert [(r[1], r[2], r[3]) for r in rows] == [(1.0, 0.5, 0.5), (2.0, 2.5, -0.5), (0.0, None, None)]


def test_window_is_48_hours_from_start_of_current_hour(tmp_path):
    fetched = NOW - timedelta(hours=1)
    write(tmp_path, "open-meteo", fetched, "Praha", {-1: 5.0, 0: 1.0, 47: 1.0, 48: 5.0})
    con = run_sql(tmp_path, NOW)
    # hour -1 (already past) and hour 48 (outside the window) are excluded
    assert query(con, "SELECT count(*), sum(om_mm) FROM hourly") == [(2, 2.0)]


def test_stale_source_is_ignored(tmp_path):
    write(tmp_path, "open-meteo", NOW - timedelta(hours=1), "Praha", {0: 1.0})
    write(tmp_path, "met-no", NOW - timedelta(hours=20), "Praha", {0: 3.0})  # older than stale_after_hours
    con = run_sql(tmp_path, NOW)
    assert query(con, "SELECT om_mm, met_mm FROM hourly") == [(1.0, None)]
    assert query(con, "SELECT met_fetched_at FROM location_totals") == [(None,)]


def test_daily_totals_use_prague_calendar_days(tmp_path):
    # 21:00 UTC = 23:00 CEST (1 July); 22:00 UTC = 00:00 CEST (2 July)
    write(tmp_path, "open-meteo", NOW - timedelta(hours=1), "Praha", {11: 1.0, 12: 2.0})
    con = run_sql(tmp_path, NOW)
    days = query(con, "SELECT CAST(local_date AS VARCHAR), om_mm FROM daily ORDER BY local_date")
    assert days == [("2026-07-01", 1.0), ("2026-07-02", 2.0)]


def test_peak_hours_top_three_by_wettest_source(tmp_path):
    fetched = NOW - timedelta(hours=1)
    write(tmp_path, "open-meteo", fetched, "Praha", {0: 1.0, 1: 0.2, 2: 0.0, 3: 4.0, 4: 0.0})
    write(tmp_path, "met-no", fetched, "Praha", {0: 0.0, 1: 3.0, 2: 0.0, 3: 0.0, 4: 0.5})
    con = run_sql(tmp_path, NOW)
    peaks = query(con, "SELECT rank, peak_mm FROM peak_hours ORDER BY rank")
    assert peaks == [(1, 4.0), (2, 3.0), (3, 1.0)]  # dry hours and the 4th place are left out


def test_average_temperature_uses_both_sources_or_falls_back_to_one(tmp_path):
    fetched = NOW - timedelta(hours=1)
    write(tmp_path, "open-meteo", fetched, "Praha", {0: 0.0, 1: 0.0, 2: 0.0}, temps={0: 10.0, 1: 12.0, 2: 14.0})
    write(tmp_path, "met-no", fetched, "Praha", {0: 0.0, 1: 0.0}, temps={0: 8.0, 1: 11.0})  # no hour 2
    con = run_sql(tmp_path, NOW)
    rows = query(con, "SELECT avg_temp_c FROM hourly ORDER BY hour_start")
    assert rows == [(9.0,), (11.5,), (14.0,)]  # average, average, Open-Meteo alone


def test_daily_temperature_range_and_highest_rain_chance(tmp_path):
    fetched = NOW - timedelta(hours=1)
    write(tmp_path, "open-meteo", fetched, "Praha", {0: 0.0, 1: 0.0, 2: 0.0},
          temps={0: 10.0, 1: 15.0, 2: 12.0}, probs={0: 20, 1: 70, 2: 40})
    write(tmp_path, "met-no", fetched, "Praha", {0: 0.0, 1: 0.0, 2: 0.0}, temps={0: 9.0, 1: 13.0, 2: 11.0})
    con = run_sql(tmp_path, NOW)
    row = query(con, "SELECT om_tmin_c, om_tmax_c, met_tmin_c, met_tmax_c, avg_tmax_c, max_prob_pct FROM daily")
    assert row == [(10.0, 15.0, 9.0, 13.0, 14.0, 70)]
    assert query(con, "SELECT temp_min_c, temp_max_c FROM location_totals") == [(9.5, 14.0)]
