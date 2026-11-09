from types import SimpleNamespace
from typing import Any, cast

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from quartet.domain.artifacts import Plan, ReviewReport, strict_json_schema
from quartet.llm.anthropic_runner import (
    FALLBACK_BETA,
    AnthropicAdapter,
    check_strict_budget,
    make_anthropic_client,
    turn_from_message,
    usage_from_anthropic,
)
from quartet.llm.base import ProviderError, Usage
from quartet.llm.loop import ProviderTurn, StopKind, ToolResult
from quartet.llm.retry import classify
from tests.unit.llm._records import cfg, plan_json
from tests.unit.llm._sse import anthropic_sse, beta_message, text_message, tool_use_message
from tests.unit.llm._streams import broken_response
from tests.unit.llm.conftest import ECHO, PROMPT, READ_FILE, Capture, tool

pytestmark = pytest.mark.anyio


def adapter_for(capture: Capture, *responses: Any) -> AnthropicAdapter:
    client = make_anthropic_client(SecretStr("sk-test"), http_client=capture.client(*responses))
    return AnthropicAdapter(client)


async def test_request_shape(capture):
    adapter = adapter_for(capture, text_message(plan_json()))
    adapter.begin(cfg(effort="high"), PROMPT, [READ_FILE, ECHO])
    turn = await adapter.complete(timeout=30)
    body, headers = capture.bodies[0], capture.headers[0]
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True
    assert body["max_tokens"] == 8_000
    assert body["output_config"] == {
        "effort": "high",
        "format": {"type": "json_schema", "schema": strict_json_schema(Plan)},
    }
    assert body["system"] == [
        {"type": "text", "text": PROMPT.system, "cache_control": {"type": "ephemeral"}}
    ]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in body["tools"]] == ["echo", "read_file"]
    assert all(t["strict"] is True for t in body["tools"])
    for banned in (
        "temperature",
        "top_p",
        "top_k",
        "thinking",
        "tool_choice",
        "stop_sequences",
        "fallbacks",
    ):
        assert banned not in body
    assert body["messages"] == [{"role": "user", "content": PROMPT.user}]
    assert headers["authorization"] == "Bearer sk-test" and "anthropic-beta" not in headers
    assert turn.request_id == "req_1"
    assert turn.stop is StopKind.FINAL and turn.final_text == plan_json()


async def test_no_tools_omits_tools_key(capture):
    adapter = adapter_for(capture, text_message(plan_json()))
    adapter.begin(cfg(), PROMPT, [])
    await adapter.complete(timeout=30)
    assert "tools" not in capture.bodies[0]


async def test_server_fallback_adds_beta(capture):
    adapter = adapter_for(capture, text_message(plan_json()))
    adapter.begin(cfg(server_fallback=True), PROMPT, [])
    await adapter.complete(timeout=30)
    assert capture.bodies[0]["fallbacks"] == "default"
    assert FALLBACK_BETA in capture.headers[0]["anthropic-beta"]


