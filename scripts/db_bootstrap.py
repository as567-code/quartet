"""Idempotently create the Quartet roles and database (used by CI)."""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg import sql

MIGRATOR = "quartet_migrator"
APP = "quartet_app"
DATABASE = "quartet"


def _ensure_role(cur: psycopg.Cursor, name: str, password: str) -> None:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    if cur.fetchone() is None:
        cur.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
    else:
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
    parser.add_argument("--admin-url", required=True, help="superuser libpq/SQLAlchemy-free URL")
    args = parser.parse_args(argv)
    try:
        migrator_pw = os.environ["QUARTET_MIGRATOR_PASSWORD"]
        app_pw = os.environ["QUARTET_APP_PASSWORD"]
    except KeyError as exc:
        print(f"missing environment variable: {exc.args[0]}", file=sys.stderr)
        return 2
    # psycopg wants a plain postgresql:// URL; strip any SQLAlchemy driver suffix.
    url = args.admin_url.replace("postgresql+psycopg://", "postgresql://", 1)
    bootstrap(url, migrator_pw, app_pw)
    print("bootstrap complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
