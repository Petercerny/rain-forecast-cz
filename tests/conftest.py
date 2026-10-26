"""Shared test setup: a throwaway Postgres for the database tests."""
import os

import pytest

import src.common  # noqa: F401  (loads .env, so the safety checks below see DATABASE_URL)
from src.db import connect
from src.db_migrate import migrate


@pytest.fixture
def conn():
    """A connection to the TEST database with a freshly migrated, empty `rain` schema.

    Needs TEST_DATABASE_URL (CI provides a Postgres container; locally use a spare database, for
    example a second free Supabase project). The schema is dropped before AND after every test,
    so point this ONLY at a database that holds nothing you care about.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set, skipping database tests")
    for name in ("DATABASE_URL", "ADMIN_DATABASE_URL"):
        if url == os.environ.get(name):
            pytest.fail(f"TEST_DATABASE_URL must not be the same as {name}: the tests drop the rain schema")

    c = connect(url)
    exists = c.execute("SELECT to_regclass('rain.fetches')").fetchone()[0]
    if exists and c.execute("SELECT count(*) FROM rain.fetches").fetchone()[0] > 0:
        c.close()
        pytest.fail("The test database already has forecast data in rain.fetches. Refusing to drop it.")
    c.execute("DROP SCHEMA IF EXISTS rain CASCADE")
    migrate(c)
    yield c
    c.rollback()
    c.execute("DROP SCHEMA IF EXISTS rain CASCADE")
    c.commit()
    c.close()
