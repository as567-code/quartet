from decimal import Decimal

import pytest

from quartet.llm.base import BudgetExceeded, Usage
from quartet.llm.budget import StepBudget
from tests.unit.llm._records import FakeClock, limits


def test_first_call_allowed_without_output_reservation():  # P2-R6
    StepBudget(limits(max_usd=Decimal("0.50"))).check_before_call(Decimal("0"))


def test_turn_cap():
    b = StepBudget(limits(max_turns=1))
    b.record_call(Usage(10, 0, 0, 0, 1, 0), Decimal("0"))
    with pytest.raises(BudgetExceeded) as e:
        b.check_before_call(Decimal("0"))
    assert e.value.kind == "turns"


def test_usd_floor_blocks_next_call_and_one_call_overshoot_is_allowed():
    b = StepBudget(limits(max_usd=Decimal("0.50")))
    b.check_before_call(Decimal("0"))
    b.record_call(Usage(1, 0, 0, 0, 1, 0), Decimal("0.70"))  # overshoot by one call
    with pytest.raises(BudgetExceeded) as e:
        b.check_before_call(Decimal("0"))
    assert e.value.kind == "usd"
    b2 = StepBudget(limits(max_usd=Decimal("0.50")))
    b2.record_call(Usage(1, 0, 0, 0, 1, 0), Decimal("0.49"))
    with pytest.raises(BudgetExceeded):
        b2.check_before_call(Decimal("0.02"))


def test_token_projection_uses_last_prompt():
    b = StepBudget(limits(max_total_tokens=100_000))
    b.record_call(Usage(60_000, 0, 0, 0, 1_000, 0), Decimal("0"))
    with pytest.raises(BudgetExceeded) as e:  # 61k + 60k > 100k
        b.check_before_call(Decimal("0"))
    assert e.value.kind == "tokens"


def test_wall_clock_uses_injected_clock():
    c = FakeClock()
    b = StepBudget(limits(max_wall_seconds=10.0), clock=c)
    c.t = 10.5
    assert b.remaining_seconds() == 0
    with pytest.raises(BudgetExceeded) as e:
        b.check_before_call(Decimal("0"))
    assert e.value.kind == "wall_clock"


def test_tool_call_cap_does_not_mutate():
    b = StepBudget(limits(max_tool_calls=2))
    with pytest.raises(BudgetExceeded):
        b.check_tool_calls(3)
    assert b.tool_calls_used == 0


def test_totals_sum_full_usage_of_every_call():
    c = FakeClock()
    b = StepBudget(limits(), clock=c)
    b.record_call(Usage(100, 10, 5, 2, 50, 7), Decimal("0.010000"))
    b.record_call(Usage(200, 20, 0, 0, 30, 3), Decimal("0.020000"))
    b.record_tool_calls(2)
    c.t = 3.5
    totals = b.totals()
    assert totals.turns == 2
    assert totals.tool_calls == 2
    assert totals.usage == Usage(300, 30, 5, 2, 80, 10)
    assert totals.cost_usd == Decimal("0.030000")
    assert totals.elapsed_seconds == 3.5
    assert b.last_prompt_tokens == 220
    assert b.turns_used == 2
    assert b.spent_usd == Decimal("0.030000")
    assert b.tokens_used == 300 + 30 + 5 + 2 + 80


def test_float_cost_is_rejected():
    with pytest.raises(TypeError):
        StepBudget(limits()).record_call(Usage.zero(), 0.5)  # type: ignore[arg-type]
