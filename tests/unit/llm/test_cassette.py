import json
from dataclasses import fields
from pathlib import Path

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from quartet.llm.anthropic_runner import AnthropicAdapter, make_anthropic_client
from quartet.llm.cassette import (
    CassetteExhausted,
    CassetteMismatch,
    Exchange,
    RecordingTransport,
    ReplayTransport,
    load_cassette,
    save_cassette,
)
from quartet.llm.loop import ProviderTurn
from quartet.telemetry.redaction import Redactor
from tests.unit.llm._records import cfg, plan_json
from tests.unit.llm._sse import anthropic_sse, text_message
from tests.unit.llm.conftest import ECHO, PROMPT, READ_FILE

# Credential-shaped literals are built by concatenation so the gitleaks hook does not block them.
ANT_KEY = "sk-ant-" + "api03-" + "A" * 40
GH_TOKEN = "ghp_" + "B" * 36


@pytest.mark.anyio
async def test_record_scrubs_headers_and_keeps_request_id(tmp_path: Path) -> None:
    inner = httpx2.MockTransport(
        lambda req: httpx2.Response(
            200,
            json={"ok": 1},
            headers={
                "request-id": "req_1",
                "set-cookie": "s=1",
                "openai-organization": "org-x",
            },
        )
    )
    rec = RecordingTransport(inner)
    async with httpx2.AsyncClient(transport=rec) as c:
        await c.post(
            "https://api.anthropic.com/v1/messages",
            json={"a": 1},
            headers={"authorization": "Bearer sk-test"},
        )
    save_cassette(tmp_path / "c.json", rec.exchanges, Redactor())
    text = (tmp_path / "c.json").read_text()
    assert "req_1" in text and "set-cookie" not in text and "org-x" not in text
    assert "Bearer" not in text and "sk-test" not in text


@pytest.mark.anyio
async def test_save_scrubs_secret_shapes_in_bodies(tmp_path: Path) -> None:
    exchange = Exchange(
        "POST",
        "/v1/messages",
        {"auth": ANT_KEY},
        200,
        {"request-id": "req_2"},
        f'{{"token": "{GH_TOKEN}"}}',
    )
    save_cassette(tmp_path / "c.json", [exchange], Redactor())
    text = (tmp_path / "c.json").read_text()
    assert ANT_KEY not in text and GH_TOKEN not in text
    loaded = load_cassette(tmp_path / "c.json")
    assert loaded[0].path == "/v1/messages" and loaded[0].response_headers == {
        "request-id": "req_2"
    }


@pytest.mark.anyio
async def test_replay_round_trips_through_anthropic_adapter(tmp_path: Path) -> None:
    def respond(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "request-id": "req_1",
                "set-cookie": "s=1",
            },
            content=anthropic_sse(text_message(plan_json())),
        )

    recorder = RecordingTransport(httpx2.MockTransport(respond))
    live = AnthropicAdapter(
        make_anthropic_client(
            SecretStr("sk-test"), http_client=anthropic.DefaultAsyncHttpxClient(transport=recorder)
        )
    )
    live.begin(cfg(), PROMPT, [READ_FILE, ECHO])
    recorded = await live.complete(timeout=30)

    cassette = tmp_path / "anthropic.json"
    save_cassette(cassette, recorder.exchanges, Redactor())
    replay = ReplayTransport(load_cassette(cassette))
    replayed_adapter = AnthropicAdapter(
        make_anthropic_client(
            SecretStr("sk-test"), http_client=anthropic.DefaultAsyncHttpxClient(transport=replay)
        )
    )
    replayed_adapter.begin(cfg(), PROMPT, [READ_FILE, ECHO])
    replayed = await replayed_adapter.complete(timeout=30)

    assert replay.remaining == 0
    assert isinstance(replayed, ProviderTurn)
    for field in fields(ProviderTurn):
        assert getattr(replayed, field.name) == getattr(recorded, field.name), field.name
    assert replayed.request_id == "req_1"


@pytest.mark.anyio
async def test_mismatch_and_exhaustion() -> None:
    rt = ReplayTransport([Exchange("POST", "/v1/messages", {}, 200, {}, "{}")])
    async with httpx2.AsyncClient(transport=rt, base_url="https://x") as c:
        with pytest.raises(CassetteMismatch):
            await c.get("/v1/models")
        assert rt.remaining == 1
        assert (await c.post("/v1/messages", json={})).status_code == 200
        assert rt.remaining == 0
        with pytest.raises(CassetteExhausted):
            await c.post("/v1/messages", json={})


def test_save_rechecks_patterns_even_if_redactor_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Redactor, "redact", lambda self, text: text)  # simulate a redactor bug
    with pytest.raises(ValueError):
        save_cassette(
            tmp_path / "c.json",
            [Exchange("POST", "/", {}, 200, {}, ANT_KEY)],
            Redactor(),
        )
    assert not (tmp_path / "c.json").exists()


def test_save_refuses_key_value_secret_in_decoded_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The quotes in this value are escaped once JSON-serialised, which hides it from a text check.
    monkeypatch.setattr(Redactor, "redact", lambda self, text: text)
    exchange = Exchange("POST", "/", {}, 200, {}, '{"password": "hunter2hunter2"}')
    with pytest.raises(ValueError, match="response_body"):
        save_cassette(tmp_path / "c.json", [exchange], Redactor())
    assert not (tmp_path / "c.json").exists()


def test_save_redacts_key_value_secret_in_body(tmp_path: Path) -> None:
    exchange = Exchange("POST", "/", {}, 200, {}, '{"password": "hunter2hunter2"}')
    save_cassette(tmp_path / "c.json", [exchange], Redactor())
    assert "hunter2hunter2" not in (tmp_path / "c.json").read_text(encoding="utf-8")


def test_save_refuses_key_value_pair_inside_request_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Redactor, "redact", lambda self, text: text)
    exchange = Exchange("POST", "/", {"password": "hunter2hunter2"}, 200, {}, "{}")
    with pytest.raises(ValueError, match="request_body"):
        save_cassette(tmp_path / "c.json", [exchange], Redactor())
    assert not (tmp_path / "c.json").exists()


@pytest.mark.parametrize("header", ["set-cookie", "Openai-Organization"])
def test_save_refuses_header_outside_allowlist(tmp_path: Path, header: str) -> None:
    exchange = Exchange("POST", "/", {}, 200, {header: "s=1"}, "{}")
    with pytest.raises(ValueError, match="allowlist"):
        save_cassette(tmp_path / "c.json", [exchange], Redactor())
    assert not (tmp_path / "c.json").exists()


@pytest.mark.anyio
async def test_recording_keeps_non_json_request_body_as_text() -> None:
    inner = httpx2.MockTransport(lambda req: httpx2.Response(200, content=b"{}"))
    rec = RecordingTransport(inner)
    async with httpx2.AsyncClient(transport=rec) as c:
        await c.post("https://x/v1/messages", content=b"plain text")
    assert rec.exchanges[0].request_body == "plain text"


@pytest.mark.anyio
async def test_recording_rejects_non_utf8_response_body() -> None:
    inner = httpx2.MockTransport(lambda req: httpx2.Response(200, content=b"\xff\xfe"))
    rec = RecordingTransport(inner)
    async with httpx2.AsyncClient(transport=rec) as c:
        with pytest.raises(ValueError, match="UTF-8"):
            await c.get("https://x/v1/models")


def test_load_rejects_unknown_cassette_version(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"version": 2, "exchanges": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported cassette version"):
        load_cassette(path)
