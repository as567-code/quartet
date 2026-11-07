"""OpenTelemetry span helpers with pinned GenAI attribute names (API only, no SDK)."""

from contextlib import AbstractContextManager

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from quartet.domain.states import Role
from quartet.llm.base import CallRecord, Provider

TRACER_NAME = "quartet"

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GEN_AI_REQUEST_REASONING_LEVEL = "gen_ai.request.reasoning.level"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"  # prompt, including cache
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS = "gen_ai.usage.cache_write.input_tokens"
GEN_AI_USAGE_REASONING_OUTPUT_TOKENS = "gen_ai.usage.reasoning.output_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
ERROR_TYPE = "error.type"

QUARTET_ROLE = "quartet.role"
QUARTET_COST_USD = "quartet.cost_usd"
QUARTET_COST_ESTIMATED = "quartet.cost_estimated"
QUARTET_ATTEMPTS = "quartet.attempts"


def _span(
    name: str, kind: SpanKind, attributes: dict[str, str | int | bool]
) -> AbstractContextManager[Span]:
    # OpenTelemetry's defaults would attach any crossing exception, unredacted, as an event.
    # Status is set only by mark_error, with a description the caller has already redacted.
    return trace.get_tracer(TRACER_NAME).start_as_current_span(
        name,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    )


def role_span(role: Role) -> AbstractContextManager[Span]:
    return _span(f"role:{role.value.lower()}", SpanKind.INTERNAL, {QUARTET_ROLE: role.value})


def llm_call_span(
    provider: Provider,
    requested_model: str,
    role: Role,
    max_output_tokens: int,
    effort: str,
) -> AbstractContextManager[Span]:
    return _span(
        "llm.call",
        SpanKind.CLIENT,
        {
            GEN_AI_OPERATION_NAME: "chat",
            GEN_AI_PROVIDER_NAME: provider.value,
            GEN_AI_REQUEST_MODEL: requested_model,
            GEN_AI_REQUEST_MAX_TOKENS: max_output_tokens,
            GEN_AI_REQUEST_REASONING_LEVEL: effort,
            QUARTET_ROLE: role.value,
        },
    )


def annotate_llm_call(span: Span, record: CallRecord) -> None:
    usage = record.usage
    span.set_attributes(
        {
            GEN_AI_RESPONSE_MODEL: record.model,
            GEN_AI_RESPONSE_FINISH_REASONS: [record.stop_reason],
            GEN_AI_USAGE_INPUT_TOKENS: usage.prompt_tokens,
            GEN_AI_USAGE_OUTPUT_TOKENS: usage.output_tokens,
            GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS: usage.cache_read_tokens,
            GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS: (
                usage.cache_write_short_tokens + usage.cache_write_long_tokens
            ),
            GEN_AI_USAGE_REASONING_OUTPUT_TOKENS: usage.reasoning_tokens,
            QUARTET_COST_USD: format(record.cost_usd, "f"),
            QUARTET_COST_ESTIMATED: record.cost_estimated,
            QUARTET_ATTEMPTS: record.attempts,
        }
    )


def tool_span(name: str, call_id: str) -> AbstractContextManager[Span]:
    return _span(
        "tool.call",
        SpanKind.INTERNAL,
        {
            GEN_AI_OPERATION_NAME: "execute_tool",
            GEN_AI_TOOL_NAME: name,
            GEN_AI_TOOL_CALL_ID: call_id,
        },
    )


def mark_error(span: Span, error_type: str, description: str) -> None:
    """Set ERROR status. ``description`` must already be redacted; never record_exception."""
    span.set_attribute(ERROR_TYPE, error_type)
    span.set_status(Status(StatusCode.ERROR, description))


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None
