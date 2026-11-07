from collections.abc import Callable
from contextlib import AbstractContextManager
from decimal import Decimal

import pytest
from opentelemetry.trace import Span, SpanKind, StatusCode

from quartet.domain.states import Role
from quartet.llm.base import Provider
from quartet.telemetry.tracing import (
    annotate_llm_call,
    current_trace_id,
    llm_call_span,
    mark_error,
    role_span,
    tool_span,
)
from tests.unit.llm._records import call_record


def test_llm_call_span_shape(span_exporter):
    with (
        role_span(Role.REVIEWER),
        llm_call_span(Provider.OPENAI, "gpt-6.1-sol", Role.REVIEWER, 8_000, "high") as span,
    ):
        assert len(current_trace_id() or "") == 32
        annotate_llm_call(span, call_record(cost=Decimal("0.012345")))
    llm, role = span_exporter.get_finished_spans()
    assert (llm.name, llm.kind) == ("llm.call", SpanKind.CLIENT)
    assert llm.parent.span_id == role.context.span_id
    assert role.name == "role:reviewer" and llm.attributes["gen_ai.provider.name"] == "openai"
    assert llm.attributes["quartet.cost_usd"] == "0.012345"
    assert llm.attributes["gen_ai.operation.name"] == "chat"
    assert llm.attributes["gen_ai.request.max_tokens"] == 8_000
    assert llm.attributes["gen_ai.usage.input_tokens"] == 1_000
    assert llm.attributes["gen_ai.usage.output_tokens"] == 200
    assert llm.attributes["gen_ai.response.finish_reasons"] == ("end_turn",)
    assert llm.attributes["quartet.cost_estimated"] is False


def test_tool_span_and_error(span_exporter):
    with tool_span("read_file", "c1") as span:
        mark_error(span, "ValidationError", "ValidationError")
    (s,) = span_exporter.get_finished_spans()
    assert s.name == "tool.call" and s.status.status_code is StatusCode.ERROR and not s.events
    assert s.attributes["error.type"] == "ValidationError"
    assert s.attributes["gen_ai.operation.name"] == "execute_tool"


def test_exceptions_crossing_spans_are_not_recorded(span_exporter):
    key = "sk-ant-api03-" + "A" * 40
    makers: list[Callable[[], AbstractContextManager[Span]]] = [
        lambda: tool_span("read_file", "c1"),
        lambda: role_span(Role.PLANNER),
        lambda: llm_call_span(Provider.ANTHROPIC, "claude-opus-5-5", Role.PLANNER, 8_000, "high"),
    ]
    for make in makers:
        with pytest.raises(RuntimeError), make():
            raise RuntimeError(f"boom {key}")
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 3
    for s in spans:
        assert s.events == () and key not in (s.status.description or "")


def test_no_active_span_has_no_trace_id(span_exporter):
    assert current_trace_id() is None
