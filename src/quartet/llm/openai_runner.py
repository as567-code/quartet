"""OpenAI adapter: streamed Responses API, strict tools and format, ``store=False`` (P2-R8).

One ``OpenAIAdapter`` is one conversation. The transcript is kept client-side: every output item
(reasoning items with their ``encrypted_content`` included) is appended exactly as the API
returned it. The adapter reads the raw event stream rather than the ``responses.stream()``
helper, because the helper ``json.loads`` strict-tool arguments and would crash on a malformed
tool call instead of letting the loop answer it with ``is_error``. It makes exactly one HTTP
attempt per ``complete``; retries belong to ``llm/retry.py``. SDK exceptions propagate unchanged
for the retry layer to classify.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast

import httpx2
import openai
from openai import AsyncStream
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseIncompleteEvent,
    ResponseStreamEvent,
)
from pydantic import BaseModel, SecretStr

from quartet.domain.artifacts import ARTIFACT_KINDS, strict_json_schema
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

TRANSIENT_ERROR_CODES = frozenset({"server_error", "rate_limit_exceeded", "vector_store_timeout"})

NOT_EXECUTED = "not executed: the turn ended before this tool could run"
_SAFE_CODE = re.compile(r"[a-z_]{1,40}")


def make_openai_client(
    api_key: SecretStr, *, http_client: httpx2.AsyncClient | None = None
) -> openai.AsyncOpenAI:
    """No SDK retries: ``llm/retry.py`` is the only retry layer."""
    return openai.AsyncOpenAI(
        api_key=api_key.get_secret_value(),
        max_retries=0,
        http_client=http_client or openai.DefaultAsyncHttpx2Client(),
    )


def schema_format_name(model: type[BaseModel]) -> str:
    """The artifact kind (``review_report``), or ``context_digest`` for the digest."""
    return next((kind for kind, cls in ARTIFACT_KINDS.items() if cls is model), "context_digest")


def _count(obj: Any, name: str) -> int:
    value = getattr(obj, name, None)
    return value if isinstance(value, int) else 0


def usage_from_openai(usage: Any) -> Usage:
    """Normalise usage. ``input_tokens`` includes cache reads and writes; fields may be None."""
    details = getattr(usage, "input_tokens_details", None)
    cached = _count(details, "cached_tokens")
    writes = _count(details, "cache_write_tokens")
    split = getattr(details, "cache_write_tokens_details", None)
    if split is None:
        short, long = writes, 0
    else:
        short, long = _count(split, "ttl_30m_tokens"), _count(split, "ttl_12h_tokens")
    return Usage(
        uncached_input_tokens=max(0, _count(usage, "input_tokens") - cached - writes),
        cache_read_tokens=cached,
        cache_write_short_tokens=short,
        cache_write_long_tokens=long,
        output_tokens=_count(usage, "output_tokens"),
        reasoning_tokens=_count(getattr(usage, "output_tokens_details", None), "reasoning_tokens"),
    )


def _arguments(raw: Any) -> dict[str, Any] | None:
    """Parse a function call's argument string; anything but a JSON object becomes None."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _safe_code(code: Any) -> str | None:
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else None


def turn_from_response(response: Any, requested_model: str) -> ProviderTurn:
    """Map a terminal response to a ``ProviderTurn``; unusable outcomes raise a billed error."""
    del requested_model  # the served model is response.model; the loop records both
    status = response.status or ""
    details = getattr(response, "incomplete_details", None)
    reason = getattr(details, "reason", None) or ""
    output = list(response.output)
    requests = tuple(
        ToolRequest(item.call_id, item.name, _arguments(item.arguments))
        for item in output
        if item.type == "function_call"
    )
    parts_in_messages = [part for item in output if item.type == "message" for part in item.content]
    refused = any(part.type == "refusal" for part in parts_in_messages)
    text = "".join(part.text for part in parts_in_messages if part.type == "output_text")
    usage_parts = (
        (UsagePart(response.model, usage_from_openai(response.usage)),)
        if response.usage is not None
        else ()
    )

    def turn(kind: StopKind, stop_reason: str, **fields: Any) -> ProviderTurn:
        return ProviderTurn(
            stop=kind,
            tool_requests=fields.get("tool_requests", ()),
            final_text=fields.get("final_text"),
            refusal_category=fields.get("refusal_category"),
            model=response.model,
            usage_parts=usage_parts,
            stop_reason=stop_reason,
            request_id=None,
        )

    if status == "completed":
        if requests:
            return turn(StopKind.TOOL_USE, status, tool_requests=requests)
        if refused:
            return turn(StopKind.REFUSAL, status)
        return turn(StopKind.FINAL, status, final_text=text)
    if status == "incomplete":
        if reason == "content_filter":
            return turn(StopKind.REFUSAL, reason, refusal_category="content_filter")
        if reason == "max_output_tokens":
            return turn(StopKind.TRUNCATED, reason)
        shown = _safe_code(reason) or "<invalid>"
        raise ProviderError(
            f"unsupported incomplete reason: {shown}",
            retryable=False,
            turn=turn(StopKind.TRUNCATED, shown) if usage_parts else None,
        )
    if status == "failed":
        code = _safe_code(getattr(response.error, "code", None))
        raise ProviderError(
            f"response failed: {code or '<unknown>'}",
            retryable=code in TRANSIENT_ERROR_CODES,
            code=code,
            turn=turn(StopKind.TRUNCATED, "failed") if usage_parts else None,
        )
    shown = _safe_code(status) or "<invalid>"
    raise ProviderError(f"unsupported response status: {shown}", retryable=False)


