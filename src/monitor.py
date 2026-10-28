"""Check that fresh data keeps arriving. Exit code 1 means "something is wrong".

    python -m src.monitor

The web app serves the same check at /api/health (HTTP 503 when something is wrong), which an
uptime monitor can poll and e-mail you about. Needs DATABASE_URL.
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


def check(conn, now: datetime | None = None) -> dict:
    """{"ok", "problems", "newest"}: the freshness check against the database behind `conn`."""
    now = now or datetime.now(timezone.utc)
    newest = dict(conn.execute("SELECT source, max(fetched_at) FROM rain.fetches GROUP BY source").fetchall())
    problems = evaluate(newest, now, load_settings()["monitor"]["alert_after_hours"])
    return {"ok": not problems, "problems": problems,
            "newest": {s: newest[s].isoformat() if newest.get(s) else None for s in EXPECTED_SOURCES}}


def main() -> int:
    with connect() as conn:
        result = check(conn)
    for source, when in result["newest"].items():
        print(f"{source}: newest fetch {when}")
    problems = result["problems"]
    for p in problems:
        print("PROBLEM:", p)
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
