from pathlib import Path

import pytest
from typer.testing import CliRunner

from quartet.cli import app


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no developer .env is read
    for var in ("QUARTET_ADMIN_DATABASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_version_flag_needs_no_env(monkeypatch):
    monkeypatch.delenv("QUARTET_DATABASE_URL", raising=False)
    r = CliRunner().invoke(app, ["--version"])
    assert r.exit_code == 0 and r.stdout.strip() == "quartet 0.1.0"


def test_doctor_exit_code_and_no_secret_leak(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:hunter2@127.0.0.1:1/q")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leakcheck")
    r = CliRunner().invoke(app, ["doctor"])
    assert r.exit_code == 1
    assert "hunter2" not in r.output and "sk-ant-leakcheck" not in r.output
    assert "Traceback" not in r.output
    assert "postgres" in r.output


def test_doctor_without_database_url_fails_cleanly(monkeypatch):
    monkeypatch.delenv("QUARTET_DATABASE_URL", raising=False)
    r = CliRunner().invoke(app, ["doctor"])
    assert r.exit_code == 1
    assert "QUARTET_DATABASE_URL" in r.output and "Traceback" not in r.output
