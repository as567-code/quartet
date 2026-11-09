"""The vendor-neutral tool loop (P2-R1): budget gate, validation, recording and repair.

The order of operations in ``run_step`` is binding. Every turn is recorded before it is acted
on; tool arguments are validated before a tool runs; repair prompts carry error kinds and field
paths only, never model text. The only broad exception handler in the codebase is at the
tool-executor boundary below. ``asyncio.CancelledError`` always propagates.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from quartet.db.sanitize import sanitize_for_storage
from quartet.llm.base import (
    MAX_ARTIFACT_REPAIRS,
    ArtifactInvalid,
    BudgetExceeded,
    CallRecord,
    LLMError,
    ModelRefused,
    PricingUnavailable,
    Provider,
    ProviderError,
    RoleConfig,
    RoleInput,
    RoleResult,
    StepRecorder,
    Toolbox,
    ToolCallRecord,
    ToolOutcome,
    ToolSpec,
    Usage,
    UsagePart,
)
from quartet.llm.budget import StepBudget
from quartet.llm.pricing import PricingTable
from quartet.llm.prompting import (
    PromptParts,
    build_prompt,
    cap_args,
    cap_tool_output,
    new_nonce,
    wrap_untrusted,
)
from quartet.llm.retry import RetryPolicy, call_with_retries
from quartet.telemetry.redaction import Redactor, redact_value
from quartet.telemetry.tracing import (
    annotate_llm_call,
    current_trace_id,
    llm_call_span,
    mark_error,
    role_span,
    tool_span,
)

REPAIR_TEMPLATE = (
    "Your previous reply was not a valid {name} object. Problems: {problems}. "
    "Reply again with only the corrected JSON object."
)
TRUNCATED_PROBLEM = "output truncated at the output token limit; reply more concisely"
MAX_PROBLEMS = 20
_SAFE_CALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


class StopKind(StrEnum):
    TOOL_USE = "tool_use"
    FINAL = "final"
    REFUSAL = "refusal"
    TRUNCATED = "truncated"


@dataclass(frozen=True, slots=True)
class ToolRequest:
    call_id: str
    name: str
    # None: the model's arguments were not a JSON object.
    arguments: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    content: str  # already capped and wrapped
    is_error: bool


@dataclass(frozen=True, slots=True)
class ProviderTurn:
    stop: StopKind
    tool_requests: tuple[ToolRequest, ...]
    final_text: str | None
    refusal_category: str | None
    model: str
    usage_parts: tuple[UsagePart, ...]
    stop_reason: str
    request_id: str | None


class ProviderAdapter(Protocol):
    """One conversation with one vendor; created fresh for every ``run()``."""

    provider: Provider

    def begin(self, config: RoleConfig, prompt: PromptParts, tools: Sequence[ToolSpec]) -> None: ...

    async def complete(self, *, timeout: float) -> ProviderTurn:
        """Make exactly one HTTP attempt; append the assistant output only on success."""
        ...

    def add_tool_results(self, results: Sequence[ToolResult]) -> None: ...

    def add_user_text(self, text: str) -> None:
        """Answer any still-pending tool calls as not executed, then add the text."""
        ...


@dataclass(frozen=True, slots=True)
class LoopDeps:
    pricing: PricingTable
    redactor: Redactor
    retry_policy: RetryPolicy
    clock: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    rand: Callable[[], float]


def _argument_problems(validator: Draft202012Validator, arguments: Mapping[str, Any] | None) -> str:
    """Paths and validator keywords only, never values; empty when the arguments are valid."""
    if arguments is None:
        return "$: type"
    found = sorted(
        ("$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in e.absolute_path))
        + f": {e.validator}"
        for e in validator.iter_errors(dict(arguments))
    )
    return "; ".join(found[:MAX_PROBLEMS])


def _artifact_problems(error: ValidationError) -> tuple[str, ...]:
    """``loc.path: error_type`` entries; never pydantic's ``input`` or ``msg``."""
    problems: list[str] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        loc = item["loc"]
        if item["type"] == "extra_forbidden":
            loc = loc[:-1]  # the last segment is a key the model chose
        path = ".".join(str(part) for part in loc) or "<root>"
        problems.append(f"{path}: {item['type']}")
        if len(problems) == MAX_PROBLEMS:
            break
    return tuple(problems)


