import json
from collections.abc import Mapping
from typing import Any

import httpx2
import pytest
from openai.types.responses import Response
from pydantic import SecretStr

from quartet.domain.artifacts import ContextDigest, Plan, ReviewReport, strict_json_schema
from quartet.domain.states import Role
from quartet.llm.base import (
    EMPTY_TOOLBOX,
    Provider,
    ProviderError,
    RoleConfig,
    RoleInput,
    RoleResult,
    Toolbox,
    ToolOutcome,
    Usage,
)
from quartet.llm.fake import ListRecorder
from quartet.llm.loop import LoopDeps, ProviderTurn, StopKind, ToolResult, run_step
from quartet.llm.openai_runner import (
    OpenAIAdapter,
    make_openai_client,
    schema_format_name,
    turn_from_response,
    usage_from_openai,
)
from quartet.llm.pricing import PricingTable
from quartet.llm.retry import RetryPolicy
from quartet.telemetry.redaction import Redactor
from tests.unit.llm._records import FakeClock, cfg
from tests.unit.llm._sse import (
    function_call_item,
    message_item,
    ns,
    reasoning_item,
    refusal_part,
    resp,
    text_part,
    usage,
)
from tests.unit.llm.conftest import ECHO, PROMPT, READ_FILE, Capture

pytestmark = pytest.mark.anyio

MODEL = "gpt-6.1-sol"
USAGE = usage(100, 0, 0, 20)
REPORT = ReviewReport(
    schema_version=1, verdict="approve", findings=[], injection_suspected=False
).model_dump_json()


def config(**overrides: Any) -> RoleConfig:
    base: dict[str, Any] = {
        "provider": Provider.OPENAI,
        "model": MODEL,
        "role": Role.REVIEWER,
        "output_model": ReviewReport,
    }
    return cfg(**{**base, **overrides})


def adapter_for(capture: Capture, *responses: Any) -> OpenAIAdapter:
    client = make_openai_client(SecretStr("sk-test"), http_client=capture.openai_client(*responses))
    return OpenAIAdapter(client)


def final(text: str = REPORT) -> Response:
    return resp(output=[message_item(text_part(text))], usage=USAGE)


def calls(*ids: str) -> Response:
    return resp(
        output=[reasoning_item(), *(function_call_item(i, "echo", '{"text": "x"}') for i in ids)],
        usage=USAGE,
    )


async def test_request_shape(capture):
    adapter = adapter_for(capture, final())
    adapter.begin(config(), PROMPT, [READ_FILE, ECHO])
    turn = await adapter.complete(timeout=30)
    body, headers = capture.bodies[0], capture.headers[0]
    assert body["model"] == MODEL and body["stream"] is True
    assert body["instructions"] == PROMPT.system
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["reasoning"] == {"effort": "high"}
    assert body["max_output_tokens"] == 8_000
    assert body["text"] == {
        "format": {
            "type": "json_schema",
            "name": "review_report",
            "schema": strict_json_schema(ReviewReport),
            "strict": True,
        }
    }
    assert [t["name"] for t in body["tools"]] == ["echo", "read_file"]
    assert body["tools"][0] == {
        "type": "function",
        "name": "echo",
        "description": ECHO.description,
        "parameters": dict(ECHO.input_schema),
        "strict": True,
    }
    for banned in ("temperature", "top_p", "previous_response_id", "tool_choice"):
        assert banned not in body
    assert body["input"][0] == {
        "role": "user",
        "content": [{"type": "input_text", "text": PROMPT.user}],
    }
    assert headers["authorization"] == "Bearer sk-test"
    assert turn.stop is StopKind.FINAL and turn.final_text == REPORT


async def test_no_tools_key_without_tools(capture):
    adapter = adapter_for(capture, final())
    adapter.begin(config(), PROMPT, [])
    await adapter.complete(timeout=30)
    assert "tools" not in capture.bodies[0]


async def test_transcript_keeps_reasoning_items_unmodified(capture):
    adapter = adapter_for(capture, calls("c1", "c2"), final())
    adapter.begin(config(), PROMPT, [ECHO])
    turn = await adapter.complete(timeout=30)
    assert [r.call_id for r in turn.tool_requests] == ["c1", "c2"]
    adapter.add_tool_results([ToolResult("c1", "one", False), ToolResult("c2", "two", False)])
    await adapter.complete(timeout=30)
    first = capture.bodies[0]["input"][0]
    assert capture.bodies[1]["input"] == [
        first,
        reasoning_item(),
        function_call_item("c1", "echo", '{"text": "x"}'),
        function_call_item("c2", "echo", '{"text": "x"}'),
        {"type": "function_call_output", "call_id": "c1", "output": "one"},
        {"type": "function_call_output", "call_id": "c2", "output": "two"},
    ]


