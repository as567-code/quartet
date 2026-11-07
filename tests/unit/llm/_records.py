"""Shared builders for the llm tests; later tasks import and extend these."""

import time
from decimal import Decimal
from typing import Any

from quartet.domain.artifacts import Plan, PlanStep, PredictedFile
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
    Usage,
)
from quartet.llm.fake import FakeProvider, FakeTurn, Handler, ListRecorder
from quartet.llm.loop import LoopDeps, run_step
from quartet.llm.pricing import PricingTable
from quartet.llm.retry import RetryPolicy
from quartet.telemetry.redaction import Redactor


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
