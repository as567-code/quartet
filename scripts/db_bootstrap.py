"""Idempotently create the Quartet roles and database (used by CI)."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import psycopg
from psycopg import sql

from quartet.config import libpq_url, validate_db_url

MIGRATOR = "quartet_migrator"
APP = "quartet_app"
DATABASE = "quartet"


def _ensure_role(cur: psycopg.Cursor[Any], name: str, password: str) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    if cur.fetchone() is None:
        cur.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
    else:
        # Intentionally resets the password on every run (keeps CI and .env in sync).
        cur.execute(
            sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )


def bootstrap(admin_url: str, migrator_pw: str, app_pw: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn, conn.cursor() as cur:
        _ensure_role(cur, MIGRATOR, migrator_pw)
        _ensure_role(cur, APP, app_pw)
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DATABASE,))
        if cur.fetchone() is None:
            cur.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(DATABASE), sql.Identifier(MIGRATOR)
                )
            )
        else:
            cur.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(DATABASE), sql.Identifier(MIGRATOR)
                )
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--admin-url",
        default=None,
        help="superuser URL (default: $QUARTET_ADMIN_DATABASE_URL)",
    )
    args = parser.parse_args(argv)
    admin_url = args.admin_url or os.environ.get("QUARTET_ADMIN_DATABASE_URL")
    if not admin_url:
        print("provide --admin-url or set QUARTET_ADMIN_DATABASE_URL", file=sys.stderr)
        return 2
    try:
        migrator_pw = os.environ["QUARTET_MIGRATOR_PASSWORD"]
        app_pw = os.environ["QUARTET_APP_PASSWORD"]
    except KeyError as exc:
        print(f"missing environment variable: {exc.args[0]}", file=sys.stderr)
        return 2
    try:
        validate_db_url(admin_url)
    except ValueError as exc:
        print(f"invalid admin database URL: {exc}", file=sys.stderr)
        return 2
    bootstrap(libpq_url(admin_url), migrator_pw, app_pw)
    print("bootstrap complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
