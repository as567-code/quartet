from pathlib import Path

import pytest

_CLEARED = (
    "QUARTET_DATABASE_URL",
    "QUARTET_ADMIN_DATABASE_URL",
    "QUARTET_ENV_FILE",
    "QUARTET_HOME",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No developer env vars, .env, or ~/.quartet/.env can change unit-test results."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for var in _CLEARED:
        monkeypatch.delenv(var, raising=False)
