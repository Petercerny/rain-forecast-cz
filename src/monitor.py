"""Check that fresh data keeps arriving. Exit code 1 means "something is wrong".

    python -m src.monitor

The GitHub workflow runs this in a separate job after every update. When it fails, the job
opens (or updates) a GitHub issue, and GitHub emails you about the failed run.
Needs DATABASE_URL.
"""
import sys
from datetime import datetime, timezone

from src.common import load_settings
from src.db import connect

EXPECTED_SOURCES = ("open-meteo", "met-no")


def evaluate(newest: dict, now: datetime, alert_after_hours: float) -> list[str]:
    """Return a list of problems (empty = healthy).

    `newest` maps source name -> time of its newest fetch (a source missing from the dict, or
    with None, has never delivered anything).
    """
    problems = []
    for source in EXPECTED_SOURCES:
        fetched_at = newest.get(source)
        if fetched_at is None:
            problems.append(f"{source}: no data in the database at all")
            continue
        age = (now - fetched_at).total_seconds() / 3600
        if age > alert_after_hours:
            problems.append(f"{source}: newest data is {age:.1f} hours old (limit {alert_after_hours} hours)")
    return problems


def main() -> int:
    hours = load_settings()["monitor"]["alert_after_hours"]
    now = datetime.now(timezone.utc)
    with connect() as conn:
        newest = dict(conn.execute("SELECT source, max(fetched_at) FROM rain.fetches GROUP BY source").fetchall())
    for source in EXPECTED_SOURCES:
        print(f"{source}: newest fetch {newest.get(source)}")
    problems = evaluate(newest, now, hours)
    for p in problems:
        print("PROBLEM:", p)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
