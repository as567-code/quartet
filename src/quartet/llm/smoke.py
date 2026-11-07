"""Live smoke checks: every artifact schema is accepted by each vendor, and tools + structured
output work together in one request. These spend real money, so spending is capped."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from quartet.domain.artifacts import ARTIFACT_MODELS, ContextDigest
from quartet.domain.states import Role
from quartet.llm.base import (
    EMPTY_TOOLBOX,
    NULL_RECORDER,
    LLMError,
    Provider,
    RoleConfig,
    RoleInput,
    RoleRunner,
    StepLimits,
    Toolbox,
    ToolOutcome,
    ToolSpec,
)

SMOKE_LIMITS = StepLimits(
    max_turns=4,
    max_tool_calls=2,
    max_total_tokens=60_000,
    max_usd=Decimal("0.25"),
    max_wall_seconds=240.0,
)
SMOKE_MODELS = {Provider.ANTHROPIC: "claude-opus-5-5", Provider.OPENAI: "gpt-6.1-sol"}

SYSTEM_PROMPT = "You produce small example objects for a schema compatibility test."
SCHEMA_INSTRUCTIONS = (
    "Return a minimal valid example object. Keep every text field under 80 characters "
    "and every list at most 2 items long."
)
TOOL_INSTRUCTIONS = (
    'Call the echo tool exactly once with text "ping". Then return an object whose goal is '
    "exactly the text the tool returned, without any markup."
)
TASK_TEXT = "schema smoke test"
TOOL_CHECK = "tool_round_trip"
SKIPPED = "skipped: smoke budget reached"

_ECHO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


class EchoToolbox(Toolbox):
    """One tool, ``echo``, that returns its text and remembers every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._specs = [ToolSpec("echo", "Return the given text unchanged.", _ECHO_SCHEMA)]

    @property
    def specs(self) -> Sequence[ToolSpec]:
        return self._specs

    async def call(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        text = str(arguments.get("text", ""))
        self.calls.append(text)
        return ToolOutcome(text, False)


@dataclass(frozen=True, slots=True)
class SmokeResult:
    provider: Provider
    check: str
    ok: bool
    cost_usd: Decimal
    detail: str


def _config(provider: Provider, output_model: type[Any], limits: StepLimits) -> RoleConfig:
    return RoleConfig(
        role=Role.ORCHESTRATOR,
        provider=provider,
        model=SMOKE_MODELS[provider],
        effort="low",
        system_prompt=SYSTEM_PROMPT,
        output_model=output_model,
        max_output_tokens=8_000,
        limits=limits,
        server_fallback=False,
    )


async def _check(
    runner: RoleRunner,
    provider: Provider,
    name: str,
    output_model: type[Any],
    tool_check: bool,
    limits: StepLimits,
) -> SmokeResult:
    toolbox = EchoToolbox() if tool_check else None
    instructions = TOOL_INSTRUCTIONS if tool_check else SCHEMA_INSTRUCTIONS
    try:
        result = await runner.run(
            _config(provider, output_model, limits),
            RoleInput(instructions, (), TASK_TEXT),
            toolbox if toolbox is not None else EMPTY_TOOLBOX,
            recorder=NULL_RECORDER,
        )
    except (LLMError, ValueError) as err:
        cost = err.totals.cost_usd if isinstance(err, LLMError) and err.totals else Decimal(0)
        return SmokeResult(provider, name, False, cost, f"{type(err).__name__}: {err}")
    cost = result.totals.cost_usd
    if toolbox is not None:
        goal = result.artifact.goal if isinstance(result.artifact, ContextDigest) else None
        ok = toolbox.calls == ["ping"] and goal is not None and goal.strip() == "ping"
        detail = "ok" if ok else f"echo calls={len(toolbox.calls)}, goal mismatch or tool misuse"
    else:
        ok = result.repairs == 0
        detail = "ok" if ok else f"needed {result.repairs} repair(s)"
    return SmokeResult(provider, name, ok, cost, detail)


async def run_smoke(
    runners: Mapping[Provider, RoleRunner], *, max_usd: Decimal
) -> list[SmokeResult]:
    results: list[SmokeResult] = []
    spent = Decimal(0)
    for provider, runner in runners.items():
        checks: list[tuple[str, type[Any], bool]] = [
            (model.__name__, model, False) for model in ARTIFACT_MODELS
        ]
        checks.append((TOOL_CHECK, ContextDigest, True))
        for name, output_model, tool_check in checks:
            if spent >= max_usd:
                results.append(SmokeResult(provider, name, False, Decimal(0), SKIPPED))
                continue
            limits = replace(SMOKE_LIMITS, max_usd=min(SMOKE_LIMITS.max_usd, max_usd - spent))
            result = await _check(runner, provider, name, output_model, tool_check, limits)
            spent += result.cost_usd
            results.append(result)
    return results
