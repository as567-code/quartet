from collections.abc import Callable
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from anthropic import DefaultAsyncHttpxClient
from pydantic import SecretStr
from typer.testing import CliRunner

from quartet.cli import app
from quartet.domain.artifacts import ARTIFACT_MODELS
from quartet.llm import cli as llm_cli
from quartet.llm.anthropic_runner import make_anthropic_client
from quartet.llm.base import Provider, RoleRunner
from quartet.llm.openai_runner import make_openai_client
from tests.unit.llm._records import scripted_runner

KEY = "sk-ant-cli-secret-value"


@pytest.fixture(autouse=True)
def _database_url(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql://u:pw@127.0.0.1:5432/q")


def _anthropic_info(model_id: str, *, max_tokens: int, fallbacks: list[str]) -> dict[str, Any]:
    return {
        "type": "model",
        "id": model_id,
        "display_name": model_id,
        "created_at": "2026-01-01T00:00:00Z",
        "lifecycle": "active",
        "max_input_tokens": 1_000_000,
        "max_tokens": max_tokens,
        "allowed_fallback_models": fallbacks,
    }


def _clients(
    *, max_tokens: int = 128_000, openai_status: int = 200
) -> tuple[anthropic.AsyncAnthropic, openai.AsyncOpenAI]:
    def anthropic_handler(request: httpx2.Request) -> httpx2.Response:
        model_id = request.url.path.rsplit("/", 1)[-1]
        return httpx2.Response(
            200, json=_anthropic_info(model_id, max_tokens=max_tokens, fallbacks=["x-fallback"])
        )

    def openai_handler(request: httpx2.Request) -> httpx2.Response:
        if openai_status != 200:
            return httpx2.Response(openai_status, json={"error": {"message": f"nope {KEY}"}})
        model_id = request.url.path.rsplit("/", 1)[-1]
        return httpx2.Response(
            200, json={"id": model_id, "object": "model", "created": 0, "owned_by": "openai"}
        )

    a = make_anthropic_client(
        SecretStr(KEY),
        http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(anthropic_handler)),
    )
    o = make_openai_client(
        SecretStr(KEY),
        http_client=openai.DefaultAsyncHttpx2Client(transport=httpx2.MockTransport(openai_handler)),
    )
    return a, o


def test_verify_models_passes_when_limits_match(monkeypatch):
    monkeypatch.setattr(llm_cli, "_make_clients", lambda *a, **k: _clients())
    r = CliRunner().invoke(app, ["llm", "verify-models"])
    assert r.exit_code == 0, r.output
    assert "✓ claude-opus-5-5" in r.output and "x-fallback" in r.output
    assert "✓ gpt-6.1-sol" in r.output and "✗" not in r.output


def test_verify_models_reports_limit_mismatch(monkeypatch):
    monkeypatch.setattr(llm_cli, "_make_clients", lambda *a, **k: _clients(max_tokens=64_000))
    r = CliRunner().invoke(app, ["llm", "verify-models", "--provider", "anthropic"])
    assert r.exit_code == 1
    assert "claude-opus-5-5" in r.output and "64000" in r.output
    assert "sk-" not in r.output and "Traceback" not in r.output


def test_verify_models_fails_on_missing_catalog_fallback(monkeypatch):
    from quartet.llm.catalog import ModelCatalog

    real = ModelCatalog.load()

    def patched_load():
        data = {}
        for m in real:
            data[m.model] = {
                "provider": m.provider.value,
                "context_window": m.context_window,
                "max_output_tokens": m.max_output_tokens,
                "efforts": sorted(m.efforts),
                "fallback_models": ["not-allowed"] if m.model == "claude-opus-5-5" else [],
                "verified_via": m.verified_via,
                "verified_at": m.verified_at,
            }
        return ModelCatalog.from_mapping({"models": data})

    monkeypatch.setattr(ModelCatalog, "load", staticmethod(patched_load))
    monkeypatch.setattr(llm_cli, "_make_clients", lambda *a, **k: _clients())
    r = CliRunner().invoke(app, ["llm", "verify-models", "--provider", "anthropic"])
    assert r.exit_code == 1 and "not-allowed" in r.output


def test_verify_models_openai_failure_never_echoes_the_key(monkeypatch):
    monkeypatch.setattr(llm_cli, "_make_clients", lambda *a, **k: _clients(openai_status=404))
    r = CliRunner().invoke(app, ["llm", "verify-models", "--provider", "openai"])
    assert r.exit_code == 1 and "✗ gpt-6.1-sol" in r.output and "404" in r.output
    assert KEY not in r.output and "Traceback" not in r.output


def _scripted(cost: str) -> Callable[..., dict[Provider, RoleRunner]]:
    return lambda settings, providers: {
        p: scripted_runner(p, cost_per_check=Decimal(cost)) for p in providers
    }


def test_smoke_cli_exit_codes(monkeypatch):
    monkeypatch.setattr(llm_cli, "_make_runners", _scripted("0.01"))
    r = CliRunner().invoke(app, ["llm", "smoke"])
    checks = 2 * (len(ARTIFACT_MODELS) + 1)
    assert r.exit_code == 0, r.output
    assert r.output.count("✓") == checks and "✗" not in r.output
    assert f"total cost: ${Decimal('0.010000') * checks}" in r.output

    monkeypatch.setattr(llm_cli, "_make_runners", _scripted("0.30"))
    r = CliRunner().invoke(app, ["llm", "smoke", "--max-usd", "0.50"])
    assert r.exit_code == 1
    assert "skipped: smoke budget reached" in r.output and "total cost: $0.600000" in r.output


def test_smoke_cli_single_provider(monkeypatch):
    monkeypatch.setattr(llm_cli, "_make_runners", _scripted("0.01"))
    r = CliRunner().invoke(app, ["llm", "smoke", "--provider", "openai"])
    assert r.exit_code == 0 and "anthropic" not in r.output and Provider.OPENAI.value in r.output


@pytest.mark.parametrize("value", ["abc", "0", "-1", "nan"])
def test_smoke_cli_rejects_bad_budget(value):
    r = CliRunner().invoke(app, ["llm", "smoke", "--max-usd", value])
    assert r.exit_code == 1 and "--max-usd" in r.output and "Traceback" not in r.output


@pytest.mark.parametrize("command", ["smoke", "verify-models"])
def test_missing_key_message(command):
    r = CliRunner().invoke(app, ["llm", command])
    assert r.exit_code == 1
    assert "ANTHROPIC_API_KEY" in r.output and "Traceback" not in r.output


def test_missing_openai_key_message(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    r = CliRunner().invoke(app, ["llm", "smoke"])
    assert r.exit_code == 1 and "OPENAI_API_KEY" in r.output and KEY not in r.output


def test_invalid_settings_fail_cleanly(monkeypatch):
    monkeypatch.delenv("QUARTET_DATABASE_URL")
    r = CliRunner().invoke(app, ["llm", "smoke"])
    assert r.exit_code == 1 and "QUARTET_DATABASE_URL" in r.output and "Traceback" not in r.output
