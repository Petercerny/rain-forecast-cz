"""Database-related code that needs no database: packing, file parsing, monitor, role URL."""
from datetime import datetime, timedelta, timezone

import pytest

from helpers import H0, hourly_rows, write_parquet
from src.db import HOUR, pack_fetch, unpack_fetch
from src.db_migrate import pipeline_url
from src.load_db import fetches_from_file, list_files
from src.monitor import evaluate


# ---- pack_fetch / unpack_fetch

def test_pack_puts_values_by_hour_offset():
    p = pack_fetch(hourly_rows({0: 1.0, 1: 2.0, 2: 3.0}, temps={0: 5.0, 1: 6.0, 2: 7.0}, probs={1: 40}))
    assert p["first_hour"] == H0
    assert p["precip_mm"] == [1.0, 2.0, 3.0]
    assert p["temp_c"] == [5.0, 6.0, 7.0]
    assert p["precip_prob"] == [None, 40, None]


def test_pack_marks_missing_hours_as_none_and_ignores_input_order():
    rows = hourly_rows({3: 3.0, 0: 0.5})          # hours 1 and 2 are missing, rows are unsorted
    p = pack_fetch(rows)
    assert p["precip_mm"] == [0.5, None, None, 3.0]
    assert p["temp_c"] == [10.0, None, None, 10.0]


def test_pack_unpack_round_trip_drops_only_the_gaps():
    rows = hourly_rows({0: 0.5, 2: 1.5}, probs={0: 10, 2: 20})
    back = unpack_fetch(**pack_fetch(rows))
    assert [(r["hour_start"], r["precip_mm"], r["precip_prob"]) for r in back] == [
        (H0, 0.5, 10), (H0 + 2 * HOUR, 1.5, 20)]


def test_pack_rejects_a_half_hour_and_an_empty_fetch():
    with pytest.raises(ValueError):
        pack_fetch([{"hour_start": H0 + timedelta(minutes=30), "precip_mm": 0, "precip_prob": None, "temp_c": 1}])
    with pytest.raises(ValueError):
        pack_fetch([])


# ---- reading Parquet files for load_db

def test_list_files_and_fetches_from_file(tmp_path):
    fetched = datetime(2026, 7, 1, 9, 15, 30, tzinfo=timezone.utc)
    write_parquet(tmp_path, "open-meteo", fetched, "České Budějovice", {0: 1.0, 1: 2.0}, probs={0: 30, 1: 60})
    write_parquet(tmp_path, "met-no", fetched, "Praha", {0: 0.0})

    files = list_files(tmp_path)
    assert sorted((f["source"], f["fetched_at"]) for f in files) == [("met-no", fetched), ("open-meteo", fetched)]

    om = next(f for f in files if f["source"] == "open-meteo")
    fetches, locations = fetches_from_file(om["path"], "open-meteo")
    assert len(fetches) == 1 and fetches[0]["slug"] == "ceske-budejovice"
    assert fetches[0]["precip_mm"] == [1.0, 2.0] and fetches[0]["precip_prob"] == [30, 60]
    assert locations == [("ceske-budejovice", "České Budějovice", 50.0, 14.0)]


def test_list_files_can_skip_old_days(tmp_path):
    write_parquet(tmp_path, "open-meteo", datetime(2026, 6, 1, 9, tzinfo=timezone.utc), "Praha", {0: 1.0})
    write_parquet(tmp_path, "open-meteo", datetime(2026, 7, 1, 9, tzinfo=timezone.utc), "Praha", {0: 1.0})
    files = list_files(tmp_path, since_date=datetime(2026, 6, 20).date())
    assert [f["fetched_at"].month for f in files] == [7]


# ---- monitor

NOW = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)


def test_monitor_healthy_when_both_sources_are_recent():
    newest = {"open-meteo": NOW - timedelta(hours=1), "met-no": NOW - timedelta(hours=7)}
    assert evaluate(newest, NOW, alert_after_hours=8) == []


def test_monitor_reports_old_and_missing_sources():
    newest = {"open-meteo": NOW - timedelta(hours=9)}      # too old; met-no never delivered
    problems = evaluate(newest, NOW, alert_after_hours=8)
    assert len(problems) == 2
    assert "open-meteo" in problems[0] and "9.0 hours" in problems[0]
    assert "met-no" in problems[1]


# ---- pipeline role connection string

def test_pipeline_url_swaps_user_keeps_project_ref_and_encodes_password():
    admin = "postgresql://postgres.abcdef:adminpw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require"
    url = pipeline_url(admin, "p@ss/w")
    assert url == "postgresql://pipeline.abcdef:p%40ss%2Fw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require"


def test_pipeline_url_on_a_plain_postgres_server():
    assert pipeline_url("postgresql://postgres:x@localhost:5432/db", "pw") == "postgresql://pipeline:pw@localhost:5432/db"
