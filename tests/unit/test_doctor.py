from pathlib import Path

import pytest

from quartet.config import load_settings
from quartet.doctor import redact_url, run_checks


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no developer .env is read
    for var in ("QUARTET_ADMIN_DATABASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_redact_url():
    assert (
        redact_url("postgresql+psycopg://u:hunter2@db:5432/q")
        == "postgresql+psycopg://u:***@db:5432/q"
    )


def test_redact_url_without_password_is_unchanged():
    assert redact_url("postgresql://db:5432/q") == "postgresql://db:5432/q"


def test_check_names_and_order(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/q")
    names = [c.name for c in run_checks(load_settings())]
    assert names == ["docker", "git", "postgres", "anthropic_key", "openai_key"]


def test_doctor_reports_unreachable_db(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:hunter2@127.0.0.1:1/q")
    pg = next(c for c in run_checks(load_settings()) if c.name == "postgres")
    assert pg.ok is False and "127.0.0.1:1" in pg.detail and "hunter2" not in pg.detail


def test_key_checks_never_include_values(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/q")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    checks = run_checks(load_settings())
    assert all("sk-test-xyz" not in c.detail for c in checks)
    assert next(c for c in checks if c.name == "openai_key").ok is True
    anthropic = next(c for c in checks if c.name == "anthropic_key")
    assert anthropic.ok is False and anthropic.detail == "missing"


def test_missing_tools_are_reported_not_installed(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/q")
    monkeypatch.setattr("quartet.doctor.shutil.which", lambda _name: None)
    checks = {c.name: c for c in run_checks(load_settings())}
    for name in ("docker", "git"):
        assert checks[name].ok is False and checks[name].detail == "not installed"