async def test_round_trip_preserves_thinking_and_batches_results(capture):
    first = tool_use_message(
        ("c1", "echo", {"text": "a"}),
        ("c2", "read_file", {"text": "b"}),
        thinking_signature="sig",
    )
    adapter = adapter_for(capture, first, text_message(plan_json()))
    adapter.begin(cfg(), PROMPT, [READ_FILE, ECHO])
    turn = await adapter.complete(timeout=30)
    assert turn.stop is StopKind.TOOL_USE
    assert [(r.call_id, r.name, r.arguments) for r in turn.tool_requests] == [
        ("c1", "echo", {"text": "a"}),
        ("c2", "read_file", {"text": "b"}),
    ]
    adapter.add_tool_results([ToolResult("c1", "fine", False), ToolResult("c2", "bad", True)])
    await adapter.complete(timeout=30)
    messages = capture.bodies[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"] == first["content"]
    assert messages[1]["content"][0]["signature"] == "sig"
    assert messages[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": "fine", "is_error": False},
        {"type": "tool_result", "tool_use_id": "c2", "content": "bad", "is_error": True},
    ]


async def test_add_user_text_answers_pending_tool_use(capture):
    truncated = tool_use_message(("c1", "echo", {"text": "a"}), stop_reason="max_tokens")
    adapter = adapter_for(capture, truncated, text_message(plan_json()))
    adapter.begin(cfg(), PROMPT, [ECHO])
    turn = await adapter.complete(timeout=30)
    assert turn.stop is StopKind.TRUNCATED
    adapter.add_user_text("again")
    await adapter.complete(timeout=30)
    last = capture.bodies[1]["messages"][-1]
    assert last["role"] == "user" and len(last["content"]) == 2
    result, text = last["content"]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "c1"
    assert result["is_error"] is True and result["content"].startswith("not executed")
    assert text == {"type": "text", "text": "again"}


async def test_add_user_text_without_pending_is_just_text(capture):
    adapter = adapter_for(capture, text_message("{}"), text_message(plan_json()))
    adapter.begin(cfg(), PROMPT, [])
    await adapter.complete(timeout=30)
    adapter.add_user_text("fix it")
    await adapter.complete(timeout=30)
    assert capture.bodies[1]["messages"][-1]["content"] == [{"type": "text", "text": "fix it"}]


async def test_tool_results_clear_pending(capture):
    adapter = adapter_for(
        capture,
        tool_use_message(("c1", "echo", {"text": "a"})),
        text_message("{}"),
        text_message(plan_json()),
    )
    adapter.begin(cfg(), PROMPT, [ECHO])
    await adapter.complete(timeout=30)
    adapter.add_tool_results([ToolResult("c1", "ok", False)])
    await adapter.complete(timeout=30)
    adapter.add_user_text("again")
    await adapter.complete(timeout=30)
    assert capture.bodies[2]["messages"][-1]["content"] == [{"type": "text", "text": "again"}]


async def test_refusal_carries_category(capture):
    refused = text_message("", stop_reason="refusal")
    refused["content"] = []
    refused["stop_details"] = {"type": "refusal", "category": "cyber"}
    adapter = adapter_for(capture, refused)
    adapter.begin(cfg(), PROMPT, [])
    turn = await adapter.complete(timeout=30)
    assert turn.stop is StopKind.REFUSAL and turn.refusal_category == "cyber"


async def test_unusable_stop_is_billed_provider_error(capture):
    adapter = adapter_for(capture, text_message("partial", stop_reason="pause_turn"))
    adapter.begin(cfg(), PROMPT, [])
    with pytest.raises(ProviderError) as info:
        await adapter.complete(timeout=30)
    err = info.value
    assert err.retryable is False and "pause_turn" in str(err) and err.request_id == "req_1"
    assert isinstance(err.turn, ProviderTurn)
    assert err.turn.usage_parts[0].usage == Usage(100, 0, 0, 0, 20, 0)
    assert err.turn.request_id == "req_1"


def test_usage_normalisation():
    u = usage_from_anthropic(
        SimpleNamespace(
            input_tokens=100,
            cache_read_input_tokens=200,
            cache_creation_input_tokens=70,
            cache_creation=SimpleNamespace(
                ephemeral_5m_input_tokens=30, ephemeral_1h_input_tokens=40
            ),
            output_tokens=50,
            output_tokens_details=SimpleNamespace(thinking_tokens=20),
        )
    )
    assert u == Usage(100, 200, 30, 40, 50, 20)
    bare = SimpleNamespace(
        input_tokens=5,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=9,
        cache_creation=None,
        output_tokens=1,
        output_tokens_details=None,
    )
    assert usage_from_anthropic(bare) == Usage(5, 0, 9, 0, 1, 0)
    assert usage_from_anthropic(SimpleNamespace()) == Usage.zero()


def test_fallback_iterations_priced_per_model():
    msg = beta_message(
        model="claude-opus-5",
        stop_reason="end_turn",
        text=plan_json(),
        iterations=[
            {
                "type": "message",
                "model": "claude-opus-5-5",
                "input_tokens": 1_000,
                "output_tokens": 0,
            },
            {
                "type": "fallback_message",
                "model": "claude-opus-5",
                "input_tokens": 1_000,
                "output_tokens": 500,
            },
        ],
    )
    turn = turn_from_message(msg, "claude-opus-5-5")
    assert turn.model == "claude-opus-5"
    assert [p.model for p in turn.usage_parts] == ["claude-opus-5-5", "claude-opus-5"]
    assert turn.usage_parts[1].usage == Usage(1_000, 0, 0, 0, 500, 0)


def test_no_iterations_uses_message_usage():
    msg = beta_message(model="claude-opus-5-5", stop_reason="end_turn", text="x", iterations=[])
    msg.usage.input_tokens, msg.usage.output_tokens = 7, 3
    turn = turn_from_message(msg, "claude-opus-5-5")
    assert turn.usage_parts[0].model == "claude-opus-5-5"
    assert turn.usage_parts[0].usage == Usage(7, 0, 0, 0, 3, 0)


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("refusal", StopKind.REFUSAL),
        ("max_tokens", StopKind.TRUNCATED),
        ("model_context_window_exceeded", StopKind.TRUNCATED),
        ("tool_use", StopKind.TOOL_USE),
        ("end_turn", StopKind.FINAL),
        ("stop_sequence", StopKind.FINAL),
    ],
)
def test_stop_mapping(reason, kind):
    msg = beta_message(model="claude-opus-5-5", stop_reason=reason, text="hi", iterations=[])
    turn = turn_from_message(msg, "claude-opus-5-5")
    assert turn.stop is kind and turn.stop_reason == reason
    assert (turn.final_text == "hi") is (kind is StopKind.FINAL)


