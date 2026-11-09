"""SSE encoding and message builders for the Anthropic and OpenAI streaming wire formats.

``anthropic_sse`` and ``openai_sse`` turn a complete message or response dict into the event
stream the SDK parses, so tests and the Task 15 cassettes replay through the same code path as
real traffic.
"""

import json
from types import SimpleNamespace
from typing import Any, cast

from anthropic.types.beta import (
    BetaFallbackMessageIterationUsage,
    BetaMessage,
    BetaMessageIterationUsage,
    BetaTextBlock,
    BetaUsage,
)
from openai.types.responses import Response

MODEL = "claude-opus-5-5"
DEFAULT_USAGE: dict[str, Any] = {
    "input_tokens": 100,
    "cache_read_input_tokens": 0,
    "cache_creation_input_tokens": 0,
    "output_tokens": 20,
}


def _event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n".encode()


def _start_block(block: dict[str, Any]) -> dict[str, Any]:
    kind = block["type"]
    if kind == "text":
        return {"type": "text", "text": ""}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
    if kind == "thinking":
        return {"type": "thinking", "thinking": "", "signature": ""}
    raise ValueError(f"unsupported block type: {kind}")


def _deltas(block: dict[str, Any]) -> list[dict[str, Any]]:
    kind = block["type"]
    if kind == "text":
        return [{"type": "text_delta", "text": block["text"]}]
    if kind == "tool_use":
        return [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
    return [
        {"type": "thinking_delta", "thinking": block["thinking"]},
        {"type": "signature_delta", "signature": block["signature"]},
    ]


def anthropic_sse(message: dict[str, Any]) -> bytes:
    usage = message.get("usage") or DEFAULT_USAGE
    start = {
        **message,
        "content": [],
        "stop_reason": None,
        "stop_details": None,
        "usage": {**usage, "output_tokens": 0, "iterations": None},
    }
    out = [_event("message_start", {"message": start})]
    for index, block in enumerate(message["content"]):
        out.append(
            _event("content_block_start", {"index": index, "content_block": _start_block(block)})
        )
        for delta in _deltas(block):
            out.append(_event("content_block_delta", {"index": index, "delta": delta}))
        out.append(_event("content_block_stop", {"index": index}))
    delta = {
        "stop_reason": message["stop_reason"],
        "stop_sequence": None,
        "stop_details": message.get("stop_details"),
    }
    out.append(_event("message_delta", {"delta": delta, "usage": usage}))
    out.append(_event("message_stop", {}))
    return b"".join(out)


def _message(
    content: list[dict[str, Any]], stop_reason: str, usage: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": None,
        "usage": usage or dict(DEFAULT_USAGE),
    }


def text_message(
    text: str, *, stop_reason: str = "end_turn", usage: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _message([{"type": "text", "text": text}], stop_reason, usage)


def tool_use_message(
    *calls: tuple[str, str, dict[str, Any]],
    thinking_signature: str | None = None,
    stop_reason: str = "tool_use",
) -> dict[str, Any]:
    """Each call is ``(id, name, input)``."""
    content: list[dict[str, Any]] = []
    if thinking_signature is not None:
        content.append({"type": "thinking", "thinking": "hmm", "signature": thinking_signature})
    content += [{"type": "tool_use", "id": i, "name": n, "input": a} for i, n, a in calls]
    return _message(content, stop_reason, None)


def beta_message(
    *, model: str, stop_reason: str, text: str, iterations: list[dict[str, Any]]
) -> BetaMessage:
    """A ``BetaMessage`` whose usage carries ``iterations`` (built without validation)."""
    built: list[Any] = []
    for entry in iterations:
        fields = {"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, **entry}
        cls = (
            BetaFallbackMessageIterationUsage
            if fields["type"] == "fallback_message"
            else BetaMessageIterationUsage
        )
        built.append(cls.model_construct(**fields))
    usage = BetaUsage.model_construct(input_tokens=0, output_tokens=0, iterations=built)
    return BetaMessage.model_construct(
        id="msg_test",
        type="message",
        role="assistant",
        model=model,
        content=[BetaTextBlock.model_construct(type="text", text=text)],
        stop_reason=cast(Any, stop_reason),
        stop_details=cast(Any, SimpleNamespace(category=None)),
        usage=usage,
    )


ns = SimpleNamespace
OPENAI_MODEL = "gpt-6.1-sol"


def openai_sse(response: dict[str, Any] | Response, terminal: str = "response.completed") -> bytes:
    """``response.created`` (in progress, no output), then the terminal event with the response.

    The SDK's accumulator needs no ``response.output_item.*`` events when the terminal event
    carries the whole ``output``.
    """
    if isinstance(response, Response):
        response = response.to_dict(mode="json")
    if terminal == "response.completed" and response.get("status") in ("incomplete", "failed"):
        terminal = f"response.{response['status']}"  # the event type follows the status
    created = {**response, "status": "in_progress", "output": [], "usage": None, "error": None}
    created["incomplete_details"] = None
    return _event("response.created", {"response": created, "sequence_number": 0}) + _event(
        terminal, {"response": response, "sequence_number": 1}
    )


def usage(input: int, cached: int, writes: int, output: int, reasoning: int = 0) -> dict[str, Any]:
    """OpenAI usage; all cache writes are reported as 30-minute writes."""
    return {
        "input_tokens": input,
        "input_tokens_details": {
            "cached_tokens": cached,
            "cache_write_tokens": writes,
            "cache_write_tokens_details": {"ttl_30m_tokens": writes, "ttl_12h_tokens": 0},
        },
        "output_tokens": output,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": input + output,
    }


def resp(
    *,
    status: str = "completed",
    reason: str | None = None,
    output: list[dict[str, Any]],
    usage: dict[str, Any] | None,
    error_code: str | None = None,
) -> Response:
    """A validated ``Response``, so adapter code sees the same types as on the wire."""
    return Response.model_validate(
        {
            "id": "resp_test",
            "object": "response",
            "created_at": 1.0,
            "model": OPENAI_MODEL,
            "status": status,
            "incomplete_details": {"reason": reason} if reason else None,
            "error": {"code": error_code, "message": "boom"} if error_code else None,
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": usage,
        }
    )


def reasoning_item() -> dict[str, Any]:
    return {"type": "reasoning", "id": "rs_1", "encrypted_content": "enc", "summary": []}


def function_call_item(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


def message_item(*parts: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": list(parts),
    }


def text_part(text: str) -> dict[str, Any]:
    return {"type": "output_text", "text": text, "annotations": []}


def refusal_part(text: str = "no") -> dict[str, Any]:
    return {"type": "refusal", "refusal": text}
