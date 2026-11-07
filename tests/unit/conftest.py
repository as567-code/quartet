from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

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


@pytest.fixture(scope="session")
def _span_exporter_session() -> InMemorySpanExporter:
    """The one global TracerProvider (settable once per process), registered once per session,
    before the first test that requests it."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return exporter


@pytest.fixture
def span_exporter(_span_exporter_session: InMemorySpanExporter) -> InMemorySpanExporter:
    _span_exporter_session.clear()
    return _span_exporter_session
