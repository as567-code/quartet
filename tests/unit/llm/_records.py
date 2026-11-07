"""Shared builders for the llm tests; later tasks import and extend these."""

import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from quartet.domain.artifacts import ContextDigest, Plan, PlanStep, PredictedFile
from quartet.domain.states import Role
from quartet.llm.base import (
    EMPTY_TOOLBOX,
    CallRecord,
    Provider,
    RoleConfig,
    RoleInput,
    RoleResult,
    StepLimits,
    Toolbox,
    ToolOutcome,
    ToolSpec,
    Usage,
)
from quartet.llm.catalog import ModelCatalog
from quartet.llm.fake import FakeProvider, FakeTurn, Handler, ListRecorder
from quartet.llm.loop import LoopDeps, ProviderAdapter, ProviderTurn, ToolResult, run_step
from quartet.llm.pricing import PricingTable
from quartet.llm.prompting import PromptParts
from quartet.llm.retry import RetryPolicy
from quartet.llm.runner import ProviderRoleRunner
from quartet.telemetry.redaction import Redactor

ROLE_INPUT = RoleInput("Plan it.", (), "t")


def limits(**overrides: Any) -> StepLimits:
    base: dict[str, Any] = {
        "max_turns": 4,
        "max_tool_calls": 4,
        "max_total_tokens": 50_000,
        "max_usd": Decimal("1.00"),
        "max_wall_seconds": 60.0,
    }
    return StepLimits(**{**base, **overrides})


def cfg(**overrides: Any) -> RoleConfig:
    base: dict[str, Any] = {
        "role": Role.PLANNER,
        "provider": Provider.ANTHROPIC,
        "model": "claude-opus-5-5",
        "effort": "high",
        "system_prompt": "You are the planner.",
        "output_model": Plan,
        "max_output_tokens": 8_000,
        "limits": limits(),
        "server_fallback": False,
    }
    return RoleConfig(**{**base, **overrides})


def call_record(*, cost: Decimal = Decimal("0.010000"), **overrides: Any) -> CallRecord:
    base: dict[str, Any] = {
        "provider": Provider.ANTHROPIC,
        "requested_model": "claude-opus-5-5",
        "model": "claude-opus-5-5",
        "usage": Usage(1_000, 0, 0, 0, 200, 0),
        "cost_usd": cost,
        "cost_estimated": False,
        "latency_ms": 1_200,
        "stop_reason": "end_turn",
        "request_id": "req_test",
        "trace_id": None,
        "attempts": 1,
    }
    return CallRecord(**{**base, **overrides})


def plan_artifact(**fields: Any) -> Plan:
    """Same values as the root ``sample_plan`` fixture, with optional field overrides."""
    base: dict[str, Any] = {
        "schema_version": 1,
        "summary": "Add a greeting helper",
        "steps": [PlanStep(description="Write greet()", files=["src/greet.py"])],
        "predicted_files": [PredictedFile(path_glob="src/greet.py", mode="write")],
        "acceptance_criteria": ["greet('x') returns 'hello x'"],
        "test_strategy": "unit test greet()",
        "risk_level": "low",
        "out_of_scope_reason": None,
    }
    return Plan(**{**base, **fields})


def plan_json(**fields: Any) -> str:
    return plan_artifact(**fields).model_dump_json()


