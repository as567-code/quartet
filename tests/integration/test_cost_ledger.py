"""Cost ledger integration tests; they run as the DML-only ``quartet_app`` role."""

from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from quartet.db.models import LlmCall, RoleStep, Task, ToolCall
from quartet.domain.states import Role
from quartet.llm.base import (
    RecorderError,
    RoleInput,
    ToolCallRecord,
    ToolOutcome,
    Usage,
)
from quartet.llm.fake import FakeProvider, FakeTurn, ScriptedToolbox
from quartet.llm.loop import LoopDeps, run_step
from quartet.llm.pricing import PricingTable
from quartet.llm.retry import RetryPolicy
from quartet.telemetry.cost_ledger import DbStepRecorder, remaining_task_budget
from quartet.telemetry.redaction import Redactor
from tests.unit.llm._records import FakeClock, call_record, cfg, plan_json

pytestmark = pytest.mark.integration


@pytest.fixture
def task_and_step(app_engine, make_tasks):
    (task_id,) = make_tasks(n=1)
    with Session(app_engine) as s, s.begin():
        s.get_one(Task, task_id).budget_usd = Decimal("5")
        step = RoleStep(task_id=task_id, role=Role.PLANNER, round=0, attempt=0)
        s.add(step)
        s.flush()
        return task_id, step.id


def _count(session: Session, model: type) -> int:
    return session.execute(sa.select(sa.func.count()).select_from(model)).scalar_one()


@pytest.mark.anyio
async def test_llm_call_row_and_task_spend_in_one_transaction(app_engine, task_and_step):
    task_id, step_id = task_and_step
    usage = Usage(1_000, 300, 40, 60, 200, 50)
    rec = call_record(cost=Decimal("0.123456"), usage=usage)
    await DbStepRecorder(app_engine, task_id=task_id, step_id=step_id).llm_call(rec)
    with Session(app_engine) as s:
        row = s.execute(sa.select(LlmCall)).scalar_one()
        assert row.cost_usd == Decimal("0.123456") and row.requested_model == "claude-opus-5-5"
        assert row.step_id == step_id and row.provider == "anthropic"
        assert (row.tokens_in, row.tokens_out) == (1_000, 200)
        assert (row.cache_read_tokens, row.cache_write_tokens) == (300, 100)
        assert (row.reasoning_tokens, row.attempts, row.request_id) == (50, 1, "req_test")
        assert row.latency_ms == 1_200 and row.stop_reason == "end_turn"
        assert s.get_one(Task, task_id).spent_usd == Decimal("0.123456")


@pytest.mark.anyio
async def test_spend_accumulates_exact_decimals(app_engine, task_and_step):
    task_id, step_id = task_and_step
    rec = DbStepRecorder(app_engine, task_id=task_id, step_id=step_id)
    for _ in range(3):
        await rec.llm_call(call_record(cost=Decimal("0.000001")))
    with Session(app_engine) as s:
        assert s.get_one(Task, task_id).spent_usd == Decimal("0.000003")
        assert _count(s, LlmCall) == 3


@pytest.mark.anyio
async def test_unknown_task_rolls_back_the_insert(app_engine, task_and_step):
    _, step_id = task_and_step
    with pytest.raises(LookupError):
        await DbStepRecorder(app_engine, task_id=uuid4(), step_id=step_id).llm_call(call_record())
    with Session(app_engine) as s:
        assert _count(s, LlmCall) == 0


@pytest.mark.anyio
async def test_tool_call_row(app_engine, task_and_step):
    task_id, step_id = task_and_step
    rec = ToolCallRecord("read_file", "toolu_1", {"path": "a.py"}, True, 17)
    await DbStepRecorder(app_engine, task_id=task_id, step_id=step_id).tool_call(rec)
    with Session(app_engine) as s:
        row = s.execute(sa.select(ToolCall)).scalar_one()
        assert (row.step_id, row.tool, row.call_id) == (step_id, "read_file", "toolu_1")
        assert row.args_redacted == {"path": "a.py"} and row.is_error is True
        assert row.duration_ms == 17
        assert s.get_one(Task, task_id).spent_usd == Decimal(0)


@pytest.mark.anyio
async def test_cost_estimated_flag_is_stored(app_engine, task_and_step):
    task_id, step_id = task_and_step
    rec = DbStepRecorder(app_engine, task_id=task_id, step_id=step_id)
    await rec.llm_call(call_record(cost_estimated=True))
    await rec.llm_call(call_record())
    with Session(app_engine) as s:
        flags = s.execute(sa.select(LlmCall.cost_estimated).order_by(LlmCall.created_at)).scalars()
        assert sorted(flags) == [False, True]


