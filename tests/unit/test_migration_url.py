"""Alembic never migrates with the application URL (ruling R31a)."""

from pathlib import Path

import alembic.command
import alembic.config
import pytest

from quartet.config import Settings
from quartet.db.alembic_url import resolve_migration_url

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = "postgresql+psycopg://quartet_app:apppw@127.0.0.1:5432/quartet"
MIG = "postgresql+psycopg://quartet_migrator:migpw@127.0.0.1:5432/quartet"


def _settings(migrator: str | None) -> Settings:
    return Settings(
        QUARTET_DATABASE_URL=APP,
        QUARTET_MIGRATOR_DATABASE_URL=migrator,
    )


def _never() -> Settings:
    raise AssertionError("settings must not be loaded when an explicit URL is given")


def test_config_attribute_wins():
    assert resolve_migration_url("postgresql://a@h/a", "postgresql://x@h/x", _never) == (
        "postgresql://a@h/a"
    )


def test_x_argument_beats_settings():
    assert resolve_migration_url(None, "postgresql://x@h/x", _never) == "postgresql://x@h/x"


def test_settings_migrator_url_is_used():
    assert resolve_migration_url(None, None, lambda: _settings(MIG)) == MIG


def test_refuses_to_fall_back_to_the_app_url():
    with pytest.raises(RuntimeError, match="QUARTET_MIGRATOR_DATABASE_URL") as ei:
        resolve_migration_url(None, None, lambda: _settings(None))
    assert "apppw" not in str(ei.value) and "quartet_app" not in str(ei.value)


def test_env_py_refuses_to_run_with_only_the_app_url(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", APP)
    cfg = alembic.config.Config(str(REPO_ROOT / "alembic.ini"))
    with pytest.raises(RuntimeError, match="QUARTET_MIGRATOR_DATABASE_URL") as ei:
        alembic.command.upgrade(cfg, "head")
    assert "apppw" not in str(ei.value)


def test_env_py_uses_the_migrator_url_from_settings(monkeypatch):
    import sqlalchemy

    seen: list[str] = []

    class Stop(Exception):
        pass

    def fake_create_engine(url: str, **_: object) -> None:
        seen.append(url)
        raise Stop

    monkeypatch.setenv("QUARTET_DATABASE_URL", APP)
    monkeypatch.setenv("QUARTET_MIGRATOR_DATABASE_URL", MIG)
    monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)
    cfg = alembic.config.Config(str(REPO_ROOT / "alembic.ini"))
    with pytest.raises(Stop):
        alembic.command.upgrade(cfg, "head")
    assert seen == [MIG]