async def test_every_call_id_answered(capture):
    adapter = adapter_for(capture, calls("c1", "c2"), final())
    adapter.begin(config(), PROMPT, [ECHO])
    await adapter.complete(timeout=30)
    adapter.add_tool_results([ToolResult("c1", "fine", False), ToolResult("c2", "bad", True)])
    await adapter.complete(timeout=30)
    outputs = [i for i in capture.bodies[1]["input"] if i.get("type") == "function_call_output"]
    assert [o["call_id"] for o in outputs] == ["c1", "c2"]
    assert outputs[0]["output"] == "fine"
    assert json.loads(outputs[1]["output"]) == {"is_error": True, "content": "bad"}


async def test_add_user_text_answers_pending_calls(capture):
    adapter = adapter_for(capture, calls("c1"), final())
    adapter.begin(config(), PROMPT, [ECHO])
    await adapter.complete(timeout=30)
    adapter.add_user_text("please continue")
    await adapter.complete(timeout=30)
    tail = capture.bodies[1]["input"][-2:]
    assert tail[0]["type"] == "function_call_output" and tail[0]["call_id"] == "c1"
    assert json.loads(tail[0]["output"])["is_error"] is True
    assert tail[1] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "please continue"}],
    }


async def test_request_id_from_stream_headers(capture):
    adapter = adapter_for(capture, final())
    adapter.begin(config(), PROMPT, [])
    turn = await adapter.complete(timeout=30)
    assert turn.request_id == "req_1" and turn.model == MODEL


async def test_complete_requires_begin(capture):
    with pytest.raises(RuntimeError):
        await adapter_for(capture).complete(timeout=1)


def test_incomplete_max_output_is_repair_not_parse():
    turn = turn_from_response(
        resp(
            status="incomplete",
            reason="max_output_tokens",
            output=[reasoning_item()],
            usage=usage(1_000, 0, 0, 900),
        ),
        MODEL,
    )
    assert turn.stop is StopKind.TRUNCATED and turn.usage_parts[0].usage.output_tokens == 900


async def test_loop_over_transport_incomplete_then_valid(capture):
    truncated = resp(
        status="incomplete",
        reason="max_output_tokens",
        output=[reasoning_item()],
        usage=usage(1_000, 0, 0, 900),
    )
    adapter = adapter_for(capture, truncated, final())
    result, recorder = await run(adapter, capture)
    assert result.repairs == 1 and len(recorder.llm) == 2


class Box:
    """A toolbox that records executions."""

    def __init__(self) -> None:
        self.specs = [ECHO]
        self.executed: list[Mapping[str, Any]] = []

    async def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        self.executed.append(arguments)
        return ToolOutcome("echoed", False)


async def run(
    adapter: OpenAIAdapter, capture: Capture, toolbox: Toolbox = EMPTY_TOOLBOX
) -> tuple[RoleResult, ListRecorder]:
    async def no_sleep(_seconds: float) -> None:
        return None

    deps = LoopDeps(
        pricing=PricingTable.load(),
        redactor=Redactor(),
        retry_policy=RetryPolicy(),
        clock=FakeClock(),
        sleep=no_sleep,
        rand=lambda: 0.5,
    )
    recorder = ListRecorder()
    result = await run_step(
        adapter, config(), RoleInput("Review it.", (), "t"), toolbox, recorder, deps
    )
    return result, recorder


async def test_loop_over_transport_bad_json_arguments(capture):
    bad = resp(output=[function_call_item("c1", "echo", "{not json")], usage=USAGE)
    adapter = adapter_for(capture, bad, final())
    box = Box()
    result, _ = await run(adapter, capture, box)
    assert isinstance(result.artifact, ReviewReport) and box.executed == []
    outputs = [i for i in capture.bodies[1]["input"] if i.get("type") == "function_call_output"]
    assert [o["call_id"] for o in outputs] == ["c1"]
    assert json.loads(outputs[0]["output"])["is_error"] is True


async def test_loop_over_transport_tool_then_final(capture):
    adapter = adapter_for(capture, calls("c1"), final())
    box = Box()
    result, recorder = await run(adapter, capture, box)
    assert box.executed == [{"text": "x"}] and len(recorder.llm) == 2
    assert recorder.llm[0].request_id == "req_1" and result.artifact is not None


@pytest.mark.parametrize(
    "code,retryable",
    [("server_error", True), ("misalignment_policy_violation", False), ("invalid_prompt", False)],
)
def test_failed_status(code, retryable):
    response = resp(status="failed", output=[], usage=usage(50, 0, 0, 5), error_code=code)
    with pytest.raises(ProviderError) as info:
        turn_from_response(response, MODEL)
    err = info.value
    assert err.retryable is retryable and err.code == code
    assert isinstance(err.turn, ProviderTurn)
    assert err.turn.usage_parts[0].usage.output_tokens == 5


