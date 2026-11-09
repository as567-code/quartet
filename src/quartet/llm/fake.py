"""Deterministic in-memory adapter, recorder and toolbox for tests (no network)."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from quartet.domain.artifacts import strictify_schema
from quartet.llm.base import (
    CallRecord,
    Provider,
    RoleConfig,
    StepRecorder,
    Toolbox,
    ToolCallRecord,
    ToolOutcome,
    ToolSpec,
    Usage,
    UsagePart,
)
from quartet.llm.loop import ProviderAdapter, ProviderTurn, StopKind, ToolRequest, ToolResult
from quartet.llm.prompting import PromptParts

DEFAULT_USAGE = Usage(1_000, 0, 0, 0, 200, 0)


@dataclass(frozen=True, slots=True)
class FakeTurn:
    tool_calls: tuple[tuple[str, str, Mapping[str, Any] | None], ...] = ()
    final: str | None = None
    refused: bool = False
    refusal_category: str | None = None
    truncated: bool = False
    usage: Usage = DEFAULT_USAGE
    model: str | None = None
    raises: Exception | None = None


class FakeProvider(ProviderAdapter):
    def __init__(self, script: Sequence[FakeTurn], provider: Provider = Provider.ANTHROPIC) -> None:
        self.provider = provider
        self._script = list(script)
        self.calls = 0
        self.timeouts: list[float] = []
        self.tool_results: list[list[ToolResult]] = []
        self.user_texts: list[str] = []
        self.began: tuple[RoleConfig, PromptParts, tuple[ToolSpec, ...]] | None = None

    def begin(self, config: RoleConfig, prompt: PromptParts, tools: Sequence[ToolSpec]) -> None:
        self.began = (config, prompt, tuple(tools))

    async def complete(self, *, timeout: float) -> ProviderTurn:
        if self.calls >= len(self._script):
            raise AssertionError(f"FakeProvider script exhausted after {self.calls} calls")
        scripted = self._script[self.calls]
        self.calls += 1
        self.timeouts.append(timeout)
        if scripted.raises is not None:
            raise scripted.raises
        model = scripted.model or (self.began[0].model if self.began else "fake-model")
        requests: tuple[ToolRequest, ...] = ()
        if scripted.refused:
            stop, reason = StopKind.REFUSAL, "refusal"
        elif scripted.truncated:
            stop, reason = StopKind.TRUNCATED, "max_tokens"
        elif scripted.tool_calls:
            stop, reason = StopKind.TOOL_USE, "tool_use"
            requests = tuple(ToolRequest(i, n, a) for i, n, a in scripted.tool_calls)
        else:
            stop, reason = StopKind.FINAL, "end_turn"
        return ProviderTurn(
            stop=stop,
            tool_requests=requests,
            final_text=scripted.final,
            refusal_category=scripted.refusal_category,
            model=model,
            usage_parts=(UsagePart(model, scripted.usage),),
            stop_reason=reason,
            request_id=f"req_fake_{self.calls}",
        )

    def add_tool_results(self, results: Sequence[ToolResult]) -> None:
        self.tool_results.append(list(results))

    def add_user_text(self, text: str) -> None:
        self.user_texts.append(text)


class ListRecorder(StepRecorder):
    def __init__(self) -> None:
        self.llm: list[CallRecord] = []
        self.tools: list[ToolCallRecord] = []

    async def llm_call(self, record: CallRecord) -> None:
        self.llm.append(record)

    async def tool_call(self, record: ToolCallRecord) -> None:
        self.tools.append(record)


_PATH_SCHEMA = strictify_schema(
    {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
)

Handler = Callable[[dict[str, Any]], Awaitable[ToolOutcome]]


class ScriptedToolbox(Toolbox):
    """Handlers by tool name; every tool takes one required string ``path``."""

    def __init__(self, handlers: Mapping[str, Handler]) -> None:
        self._handlers = dict(handlers)
        self._specs = [
            ToolSpec(name, f"fake {name}", _PATH_SCHEMA) for name in sorted(self._handlers)
        ]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def specs(self) -> Sequence[ToolSpec]:
        return self._specs

    async def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        args = dict(arguments)
        self.calls.append((name, args))
        return await self._handlers[name](args)
