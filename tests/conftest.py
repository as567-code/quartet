"""Shared fixtures: isolated, migrated Postgres databases for integration tests.

Each database is created by the admin role (``CREATE DATABASE ... OWNER quartet_migrator``),
migrated as the migrator, and dropped in a ``finally``. No URL containing a password is ever
printed, logged, or put in a dataclass ``repr``.
"""

import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import alembic.command
import alembic.config
import psycopg
import pytest
import sqlalchemy as sa
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from quartet.config import InvalidSettings, _resolve_env_file, libpq_url, load_settings
from quartet.db.session import make_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
_PREFIX = "quartet_test_"


@dataclass(frozen=True)
class MigratedDb:
    name: str
    migrator_url: str = field(repr=False)
    app_url: str = field(repr=False)


def alembic_config(url: str) -> alembic.config.Config:
    """Alembic config for ``url``; the URL travels via ``attributes`` and is never logged."""
    cfg = alembic.config.Config(str(REPO_ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    return cfg


def _secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        env_file = _resolve_env_file(None)
        if env_file is not None:
            value = dotenv_values(env_file).get(name)
    if not value:
        raise RuntimeError(f"{name} is not set in the environment or the selected env file")
    return value


def _role_url(admin_url: str, database: str, user: str, password: str) -> str:
    info = conninfo_to_dict(libpq_url(admin_url))
    port = info.get("port")
    return URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=str(info["host"]) if info.get("host") else None,
        port=int(port) if port else None,
        database=database,
    ).render_as_string(hide_password=False)


def _admin_connect(admin_url: str) -> psycopg.Connection:
    return psycopg.connect(libpq_url(admin_url), autocommit=True)


def create_migrated_database(admin_url: str) -> MigratedDb:
    name = f"{_PREFIX}{uuid.uuid4().hex[:12]}"
    db = MigratedDb(
        name=name,
        migrator_url=_role_url(
            admin_url, name, "quartet_migrator", _secret("QUARTET_MIGRATOR_PASSWORD")
        ),
        app_url=_role_url(admin_url, name, "quartet_app", _secret("QUARTET_APP_PASSWORD")),
    )
    with _admin_connect(admin_url) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER quartet_migrator").format(sql.Identifier(name))
        )
    try:
        alembic.command.upgrade(alembic_config(db.migrator_url), "head")
    except BaseException:
        drop_database(admin_url, name)
        raise
    return db


def drop_database(admin_url: str, name: str) -> None:
    with _admin_connect(admin_url) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def list_test_databases(admin_url: str) -> list[str]:
    with _admin_connect(admin_url) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s ORDER BY datname",
            (_PREFIX.replace("_", r"\_") + "%",),
        ).fetchall()
    return [r[0] for r in rows]


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get("QUARTET_ADMIN_DATABASE_URL")
    if not url:
        try:
            secret = load_settings().admin_database_url
        except InvalidSettings:
            secret = None
        url = secret.get_secret_value() if secret else None
    if not url:
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("integration DB not configured in CI")
        pytest.skip("QUARTET_ADMIN_DATABASE_URL is not available; skipping integration tests")
    return url


@pytest.fixture(name="alembic_config")
def _alembic_config_fixture() -> Callable[[str], alembic.config.Config]:
    return alembic_config


@pytest.fixture(scope="session")
def migrated_db(admin_url: str) -> Iterator[MigratedDb]:
    db = create_migrated_database(admin_url)
    try:
        yield db
    finally:
        drop_database(admin_url, db.name)


@pytest.fixture
def fresh_db(admin_url: str) -> Iterator[MigratedDb]:
    db = create_migrated_database(admin_url)
    try:
        yield db
    finally:
        drop_database(admin_url, db.name)


@pytest.fixture(autouse=True)
def clean_db(request: pytest.FixtureRequest) -> None:
    """Truncate every public table (except alembic_version) before each integration test."""
    if request.node.get_closest_marker("integration") is None:
        return
    if "fresh_db" in request.fixturenames:
        return
    db: MigratedDb = request.getfixturevalue("migrated_db")
    with psycopg.connect(libpq_url(db.migrator_url), autocommit=True) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        ]
        if tables:
            conn.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier("public", t) for t in tables)
                )
            )


@pytest.fixture(scope="session")
def app_url(migrated_db: MigratedDb) -> str:
    return migrated_db.app_url


@pytest.fixture(scope="session")
def app_engine(app_url: str) -> Iterator[sa.Engine]:
    engine = make_engine(SecretStr(app_url))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def app_session(app_engine: sa.Engine) -> Iterator[Session]:
    session = Session(app_engine)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---- data fixtures (insert through the app role, always COMMIT) ----


@pytest.fixture
def repo_id(app_engine: sa.Engine) -> uuid.UUID:
    from quartet.db.models import Repo

    with Session(app_engine) as session, session.begin():
        repo = Repo(owner="acme", name=f"widgets-{uuid.uuid4().hex[:8]}", mirror_path=None)
        session.add(repo)
        session.flush()
        return repo.id


@pytest.fixture
def make_tasks(app_engine: sa.Engine, repo_id: uuid.UUID) -> Callable[..., list[uuid.UUID]]:
    """Factory: insert ``n`` tasks in list order, one committed transaction per row.

    A transaction per row means ``now()`` (transaction-stable) differs between rows, so
    ``created_at`` is strictly increasing in list order.
    """
    from quartet.db.models import Task
    from quartet.domain.states import TaskState

    def _make(
        n: int = 1,
        priorities: list[int] | None = None,
        state: TaskState = TaskState.QUEUED,
    ) -> list[uuid.UUID]:
        if priorities is not None and len(priorities) != n:
            raise ValueError("priorities must have length n")
        ids: list[uuid.UUID] = []
        for i in range(n):
            with Session(app_engine) as session, session.begin():
                task = Task(
                    repo_id=repo_id,
                    source="manual",
                    title=f"task {i}",
                    state=state,
                    version=0,
                )
                if priorities is not None:
                    task.priority = priorities[i]
                session.add(task)
                session.flush()
                ids.append(task.id)
        return ids

    return _make


@pytest.fixture
def seeded_task(make_tasks: Callable[..., list[uuid.UUID]]) -> uuid.UUID:
    from quartet.domain.states import TaskState

    return make_tasks(1, None, TaskState.QUEUED)[0]