def test_failed_status_without_usage_has_no_turn():
    response = resp(status="failed", output=[], usage=None, error_code="server_error")
    with pytest.raises(ProviderError) as info:
        turn_from_response(response, MODEL)
    assert info.value.turn is None and info.value.retryable is True


def test_other_incomplete_and_unknown_status_are_terminal_errors():
    other = resp(status="incomplete", reason="steered", output=[], usage=USAGE)
    with pytest.raises(ProviderError) as info:
        turn_from_response(other, MODEL)
    assert info.value.retryable is False and info.value.turn is not None
    with pytest.raises(ProviderError) as info:
        turn_from_response(resp(status="cancelled", output=[], usage=USAGE), MODEL)
    assert info.value.retryable is False


def test_refusal_and_content_filter():
    refused = turn_from_response(resp(output=[message_item(refusal_part())], usage=USAGE), MODEL)
    assert refused.stop is StopKind.REFUSAL and refused.refusal_category is None
    filtered = turn_from_response(
        resp(status="incomplete", reason="content_filter", output=[], usage=USAGE), MODEL
    )
    assert filtered.stop is StopKind.REFUSAL and filtered.refusal_category == "content_filter"


def test_final_text_concatenates_output_text_parts():
    turn = turn_from_response(
        resp(output=[message_item(text_part("ab"), text_part("cd"))], usage=USAGE), MODEL
    )
    assert turn.stop is StopKind.FINAL and turn.final_text == "abcd"


def test_bad_json_arguments_become_none():
    items = [
        function_call_item("a", "echo", "{not json"),
        function_call_item("b", "echo", "[1]"),
        function_call_item("c", "echo", '{"text": "ok"}'),
    ]
    turn = turn_from_response(resp(output=items, usage=USAGE), MODEL)
    assert [r.arguments for r in turn.tool_requests] == [None, None, {"text": "ok"}]


def test_usage_normalisation():
    assert usage_from_openai(
        ns(
            input_tokens=1_000,
            input_tokens_details=ns(
                cached_tokens=600,
                cache_write_tokens=100,
                cache_write_tokens_details=ns(ttl_30m_tokens=70, ttl_12h_tokens=30),
            ),
            output_tokens=300,
            output_tokens_details=ns(reasoning_tokens=200),
        )
    ) == Usage(300, 600, 70, 30, 300, 200)


def test_usage_without_details_counts_writes_as_short_and_none_as_zero():
    no_split = ns(
        input_tokens=500,
        input_tokens_details=ns(cached_tokens=100, cache_write_tokens=50),
        output_tokens=10,
        output_tokens_details=None,
    )
    assert usage_from_openai(no_split) == Usage(350, 100, 50, 0, 10, 0)
    assert usage_from_openai(None) == Usage(0, 0, 0, 0, 0, 0)


def test_schema_format_name():
    assert schema_format_name(ReviewReport) == "review_report"
    assert schema_format_name(Plan) == "plan"
    assert schema_format_name(ContextDigest) == "context_digest"


async def test_error_event_and_missing_terminal_event(capture):
    def sse(*events: dict[str, Any]) -> httpx2.Response:
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "req_9"},
            content=body.encode(),
        )

    error = {
        "type": "error",
        "code": "rate_limit_exceeded",
        "message": "slow",
        "sequence_number": 0,
    }
    fatal = {**error, "code": "invalid_prompt"}
    created = {
        "type": "response.created",
        "sequence_number": 0,
        "response": resp(output=[], usage=None).to_dict(mode="json"),
    }
    adapter = adapter_for(capture, sse(error), sse(fatal), sse(created))
    adapter.begin(config(), PROMPT, [])
    with pytest.raises(ProviderError) as info:
        await adapter.complete(timeout=30)
    assert info.value.retryable is True and info.value.code == "rate_limit_exceeded"
    assert info.value.request_id == "req_9"
    with pytest.raises(ProviderError) as info:
        await adapter.complete(timeout=30)
    assert info.value.retryable is False and info.value.code == "invalid_prompt"
    with pytest.raises(ProviderError) as info:
        await adapter.complete(timeout=30)
    assert info.value.retryable is True and info.value.code is None
    assert len(capture.bodies[2]["input"]) == 1  # nothing appended after a failed attempt


def test_client_options():
    client = make_openai_client(SecretStr("sk-test"))
    assert client.max_retries == 0


async def test_failed_response_over_transport_carries_billed_turn_and_request_id(capture):
    failed = resp(status="failed", output=[], usage=usage(50, 0, 0, 5), error_code="server_error")
    adapter = adapter_for(capture, failed)
    adapter.begin(config(), PROMPT, [])
    with pytest.raises(ProviderError) as info:
        await adapter.complete(timeout=30)
    err = info.value
    assert err.request_id == "req_1" and err.retryable is True
    assert isinstance(err.turn, ProviderTurn) and err.turn.request_id == "req_1"