class FakeClock:
    """Callable monotonic clock whose time is set by the test."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def ok(text: str) -> Handler:
    async def handler(_args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(text, False)

    return handler


async def _no_sleep(_seconds: float) -> None:
    return None


_limits = limits


async def step(
    script: list[FakeTurn],
    *,
    toolbox: Toolbox = EMPTY_TOOLBOX,
    limits: StepLimits | None = None,
    recorder: ListRecorder | None = None,
    task_text: str = "t",
    real_clock: bool = False,
) -> tuple[RoleResult, FakeProvider, ListRecorder]:
    fake = FakeProvider(script)
    rec = recorder if recorder is not None else ListRecorder()
    deps = LoopDeps(
        pricing=PricingTable.load(),
        redactor=Redactor(),
        retry_policy=RetryPolicy(),
        clock=time.monotonic if real_clock else FakeClock(),
        sleep=_no_sleep,
        rand=lambda: 0.5,
    )
    config = cfg(limits=limits if limits is not None else _limits())
    result = await run_step(fake, config, RoleInput("Plan it.", (), task_text), toolbox, rec, deps)
    return result, fake, rec


async def _noop_sleep(_seconds: float) -> None:
    return None


def runner(factory: Callable[[], ProviderAdapter]) -> ProviderRoleRunner:
    """An Anthropic runner over real pricing and catalog, with a no-op sleep."""
    return ProviderRoleRunner(
        factory,
        provider=Provider.ANTHROPIC,
        pricing=PricingTable.load(),
        catalog=ModelCatalog.load(),
        redactor=Redactor(),
        sleep=_noop_sleep,
    )


def _minimal(node: Any, defs: Mapping[str, Any]) -> Any:
    """The smallest JSON value that satisfies a pydantic-generated schema."""
    if "$ref" in node:
        return _minimal(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return node["enum"][0]
    if "anyOf" in node:
        options = node["anyOf"]
        null = [o for o in options if o.get("type") == "null"]
        return None if null else _minimal(options[0], defs)
    kind = node["type"]
    if kind == "object":
        return {k: _minimal(v, defs) for k, v in node.get("properties", {}).items()}
    scalars: dict[str, Any] = {"string": "x", "integer": 0, "number": 0, "boolean": False}
    return [] if kind == "array" else scalars[kind]


def minimal_instance_json(model: type[BaseModel]) -> str:
    schema = model.model_json_schema()
    instance = _minimal(schema, schema.get("$defs", {}))
    return model.model_validate(instance).model_dump_json()


class SmokeFake(ProviderAdapter):
    """Answers each smoke check correctly, billing exactly ``cost_per_check`` per check."""

    def __init__(self, provider: Provider, pricing: PricingTable, cost_per_check: Decimal) -> None:
        self.provider = provider
        self._pricing = pricing
        self._cost = cost_per_check
        self._inner = FakeProvider([], provider)

    def _output_tokens(self, model: str) -> int:
        tokens = self._cost * 1_000_000 / self._pricing.price(model).standard.output
        if tokens != int(tokens):
            raise AssertionError(f"{self._cost} is not a whole number of output tokens")
        return int(tokens)

    def begin(self, config: RoleConfig, prompt: PromptParts, tools: Sequence[ToolSpec]) -> None:
        total = self._output_tokens(config.model)
        final_text = minimal_instance_json(config.output_model)
        if any(t.name == "echo" for t in tools):
            first = total // 2
            digest = ContextDigest(
                schema_version=1, goal="ping", constraints=[], open_questions=[], artifact_ids=[]
            )
            script = [
                FakeTurn(
                    tool_calls=(("call_1", "echo", {"text": "ping"}),),
                    usage=Usage(0, 0, 0, 0, first, 0),
                ),
                FakeTurn(final=digest.model_dump_json(), usage=Usage(0, 0, 0, 0, total - first, 0)),
            ]
        else:
            script = [FakeTurn(final=final_text, usage=Usage(0, 0, 0, 0, total, 0))]
        self._inner = FakeProvider(script, self.provider)
        self._inner.begin(config, prompt, tools)

    async def complete(self, *, timeout: float) -> ProviderTurn:
        return await self._inner.complete(timeout=timeout)

    def add_tool_results(self, results: Sequence[ToolResult]) -> None:
        self._inner.add_tool_results(results)

    def add_user_text(self, text: str) -> None:
        self._inner.add_user_text(text)


def scripted_runner(
    provider: Provider, *, cost_per_check: Decimal = Decimal("0.01")
) -> ProviderRoleRunner:
    pricing = PricingTable.load()
    return ProviderRoleRunner(
        lambda: SmokeFake(provider, pricing, cost_per_check),
        provider=provider,
        pricing=pricing,
        catalog=ModelCatalog.load(),
        redactor=Redactor(),
        sleep=_noop_sleep,
    )