class OpenAIAdapter(ProviderAdapter):
    provider = Provider.OPENAI

    def __init__(self, client: openai.AsyncOpenAI) -> None:
        self._client = client
        self._config: RoleConfig | None = None
        self._request: dict[str, Any] = {}
        self._input: list[dict[str, Any]] = []
        self._pending: list[str] = []

    def begin(self, config: RoleConfig, prompt: PromptParts, tools: Sequence[ToolSpec]) -> None:
        request: dict[str, Any] = {
            "model": config.model,
            "instructions": prompt.system,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_format_name(config.output_model),
                    "schema": strict_json_schema(config.output_model),
                    "strict": True,
                }
            },
            "reasoning": {"effort": config.effort},
            "max_output_tokens": config.max_output_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": dict(t.input_schema),
                    "strict": True,
                }
                for t in sorted(tools, key=lambda t: t.name)
            ]
        self._config = config
        self._request = request
        self._input = [{"role": "user", "content": [{"type": "input_text", "text": prompt.user}]}]
        self._pending = []

    async def complete(self, *, timeout: float) -> ProviderTurn:
        if self._config is None:
            raise RuntimeError("begin() must be called before complete()")
        # **kwargs defeats overload resolution: the call returns the raw stream.
        stream = cast(
            "AsyncStream[ResponseStreamEvent]",
            await self._client.responses.create(
                stream=True, **{**self._request, "input": self._input}, timeout=timeout
            ),
        )
        request_id = stream.response.headers.get("x-request-id")
        response: Any = None
        try:
            async for event in stream:
                if isinstance(event, ResponseErrorEvent):
                    code = _safe_code(event.code)
                    raise ProviderError(
                        f"stream error: {code or '<unknown>'}",
                        retryable=code in TRANSIENT_ERROR_CODES,
                        code=code,
                        request_id=request_id,
                    )
                if isinstance(
                    event, ResponseCompletedEvent | ResponseIncompleteEvent | ResponseFailedEvent
                ):
                    response = event.response
                    break
        finally:
            await stream.close()
        if response is None:
            raise ProviderError(
                "stream ended without a terminal event", retryable=True, request_id=request_id
            )
        try:
            turn = turn_from_response(response, self._config.model)
        except ProviderError as err:
            if isinstance(err.turn, ProviderTurn):
                err.turn = replace(err.turn, request_id=request_id)
            err.request_id = request_id
            raise
        self._input.extend(item.to_dict() for item in response.output)
        self._pending = [i.call_id for i in response.output if i.type == "function_call"]
        return replace(turn, request_id=request_id)

    def add_tool_results(self, results: Sequence[ToolResult]) -> None:
        self._input.extend(_tool_output(r) for r in results)
        self._pending = []

    def add_user_text(self, text: str) -> None:
        self._input.extend(
            _tool_output(ToolResult(call_id, NOT_EXECUTED, True)) for call_id in self._pending
        )
        self._input.append({"role": "user", "content": [{"type": "input_text", "text": text}]})
        self._pending = []


def _tool_output(result: ToolResult) -> dict[str, Any]:
    output = (
        json.dumps({"is_error": True, "content": result.content})
        if result.is_error
        else result.content
    )
    return {"type": "function_call_output", "call_id": result.call_id, "output": output}
