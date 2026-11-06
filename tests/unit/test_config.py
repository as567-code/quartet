from pathlib import Path

import pytest

from quartet.config import load_settings


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no developer .env is read
    for var in (
        "QUARTET_DATABASE_URL",
        "QUARTET_ADMIN_DATABASE_URL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "QUARTET_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


def test_secrets_are_hidden_in_repr(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:hunter2@localhost:5432/q")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-abc")
    s = load_settings()
    assert "hunter2" not in repr(s) and "sk-ant-test-abc" not in repr(s)
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-test-abc"


def test_defaults(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/q")
    s = load_settings()
    assert s.admin_database_url is None
    assert s.anthropic_api_key is None and s.openai_api_key is None
    assert s.otlp_endpoint == "http://127.0.0.1:4318"
    assert s.home == Path("~/.quartet").expanduser()