@pytest.mark.anyio
async def test_database_error_becomes_a_content_free_recorder_error(app_engine, task_and_step):
    task_id, step_id = task_and_step
    rec = DbStepRecorder(app_engine, task_id=task_id, step_id=step_id)
    with pytest.raises(RecorderError) as info:
        await rec.llm_call(call_record(cost=Decimal("-1"), request_id="MARKER-row-text"))
    assert str(info.value) == "ledger write failed (SQLSTATE 23514)"
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert "MARKER" not in str(info.value)
    with Session(app_engine) as s:
        assert _count(s, LlmCall) == 0 and s.get_one(Task, task_id).spent_usd == Decimal(0)


@pytest.mark.anyio
async def test_run_step_through_the_db_recorder_writes_one_row_per_call(app_engine, task_and_step):
    """FakeProvider -> run_step -> DbStepRecorder, with NUL and a lone surrogate in tool args."""
    task_id, step_id = task_and_step

    async def read(_args):
        return ToolOutcome("contents", False)

    toolbox = ScriptedToolbox({"read_file": read})
    nasty = "a\x00b\ud800c"
    script = [
        FakeTurn(tool_calls=(("c1", "read_file", {"path": nasty}),)),
        FakeTurn(final=plan_json()),
    ]

    async def no_sleep(_s: float) -> None:
        return None

    deps = LoopDeps(
        PricingTable.load(), Redactor(), RetryPolicy(), FakeClock(), no_sleep, lambda: 0.5
    )
    recorder = DbStepRecorder(app_engine, task_id=task_id, step_id=step_id)
    result = await run_step(
        FakeProvider(script), cfg(), RoleInput("Plan it.", (), "t"), toolbox, recorder, deps
    )
    assert result.totals.turns == 2 and result.totals.cost_usd > 0
    with Session(app_engine) as s:
        assert _count(s, LlmCall) == 2 and _count(s, ToolCall) == 1
        assert s.get_one(Task, task_id).spent_usd == result.totals.cost_usd
        row = s.execute(sa.select(ToolCall)).scalar_one()
        assert row.args_redacted == {"path": "ab\ufffdc"} and row.call_id == "c1"


@pytest.mark.anyio
async def test_billed_attempts_each_get_a_row_and_the_spend_adds_up(app_engine, task_and_step):
    from dataclasses import replace

    from quartet.llm.base import ProviderError

    task_id, step_id = task_and_step

    class Billed(FakeProvider):
        async def complete(self, *, timeout):
            turn = await super().complete(timeout=timeout)
            if self.calls == 1:
                failed = replace(turn, stop_reason="failed")
                raise ProviderError("failed", retryable=True, code="server_error", turn=failed)
            return turn

    async def no_sleep(_s: float) -> None:
        return None

    deps = LoopDeps(
        PricingTable.load(), Redactor(), RetryPolicy(), FakeClock(), no_sleep, lambda: 0.5
    )
    recorder = DbStepRecorder(app_engine, task_id=task_id, step_id=step_id)
    fake = Billed([FakeTurn(), FakeTurn(final=plan_json())])
    result = await run_step(
        fake, cfg(), RoleInput("Plan it.", (), "t"), ScriptedToolbox({}), recorder, deps
    )
    with Session(app_engine) as s:
        rows = s.execute(sa.select(LlmCall).order_by(LlmCall.created_at)).scalars().all()
        assert [(r.stop_reason, r.attempts) for r in rows] == [("failed", 1), ("end_turn", 2)]
        assert s.get_one(Task, task_id).spent_usd == result.totals.cost_usd
        assert result.totals.cost_usd == sum((r.cost_usd for r in rows), Decimal(0))


def test_remaining_task_budget(app_engine, app_session, task_and_step):
    task_id, _ = task_and_step
    with Session(app_engine) as s, s.begin():
        s.get_one(Task, task_id).spent_usd = Decimal("1.25")
    assert remaining_task_budget(app_session, task_id) == Decimal("3.750000")


def test_remaining_task_budget_floors_at_zero_and_rejects_unknown(
    app_engine, app_session, task_and_step
):
    task_id, _ = task_and_step
    with Session(app_engine) as s, s.begin():
        s.get_one(Task, task_id).spent_usd = Decimal("7")
    assert remaining_task_budget(app_session, task_id) == Decimal(0)
    with pytest.raises(LookupError):
        remaining_task_budget(app_session, uuid4())


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "google"},
        {"tokens_in": -1},
        {"tokens_out": -1},
        {"cache_read_tokens": -1},
        {"cache_write_tokens": -1},
        {"reasoning_tokens": -1},
        {"cost_usd": Decimal("-0.000001")},
    ],
    ids=lambda o: next(iter(o)),
)
def test_constraints_reject_bad_provider_and_negative_tokens(app_session, task_and_step, overrides):
    _, step_id = task_and_step
    fields = {
        "step_id": step_id,
        "provider": "anthropic",
        "requested_model": "m",
        "model": "m",
        **overrides,
    }
    app_session.add(LlmCall(**fields))
    with pytest.raises(sa.exc.IntegrityError):
        app_session.flush()
