"""Anthropic adapter: streamed beta Messages API, strict tools, structured output (P2-R9).

One ``AnthropicAdapter`` is one conversation. Assistant content is appended exactly as the API
returned it, so thinking signatures survive and the prompt cache stays valid. The adapter makes
exactly one HTTP attempt per ``complete``; retries belong to ``llm/retry.py``. SDK exceptions
propagate unchanged for the retry layer to classify.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import anthropic
import httpx2
from pydantic import SecretStr

from quartet.domain.artifacts import count_union_params, strict_json_schema
from quartet.llm.base import (
    Provider,
    ProviderError,
    RoleConfig,
    ToolSpec,
    Usage,
    UsagePart,
)
from quartet.llm.loop import ProviderAdapter, ProviderTurn, StopKind, ToolRequest, ToolResult
from quartet.llm.prompting import PromptParts

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_STRICT_TOOLS = 20
MAX_UNION_PARAMS = 16

NOT_EXECUTED = "not executed: the turn ended before this tool could run"
_SAFE_REASON = re.compile(r"[a-z_]{1,40}")
_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


def make_anthropic_client(
    api_key: SecretStr, *, http_client: httpx2.AsyncClient | None = None
) -> anthropic.AsyncAnthropic:
    """No SDK retries, and SDK spans that never carry content, whatever the environment says."""
    return anthropic.AsyncAnthropic(
        api_key=api_key.get_secret_value(),
        max_retries=0,
        http_client=http_client or anthropic.DefaultAsyncHttpxClient(),
        open_telemetry={
            "traces": {"enabled": True, "content_mode": "metadata_only"},
            "propagation": False,
        },
    )


def check_strict_budget(tools: Sequence[ToolSpec], output_schema: Mapping[str, Any]) -> None:
    """The API caps strict tools and union-typed parameters per request (tools plus output)."""
    if len(tools) > MAX_STRICT_TOOLS:
        raise ValueError(f"too many strict tools: {len(tools)} > {MAX_STRICT_TOOLS}")
    unions = count_union_params(output_schema) + sum(
        count_union_params(tool.input_schema) for tool in tools
    )
    if unions > MAX_UNION_PARAMS:
        raise ValueError(f"too many union-typed parameters: {unions} > {MAX_UNION_PARAMS}")


def _count(obj: Any, name: str) -> int:
    value = getattr(obj, name, None)
    return value if isinstance(value, int) else 0


def usage_from_anthropic(usage: Any) -> Usage:
    """Normalise a usage object; iteration entries lack some fields, and any field may be None."""
    breakdown = getattr(usage, "cache_creation", None)
    if breakdown is None:
        short, long = _count(usage, "cache_creation_input_tokens"), 0
    else:
        short = _count(breakdown, "ephemeral_5m_input_tokens")
        long = _count(breakdown, "ephemeral_1h_input_tokens")
    return Usage(
        uncached_input_tokens=_count(usage, "input_tokens"),
        cache_read_tokens=_count(usage, "cache_read_input_tokens"),
        cache_write_short_tokens=short,
        cache_write_long_tokens=long,
        output_tokens=_count(usage, "output_tokens"),
        reasoning_tokens=_count(getattr(usage, "output_tokens_details", None), "thinking_tokens"),
    )


def _usage_parts(message: Any) -> tuple[UsagePart, ...]:
    """One part per attempt (a fallback bills each attempt at its own model's rates)."""
    iterations = getattr(message.usage, "iterations", None)
    if iterations:
        return tuple(
            UsagePart(getattr(it, "model", None) or message.model, usage_from_anthropic(it))
            for it in iterations
        )
    return (UsagePart(message.model, usage_from_anthropic(message.usage)),)


def _arguments(raw: Any) -> dict[str, Any] | None:
    return dict(raw) if isinstance(raw, dict) else None


def turn_from_message(message: Any, requested_model: str) -> ProviderTurn:
    """Map a final beta message to a ``ProviderTurn``; unusable stops raise a billed error."""
    del requested_model  # the served model is message.model; the loop records both
    reason = message.stop_reason or ""
    requests = tuple(
        ToolRequest(b.id, b.name, _arguments(b.input))
        for b in message.content
        if b.type == "tool_use"
    )
    text = "".join(b.text for b in message.content if b.type == "text")
    parts = _usage_parts(message)

    def turn(kind: StopKind, **fields: Any) -> ProviderTurn:
        return ProviderTurn(
            stop=kind,
            tool_requests=fields.get("tool_requests", ()),
            final_text=fields.get("final_text"),
            refusal_category=fields.get("refusal_category"),
            model=message.model,
            usage_parts=parts,
            stop_reason=reason,
            request_id=None,
        )

    if reason == "tool_use":
        return turn(StopKind.TOOL_USE, tool_requests=requests)
    if reason in ("end_turn", "stop_sequence"):
        return turn(StopKind.FINAL, final_text=text)
    if reason == "refusal":
        category = getattr(getattr(message, "stop_details", None), "category", None)
        return turn(StopKind.REFUSAL, refusal_category=category)
    if reason in ("max_tokens", "model_context_window_exceeded"):
        return turn(StopKind.TRUNCATED)
    shown = reason if _SAFE_REASON.fullmatch(reason) else "<invalid>"
    raise ProviderError(
        f"unsupported stop_reason: {shown}", retryable=False, turn=turn(StopKind.TRUNCATED)
    )


class AnthropicAdapter(ProviderAdapter):
    provider = Provider.ANTHROPIC

    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        self._client = client
        self._config: RoleConfig | None = None
        self._request: dict[str, Any] = {}
        self._messages: list[Any] = []
        self._pending: list[str] = []

    def begin(self, config: RoleConfig, prompt: PromptParts, tools: Sequence[ToolSpec]) -> None:
        output_schema = strict_json_schema(config.output_model)
        check_strict_budget(tools, output_schema)
        request: dict[str, Any] = {
            "model": config.model,
            "max_tokens": config.max_output_tokens,
            "system": [{"type": "text", "text": prompt.system, "cache_control": dict(_EPHEMERAL)}],
            "output_config": {
                "effort": config.effort,
                "format": {"type": "json_schema", "schema": output_schema},
            },
            "cache_control": dict(_EPHEMERAL),
        }
        if tools:
            request["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": dict(t.input_schema),
                    "strict": True,
                }
                for t in sorted(tools, key=lambda t: t.name)
            ]
        if config.server_fallback:
            request["fallbacks"] = "default"
            request["betas"] = [FALLBACK_BETA]
        self._config = config
        self._request = request
        self._messages = [{"role": "user", "content": prompt.user}]
        self._pending = []

    async def complete(self, *, timeout: float) -> ProviderTurn:
        if self._config is None:
            raise RuntimeError("begin() must be called before complete()")
        async with self._client.beta.messages.stream(
            **self._request, messages=self._messages, timeout=timeout
        ) as stream:
            message = await stream.get_final_message()
            request_id = stream.request_id
        self._messages.append(
            {"role": "assistant", "content": [block.to_dict() for block in message.content]}
        )
        self._pending = [b.id for b in message.content if b.type == "tool_use"]
        try:
            turn = turn_from_message(message, self._config.model)
        except ProviderError as err:
            if isinstance(err.turn, ProviderTurn):
                err.turn = replace(err.turn, request_id=request_id)
            err.request_id = request_id
            raise
        return replace(turn, request_id=request_id)

    def add_tool_results(self, results: Sequence[ToolResult]) -> None:
        self._messages.append({"role": "user", "content": [_tool_result(r) for r in results]})
        self._pending = []

    def add_user_text(self, text: str) -> None:
        blocks: list[dict[str, Any]] = [
            _tool_result(ToolResult(call_id, NOT_EXECUTED, True)) for call_id in self._pending
        ]
        blocks.append({"type": "text", "text": text})
        self._messages.append({"role": "user", "content": blocks})
        self._pending = []


def _tool_result(result: ToolResult) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": result.call_id,
        "content": result.content,
        "is_error": result.is_error,
    }
