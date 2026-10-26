"""Tests against a real Postgres (TEST_DATABASE_URL), see tests/conftest.py."""
import json
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from helpers import H0, NOW, add_fetch, write_parquet
from src.db_migrate import migrate
from src.load_db import list_files, load_files, record_run


def count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_migrations_are_applied_once(conn):
    assert migrate(conn) == []   # the fixture already applied them
    assert conn.execute("SELECT version FROM rain.schema_migrations").fetchall() == [("001_init.sql",)]


def test_forecast_view_unpacks_arrays_and_skips_gaps(conn):
    add_fetch(conn, "open-meteo", NOW, "Praha", {0: 1.0, 3: 2.0}, probs={0: 10, 3: 30})  # hours 1 and 2 missing
    rows = conn.execute(
        "SELECT hour_start, precip_mm, precip_prob FROM rain.forecast_hours ORDER BY hour_start").fetchall()
    assert rows == [(H0, 1.0, 10), (H0 + timedelta(hours=3), 2.0, 30)]


def test_the_same_fetch_cannot_be_stored_twice(conn):
    add_fetch(conn, "open-meteo", NOW, "Praha", {0: 1.0})
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_fetch(conn, "open-meteo", NOW, "Praha", {0: 1.0})


def test_arrays_of_different_length_are_rejected(conn):
    add_fetch(conn, "open-meteo", NOW, "Praha", {0: 1.0})   # creates the location
    conn.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO rain.fetches (source, location_slug, fetched_at, first_hour, precip_mm, precip_prob, temp_c) "
            "VALUES ('met-no', 'praha', %s, %s, '{1,2}', '{NULL}', '{5,6}')", (NOW, H0))


def test_load_db_is_idempotent_and_catches_up(tmp_path, conn):
    t1, t2 = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    write_parquet(tmp_path, "open-meteo", t1, "Praha", {0: 1.0, 1: 2.0})
    write_parquet(tmp_path, "met-no", t1, "Praha", {0: 0.5, 1: 1.5})

    first = load_files(conn, list_files(tmp_path))
    assert first == {"files": 2, "fetches": 2}
    assert load_files(conn, list_files(tmp_path)) == {"files": 0, "fetches": 0}   # nothing twice

    write_parquet(tmp_path, "open-meteo", t2, "Praha", {0: 3.0})                   # a new run appears
    assert load_files(conn, list_files(tmp_path)) == {"files": 1, "fetches": 1}
    assert count(conn, "rain.fetches") == 3
    assert conn.execute("SELECT slug, name FROM rain.locations").fetchall() == [("praha", "Praha")]


def test_restore_from_parquet_reproduces_the_forecast_rows(tmp_path, conn):
    write_parquet(tmp_path, "open-meteo", NOW, "Brno", {0: 1.0, 1: 2.0, 2: 0.0}, temps={0: 5.0, 1: 6.0, 2: 7.0})
    load_files(conn, list_files(tmp_path))
    rows = conn.execute("SELECT hour_start, precip_mm, temp_c FROM rain.forecast_hours ORDER BY hour_start").fetchall()
    assert rows == [(H0, 1.0, 5.0), (H0 + timedelta(hours=1), 2.0, 6.0), (H0 + timedelta(hours=2), 0.0, 7.0)]


def test_record_run_copies_the_ingest_result_into_the_run_log(tmp_path, conn):
    result = tmp_path / "ingest_result.json"
    result.write_text(json.dumps({
        "recorded_at": datetime(2026, 7, 1, 10, tzinfo=timezone.utc).isoformat(),
        "sources": {"open-meteo": {"rows": 936, "failures": 0, "file": "x"},
                    "met-no": {"rows": 700, "failures": 2, "file": None}},
    }), encoding="utf-8")
    assert record_run(conn, result) == 2
    rows = conn.execute("SELECT source, rows_loaded, failures FROM rain.ingest_runs ORDER BY source").fetchall()
    assert rows == [("met-no", 700, 2), ("open-meteo", 936, 0)]


def test_pipeline_role_can_read_but_not_change_the_structure_or_delete_history(conn):
    add_fetch(conn, "open-meteo", NOW, "Praha", {0: 1.0})
    conn.commit()
    try:
        conn.execute("SET ROLE pipeline")
    except psycopg.Error:
        conn.rollback()
        pytest.skip("this connection cannot switch to the pipeline role")
    assert count(conn, "rain.forecast_hours") == 1                      # may read (through the view)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):           # may not create tables
        conn.execute("CREATE TABLE rain.evil (x int)")
    conn.rollback()
    conn.execute("SET ROLE pipeline")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):           # may not delete history
        conn.execute("DELETE FROM rain.fetches")
    conn.rollback()