class _Step:
    def __init__(
        self,
        adapter: ProviderAdapter,
        config: RoleConfig,
        toolbox: Toolbox,
        recorder: StepRecorder,
        deps: LoopDeps,
        validators: dict[str, Draft202012Validator],
        nonce: str,
    ) -> None:
        self.adapter = adapter
        self.config = config
        self.toolbox = toolbox
        self.recorder = recorder
        self.deps = deps
        self.validators = validators
        self.nonce = nonce
        self.budget = StepBudget(config.limits, clock=deps.clock)
        self.records: list[CallRecord] = []
        self.repairs = 0

    def _wall_clock(self) -> BudgetExceeded:
        return BudgetExceeded(
            "wall_clock",
            self.budget.totals().elapsed_seconds,
            self.budget.limits.max_wall_seconds,
        )

    def _gate(self) -> None:
        """The budget gate: before every HTTP attempt, retries included."""
        floor = (
            Decimal(self.budget.last_prompt_tokens)
            * self.deps.pricing.cache_read_rate(self.config.model)
            / Decimal(1_000_000)
        )
        self.budget.check_before_call(floor)

    async def drive(self) -> RoleResult:
        adapter = self.adapter
        while True:
            self._gate()
            turn = await self._call()
            if turn.stop is StopKind.REFUSAL:
                raise ModelRefused(provider=adapter.provider, category=turn.refusal_category)
            if turn.stop is StopKind.TOOL_USE:
                if not turn.tool_requests:
                    raise ProviderError("tool_use stop with no tool requests", retryable=False)
                await self._run_tools(turn.tool_requests)
                continue
            result = self._try_final(turn)
            if result is not None:
                return result

    async def _call(self) -> ProviderTurn:
        config, adapter, budget, deps = self.config, self.adapter, self.budget, self.deps
        timed_out = False
        provider_error: ProviderError | None = None
        budget_error: BudgetExceeded | None = None
        turn: ProviderTurn | None = None
        attempts = 1
        started = deps.clock()

        async def on_billed(billed: object) -> None:
            # A retryable failure that the vendor billed: it gets its own row now, and its
            # usage counts against the budget before the next attempt is allowed.
            if isinstance(billed, ProviderTurn):
                latency = max(0, int((deps.clock() - started) * 1000))
                await self._record(replace(billed, stop_reason="failed"), 1, latency, span)

        with llm_call_span(
            adapter.provider, config.model, config.role, config.max_output_tokens, config.effort
        ) as span:
            try:
                try:
                    async with asyncio.timeout(budget.remaining_seconds()) as cm:
                        turn, attempts = await call_with_retries(
                            lambda t: adapter.complete(timeout=t),
                            budget=budget,
                            policy=deps.retry_policy,
                            redactor=deps.redactor,
                            sleep=deps.sleep,
                            rand=deps.rand,
                            on_billed=on_billed,
                            before_retry=self._gate,
                        )
                except TimeoutError:
                    if not cm.expired():
                        raise
                    timed_out = True
            except ProviderError as err:
                provider_error = err
            except BudgetExceeded as err:
                budget_error = err
            latency_ms = max(0, int((deps.clock() - started) * 1000))
            if provider_error is not None:
                billed = provider_error.turn
                if isinstance(billed, ProviderTurn):
                    # The vendor answered but the turn is unusable; its usage is still owed.
                    # Retryable billed failures were recorded per attempt by ``on_billed``;
                    # this one is the final, non-retryable attempt: one row, attempts 1.
                    await self._record(billed, 1, latency_ms, span)
                mark_error(span, type(provider_error).__name__, provider_error.summary)
            elif budget_error is not None:
                mark_error(span, "BudgetExceeded", budget_error.kind)
            elif timed_out:
                # A cut-off call has unknown usage, so no llm_calls row is written.
                mark_error(span, "BudgetExceeded", "wall_clock")
            elif turn is not None:
                await self._record(turn, attempts, latency_ms, span)
        if provider_error is not None:
            raise provider_error
        if budget_error is not None:
            raise budget_error
        if timed_out or turn is None:
            raise self._wall_clock()
        unpriced = [p.model for p in turn.usage_parts if not deps.pricing.has(p.model)]
        if unpriced:
            raise PricingUnavailable(model=unpriced[0])
        return turn

    async def _record(self, turn: ProviderTurn, attempts: int, latency_ms: int, span: Any) -> None:
        deps = self.deps
        cost, estimated = deps.pricing.cost_or_ceiling(turn.usage_parts, self.adapter.provider)
        usage = sum((p.usage for p in turn.usage_parts), Usage.zero())
        record = CallRecord(
            provider=self.adapter.provider,
            requested_model=self.config.model,
            model=turn.model,
            usage=usage,
            cost_usd=cost,
            cost_estimated=estimated,
            latency_ms=latency_ms,
            stop_reason=turn.stop_reason,
            request_id=turn.request_id,
            trace_id=current_trace_id(),
            attempts=attempts,
        )
        self.budget.record_call(usage, cost)
        self.records.append(record)
        annotate_llm_call(span, record)
        await self.recorder.llm_call(record)

    async def _run_tools(self, requests: tuple[ToolRequest, ...]) -> None:
        self.budget.check_tool_calls(len(requests))
        results = [await self._run_tool(req) for req in requests]
        self.budget.record_tool_calls(len(requests))
        self.adapter.add_tool_results(results)

    async def _run_tool(self, req: ToolRequest) -> ToolResult:
        deps = self.deps
        outcome = ToolOutcome("", True)
        expired = False
        started = deps.clock()
        # Model-chosen identifiers never reach telemetry unless they are declared or well-formed.
        validator = self.validators.get(req.name)
        safe_name = req.name if validator is not None else "<unknown>"
        safe_id = req.call_id if _SAFE_CALL_ID.fullmatch(req.call_id) else "<invalid>"
        marked = False
        with tool_span(safe_name, safe_id) as span:
            problems = "" if validator is None else _argument_problems(validator, req.arguments)
            if validator is None:
                outcome = ToolOutcome("unknown tool", True)
            elif problems:
                outcome = ToolOutcome(f"invalid arguments: {problems}", True)
            else:
                try:
                    async with asyncio.timeout(self.budget.remaining_seconds()) as cm:
                        outcome = await self.toolbox.call(req.name, dict(req.arguments or {}))
                except Exception as exc:  # the one broad except: the tool-executor boundary
                    if cm.expired():
                        expired = True
                    else:
                        name = type(exc).__name__
                        outcome = ToolOutcome(f"{name}: {deps.redactor.redact(str(exc))}", True)
                        mark_error(span, name, name)
                        marked = True
            if expired:
                mark_error(span, "BudgetExceeded", "wall_clock")
            elif outcome.is_error and not marked:
                mark_error(span, "ToolError", "ToolError")
        if expired:
            raise self._wall_clock()
        duration_ms = max(0, int((deps.clock() - started) * 1000))
        args = cap_args(
            sanitize_for_storage(redact_value(dict(req.arguments or {}), deps.redactor))
        )
        await self.recorder.tool_call(
            ToolCallRecord(safe_name, safe_id, args, outcome.is_error, duration_ms)
        )
        content = wrap_untrusted("tool_output", cap_tool_output(outcome.content), nonce=self.nonce)
        return ToolResult(req.call_id, content, outcome.is_error)

    def _try_final(self, turn: ProviderTurn) -> RoleResult | None:
        """The artifact, or ``None`` after queueing a repair prompt."""
        if turn.stop is StopKind.TRUNCATED:
            problems: tuple[str, ...] = (TRUNCATED_PROBLEM,)
        else:
            try:
                artifact: BaseModel = self.config.output_model.model_validate_json(
                    turn.final_text or ""
                )
            except ValidationError as err:
                problems = _artifact_problems(err)
            else:
                return RoleResult(
                    artifact=artifact,
                    calls=tuple(self.records),
                    totals=self.budget.totals(),
                    repairs=self.repairs,
                    trace_id=current_trace_id(),
                )
        if self.repairs == MAX_ARTIFACT_REPAIRS:
            raise ArtifactInvalid(attempts=self.repairs + 1, problems=problems)
        self.repairs += 1
        self.adapter.add_user_text(
            REPAIR_TEMPLATE.format(
                name=self.config.output_model.__name__, problems="; ".join(problems)
            )
        )
        return None


async def run_step(
    adapter: ProviderAdapter,
    config: RoleConfig,
    role_input: RoleInput,
    toolbox: Toolbox,
    recorder: StepRecorder,
    deps: LoopDeps,
) -> RoleResult:
    nonce = new_nonce()
    prompt = build_prompt(config, role_input, nonce=nonce)
    specs = sorted(toolbox.specs, key=lambda s: s.name)
    if len({s.name for s in specs}) != len(specs):
        raise ValueError("duplicate tool names")
    validators = {s.name: Draft202012Validator(dict(s.input_schema)) for s in specs}
    adapter.begin(config, prompt, specs)
    step = _Step(adapter, config, toolbox, recorder, deps, validators, nonce)
    with role_span(config.role) as span:
        try:
            return await step.drive()
        except LLMError as err:
            failure = err
        failure.totals = step.budget.totals()
        failure.calls = tuple(step.records)
        mark_error(span, type(failure).__name__, failure.summary)
        raise failure
