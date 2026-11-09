"""Per-step budget gate, checked before every LLM call and before every tool-call batch.

P2-R6 binding semantics: a call is refused when the step is already at a cap, or when the cap
would be breached by the minimum the next call must cost (the previous prompt re-sent at the
cache-read rate, with no output). Output tokens are not reserved. The overshoot bound is:
a step can exceed `max_usd` or `max_total_tokens` by **at most one call**, namely that call's
input plus `max_output_tokens` of output.
"""

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from quartet.llm.base import BudgetExceeded, StepLimits, StepTotals, Usage


def _require_decimal(name: str, value: Any) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")


class StepBudget:
    def __init__(
        self,
        limits: StepLimits,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._started = clock()
        self._turns = 0
        self._tool_calls = 0
        self._usage = Usage.zero()
        self._spent = Decimal("0")
        self._last_prompt_tokens = 0

    @property
    def limits(self) -> StepLimits:
        return self._limits

    @property
    def turns_used(self) -> int:
        return self._turns

    @property
    def tool_calls_used(self) -> int:
        return self._tool_calls

    @property
    def tokens_used(self) -> int:
        return self._usage.prompt_tokens + self._usage.output_tokens

    @property
    def spent_usd(self) -> Decimal:
        return self._spent

    @property
    def last_prompt_tokens(self) -> int:
        return self._last_prompt_tokens

    def _elapsed(self) -> float:
        return self._clock() - self._started

    def remaining_seconds(self) -> float:
        return max(0.0, self._limits.max_wall_seconds - self._elapsed())

    def check_wall_clock(self) -> None:
        if self.remaining_seconds() <= 0:
            raise BudgetExceeded("wall_clock", self._elapsed(), self._limits.max_wall_seconds)

    def check_before_call(self, next_input_floor_usd: Decimal) -> None:
        _require_decimal("next_input_floor_usd", next_input_floor_usd)
        limits = self._limits

        self.check_wall_clock()

        if self._turns >= limits.max_turns:
            raise BudgetExceeded("turns", self._turns, limits.max_turns)

        projected_tokens = self.tokens_used + self._last_prompt_tokens
        if self.tokens_used >= limits.max_total_tokens or (
            projected_tokens > limits.max_total_tokens
        ):
            raise BudgetExceeded("tokens", projected_tokens, limits.max_total_tokens)

        projected_usd = self._spent + next_input_floor_usd
        if self._spent >= limits.max_usd or projected_usd > limits.max_usd:
            raise BudgetExceeded("usd", projected_usd, limits.max_usd)

    def check_tool_calls(self, n: int) -> None:
        if self._tool_calls + n > self._limits.max_tool_calls:
            raise BudgetExceeded("tool_calls", self._tool_calls + n, self._limits.max_tool_calls)

    def record_call(self, usage: Usage, cost: Decimal) -> None:
        _require_decimal("cost", cost)
        self._turns += 1
        self._usage = self._usage + usage
        self._spent += cost
        self._last_prompt_tokens = usage.prompt_tokens

    def record_tool_calls(self, n: int) -> None:
        self._tool_calls += n

    def totals(self) -> StepTotals:
        return StepTotals(
            turns=self._turns,
            tool_calls=self._tool_calls,
            usage=self._usage,
            cost_usd=self._spent,
            elapsed_seconds=self._elapsed(),
        )
