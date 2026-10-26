"""Create or update the database structure. Run this yourself, locally, with the ADMIN connection.

    python -m src.db_migrate                        # apply db/migrations/*.sql that are not applied yet
    python -m src.db_migrate --create-pipeline-role # give the `pipeline` role a login and print its
                                                    # connection string ONCE (paste it into the GitHub
                                                    # secret DATABASE_URL, do not save it anywhere else)

Uses ADMIN_DATABASE_URL (the "postgres" user). CI never runs this: its `pipeline` role cannot
change the table structure.
"""
import argparse
import os
import secrets
import sys
from urllib.parse import quote, urlsplit, urlunsplit

from psycopg import sql

from src.common import ROOT
from src.db import connect

MIGRATIONS_DIR = ROOT / "db" / "migrations"


def migrate(conn) -> list[str]:
    """Apply every migration file that has not been applied yet. Returns the versions applied."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS rain")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rain.schema_migrations "
        "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    done = {r[0] for r in conn.execute("SELECT version FROM rain.schema_migrations")}
    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():  # a migration is all-or-nothing
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO rain.schema_migrations (version) VALUES (%s)", (path.name,))
        applied.append(path.name)
    conn.commit()
    return applied


def pipeline_url(admin_url: str, password: str) -> str:
    """The admin connection string with the user and password swapped for the pipeline role.

    On the Supabase pooler the user is "<role>.<project-ref>", so "postgres.abcd" -> "pipeline.abcd".
    """
    parts = urlsplit(admin_url)
    admin_user = parts.username or "postgres"
    suffix = admin_user.partition(".")[2]
    user = "pipeline" + ("." + suffix if suffix else "")
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, f"{user}:{quote(password, safe='')}@{host}", parts.path, parts.query, ""))


def create_pipeline_role(conn, admin_url: str) -> str:
    password = secrets.token_urlsafe(24)
    conn.execute(sql.SQL("ALTER ROLE pipeline WITH LOGIN PASSWORD {}").format(sql.Literal(password)))
    conn.commit()
    return pipeline_url(admin_url, password)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--create-pipeline-role", action="store_true")
    args = parser.parse_args()

    admin_url = os.environ.get("ADMIN_DATABASE_URL")
    if not admin_url:
        print("ADMIN_DATABASE_URL is not set (see .env.example).", file=sys.stderr)
        return 1
    with connect(admin_url) as conn:
        applied = migrate(conn)
        print("applied:", ", ".join(applied) if applied else "nothing (already up to date)")
        if args.create_pipeline_role:
            url = create_pipeline_role(conn, admin_url)
            print("\nNew password set for role `pipeline`. Add this as the GitHub secret DATABASE_URL.")
            print("It is shown only now and is not stored anywhere:\n")
            print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
