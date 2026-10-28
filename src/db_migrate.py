"""Create or update the database structure. Run this yourself, locally, with the ADMIN connection.

    python -m src.db_migrate                        # apply db/migrations/*.sql that are not applied yet
    python -m src.db_migrate --create-pipeline-role # give the `pipeline` role a login and print its
                                                    # connection string ONCE (paste it into the GitHub
                                                    # secret DATABASE_URL, do not save it anywhere else)
    python -m src.db_migrate --create-archiver-role # the same for the `archiver` role of the daily R2 archive job
                                                    # (GitHub secret ARCHIVE_DATABASE_URL)

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


def role_url(admin_url: str, role: str, password: str) -> str:
    """The admin connection string with the user and password swapped for another role.

    On the Supabase pooler the user is "<role>.<project-ref>", so "postgres.abcd" -> "pipeline.abcd".
    """
    parts = urlsplit(admin_url)
    admin_user = parts.username or "postgres"
    suffix = admin_user.partition(".")[2]
    user = role + ("." + suffix if suffix else "")
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, f"{user}:{quote(password, safe='')}@{host}", parts.path, parts.query, ""))


def pipeline_url(admin_url: str, password: str) -> str:
    return role_url(admin_url, "pipeline", password)


def create_login_role(conn, admin_url: str, role: str) -> str:
    """Give a role (made NOLOGIN by a migration) a random login password. Returns its connection string."""
    password = secrets.token_urlsafe(24)
    conn.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    conn.commit()
    return role_url(admin_url, role, password)


def create_pipeline_role(conn, admin_url: str) -> str:
    return create_login_role(conn, admin_url, "pipeline")


def create_archiver_role(conn, admin_url: str) -> str:
    return create_login_role(conn, admin_url, "archiver")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--create-pipeline-role", action="store_true")
    parser.add_argument("--create-archiver-role", action="store_true")
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
        if args.create_archiver_role:
            url = create_archiver_role(conn, admin_url)
            print("\nNew password set for role `archiver` (it can read and DELETE forecast data).")
            print("Add this as the GitHub secret ARCHIVE_DATABASE_URL. It is shown only now and is not stored anywhere:\n")
            print(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