def test_non_object_tool_arguments_become_none():
    msg = beta_message(model="claude-opus-5-5", stop_reason="tool_use", text="", iterations=[])
    msg.content = [cast(Any, SimpleNamespace)(type="tool_use", id="c1", name="echo", input="oops")]
    turn = turn_from_message(msg, "claude-opus-5-5")
    assert turn.tool_requests[0].arguments is None


@pytest.mark.parametrize("reason", ["pause_turn", "compaction", "Not Valid!"])
def test_pause_turn_is_provider_error(reason):
    msg = beta_message(model="claude-opus-5-5", stop_reason=reason, text="x", iterations=[])
    with pytest.raises(ProviderError) as info:
        turn_from_message(msg, "claude-opus-5-5")
    assert info.value.retryable is False and info.value.turn is not None
    assert "!" not in str(info.value)
    # The billed turn carries the sanitised reason too, never the raw vendor string.
    billed = cast(ProviderTurn, info.value.turn)
    assert "!" not in billed.stop_reason
    assert billed.stop_reason == (reason if reason != "Not Valid!" else "<invalid>")


async def test_sdk_spans_have_no_content_even_with_env_override(
    span_exporter, monkeypatch, capture
):
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    adapter = adapter_for(capture, text_message(plan_json()))
    adapter.begin(cfg(), PROMPT.__class__("sys", "MARKER-c0ffee"), [])
    await adapter.complete(timeout=30)
    spans = span_exporter.get_finished_spans()
    assert any(s.name == "anthropic.messages.create" for s in spans)
    assert all("MARKER-c0ffee" not in str(v) for s in spans for v in s.attributes.values())


def test_client_options():
    c = make_anthropic_client(SecretStr("sk-test"))
    assert c.max_retries == 0 and c.open_telemetry["traces"]["content_mode"] == "metadata_only"
    assert c.open_telemetry["propagation"] is False


def test_check_strict_budget():
    with pytest.raises(ValueError, match="strict tools"):
        check_strict_budget([tool(f"t{i}") for i in range(21)], strict_json_schema(Plan))
    check_strict_budget([tool(f"t{i}") for i in range(5)], strict_json_schema(ReviewReport))
    with pytest.raises(ValueError, match="union"):
        check_strict_budget([], {"properties": {f"p{i}": {"anyOf": []} for i in range(17)}})
    check_strict_budget([], {"properties": {f"p{i}": {"anyOf": []} for i in range(16)}})


async def test_begin_enforces_budget(capture):
    adapter = adapter_for(capture)
    with pytest.raises(ValueError, match="strict tools"):
        adapter.begin(cfg(), PROMPT, [tool(f"t{i}") for i in range(21)])


async def test_complete_requires_begin(capture):
    with pytest.raises(RuntimeError):
        await adapter_for(capture).complete(timeout=1)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx2.ReadError("boom"), anthropic.APIConnectionError),
        (httpx2.RemoteProtocolError("peer closed"), anthropic.APIConnectionError),
        (httpx2.ReadTimeout("slow"), anthropic.APITimeoutError),
    ],
    ids=["read_error", "remote_protocol_error", "read_timeout"],
)
async def test_mid_stream_transport_error_becomes_a_retryable_sdk_error(capture, error, expected):
    body = anthropic_sse(text_message(plan_json()))
    adapter = adapter_for(capture, broken_response(body, error))
    adapter.begin(cfg(), PROMPT, [])
    with pytest.raises(anthropic.APIConnectionError) as info:
        await adapter.complete(timeout=30)
    assert type(info.value) is expected
    assert info.value.__context__ is None
    decision = classify(info.value)
    assert decision is not None and decision.retryable
    assert adapter._messages == [{"role": "user", "content": PROMPT.user}]  # nothing appended


def test_transport_error_without_a_request_still_converts():
    from quartet.llm.anthropic_runner import _sdk_transport_error

    bare = _sdk_transport_error(httpx2.ReadError("boom"))
    assert type(bare) is anthropic.APIConnectionError and bare.request.method == "POST"
