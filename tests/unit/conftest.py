from pathlib import Path

import pytest

_CLEARED = (
    "QUARTET_DATABASE_URL",
    "QUARTET_ADMIN_DATABASE_URL",
    "QUARTET_MIGRATOR_DATABASE_URL",
    "QUARTET_ENV_FILE",
    "QUARTET_HOME",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_OPEN_TELEMETRY",
    "ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE",
    "ANTHROPIC_OPEN_TELEMETRY_PROPAGATION",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
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
