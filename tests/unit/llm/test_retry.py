import asyncio
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import openai
import pytest

from quartet.llm.base import BudgetExceeded, ProviderError
from quartet.llm.budget import StepBudget
from quartet.llm.retry import RetryPolicy, call_with_retries, classify
from quartet.telemetry.redaction import Redactor
from tests.unit.llm._errors import status_error
from tests.unit.llm._records import FakeClock, limits

pytestmark = pytest.mark.anyio


async def run(
    errors: list[Any],
    *,
    budget: StepBudget | None = None,
    rand: Callable[[], float] = lambda: 0.5,
    policy: RetryPolicy | None = None,
    sleeps: list[float] | None = None,
) -> tuple[tuple[Any, int], list[float]]:
    policy = policy or RetryPolicy()
    sleeps = [] if sleeps is None else sleeps
    queue = list(errors)

    async def attempt(timeout: float) -> Any:
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def sleep(s: float) -> None:
        sleeps.append(s)

    result = await call_with_retries(
        attempt,
        budget=budget or StepBudget(limits()),
        policy=policy,
        redactor=Redactor(),
        sleep=sleep,
        rand=rand,
    )
    return result, sleeps


async def test_429_then_success_uses_full_jitter():
    (value, attempts), sleeps = await run([status_error(anthropic.RateLimitError, 429), "ok"])
    assert (value, attempts, sleeps) == ("ok", 2, [0.5])


@pytest.mark.parametrize(
    "cls,status",
    [
        (anthropic.OverloadedError, 529),
        (openai.InternalServerError, 503),
        (anthropic.ConflictError, 409),
    ],
)
async def test_transient_statuses_retry(cls, status):
    assert (await run([status_error(cls, status), "ok"]))[0] == ("ok", 2)


@pytest.mark.parametrize(
    "cls,status",
    [
        (anthropic.BadRequestError, 400),
        (anthropic.AuthenticationError, 401),
        (openai.NotFoundError, 404),
        (openai.BadRequestError, 400),
    ],
)
async def test_client_errors_never_retry(cls, status):
    with pytest.raises(ProviderError) as e:
        await run([status_error(cls, status)])
    assert e.value.retryable is False and e.value.status == status


async def test_connection_error_retried():
    req = httpx2.Request("POST", "https://api.example.test/v1/x")
    assert (await run([openai.APIConnectionError(request=req), "ok"]))[0] == ("ok", 2)


async def test_insufficient_quota_not_retried():
    err = status_error(
        openai.RateLimitError, 429, body={"code": "insufficient_quota", "message": "x"}
    )
    with pytest.raises(ProviderError) as e:
        await run([err])
    assert e.value.retryable is False


async def test_x_should_retry_false_overrides_status():
    with pytest.raises(ProviderError):
        await run(
            [status_error(anthropic.InternalServerError, 500, headers={"x-should-retry": "false"})]
        )


async def test_x_should_retry_true_overrides_status():
    err = status_error(anthropic.BadRequestError, 400, headers={"x-should-retry": "true"})
    assert (await run([err, "ok"]))[0] == ("ok", 2)


async def test_retry_after_and_retry_after_ms_honoured():
    _, sleeps = await run(
        [
            status_error(openai.RateLimitError, 429, headers={"retry-after": "2"}),
            status_error(openai.RateLimitError, 429, headers={"retry-after-ms": "1500"}),
            "ok",
        ]
    )
    assert sleeps == [2.0, 1.5]


async def test_retry_after_http_date_is_ignored():
    err = status_error(
        anthropic.RateLimitError, 429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}
    )
    decision = classify(err)
    assert decision is not None and decision.retry_after is None


async def test_retry_after_beyond_deadline_raises_without_sleep():  # Review Focus 1
    clock = FakeClock()
    budget = StepBudget(limits(max_wall_seconds=30.0), clock=clock)
    slept: list[float] = []
    with pytest.raises(BudgetExceeded) as e:
        await run(
            [status_error(anthropic.RateLimitError, 429, headers={"retry-after": "45"})],
            budget=budget,
            sleeps=slept,
        )
    assert e.value.kind == "wall_clock" and slept == []


async def test_retry_after_above_policy_cap_raises_provider_error():
    err = status_error(anthropic.RateLimitError, 429, headers={"retry-after": "120"})
    slept: list[float] = []
    with pytest.raises(ProviderError) as e:
        await run([err], sleeps=slept)
    assert e.value.retryable is True and slept == []


async def test_gives_up_after_max_attempts():
    errs = [status_error(anthropic.InternalServerError, 500) for _ in range(4)]
    with pytest.raises(ProviderError) as e:
        await run(errs)
    assert e.value.retryable is True


async def test_non_provider_exceptions_propagate_unchanged():
    boom = ValueError("x")
    with pytest.raises(ValueError) as e:
        await run([boom])
    assert e.value is boom
    with pytest.raises(asyncio.CancelledError):
        await run([asyncio.CancelledError()])


async def test_provider_error_keeps_turn_and_other_sdk_errors_do_not_retry():
    turn = object()
    with pytest.raises(ProviderError) as e:
        await run([ProviderError("bad", retryable=False, status=400, turn=turn)])
    assert e.value.turn is turn
    decision = classify(openai.APIError("x", httpx2.Request("POST", "https://a.test"), body=None))
    assert decision is not None and decision.retryable is False
    assert classify(TimeoutError()) is None


async def test_provider_error_message_redacted():
    key = "sk-ant-api03-" + "A" * 40
    err = status_error(anthropic.AuthenticationError, 401, message=f"invalid key {key}")
    assert key in str(err) and key in err.message  # the fixture really carries the key
    with pytest.raises(ProviderError) as e:
        await run([err])
    assert key not in str(e.value) and "401" in str(e.value) and e.value.__cause__ is None
    assert e.value.__context__ is None


async def test_wall_clock_error_reports_configured_limit_after_deadline():
    clock = FakeClock()
    budget = StepBudget(limits(max_wall_seconds=30.0), clock=clock)
    queue: list[Any] = [status_error(anthropic.InternalServerError, 500)]

    async def attempt(timeout: float) -> Any:
        clock.t = 35.0
        raise queue.pop(0)

    with pytest.raises(BudgetExceeded) as e:
        await call_with_retries(
            attempt, budget=budget, policy=RetryPolicy(), redactor=Redactor(), rand=lambda: 0.5
        )
    assert e.value.kind == "wall_clock"
    assert e.value.limit == 30.0 and e.value.used >= 35.0
    assert e.value.__context__ is None


async def _run_billed(
    outcomes: list[Any], *, retryable: bool = True, max_attempts: int = 4
) -> tuple[list[object], list[int], Any]:
    billed: list[object] = []
    gate_calls: list[int] = []
    queue = list(outcomes)

    async def attempt(timeout: float) -> Any:
        item = queue.pop(0)
        if item == "ok":
            return item
        raise ProviderError("x", retryable=retryable, turn=item)

    async def on_billed(turn: object) -> None:
        billed.append(turn)

    async def sleep(_s: float) -> None:
        return None

    outcome: Any
    try:
        outcome = await call_with_retries(
            attempt,
            budget=StepBudget(limits()),
            policy=RetryPolicy(max_attempts=max_attempts),
            redactor=Redactor(),
            sleep=sleep,
            rand=lambda: 0.5,
            on_billed=on_billed,
            before_retry=lambda: gate_calls.append(len(billed)),
        )
    except ProviderError as err:
        outcome = err
    return billed, gate_calls, outcome


async def test_billed_retryable_failures_are_handed_over_and_the_gate_runs_before_each_retry():
    billed, gate_calls, outcome = await _run_billed(["t1", "t2", "ok"])
    assert billed == ["t1", "t2"] and outcome == ("ok", 3)
    # No gate before the first attempt; before attempts 2 and 3 the earlier turns were handed over.
    assert gate_calls == [1, 2]


async def test_final_retryable_failure_drops_its_turn_after_handing_it_over():
    billed, gate_calls, outcome = await _run_billed(["t1", "t2"], max_attempts=2)
    assert billed == ["t1", "t2"] and gate_calls == [1]
    assert isinstance(outcome, ProviderError) and outcome.turn is None and outcome.retryable
    assert outcome.__context__ is None


async def test_non_retryable_failure_keeps_its_turn_for_the_caller():
    billed, _, outcome = await _run_billed(["t1"], retryable=False)
    assert billed == [] and isinstance(outcome, ProviderError) and outcome.turn == "t1"


async def test_gate_error_stops_retrying():
    calls: list[int] = []

    async def attempt(timeout: float) -> Any:
        calls.append(1)
        raise ProviderError("x", retryable=True)

    def gate() -> None:
        raise BudgetExceeded("usd", 2, 1)

    with pytest.raises(BudgetExceeded):
        await call_with_retries(
            attempt,
            budget=StepBudget(limits()),
            policy=RetryPolicy(),
            redactor=Redactor(),
            sleep=lambda s: asyncio.sleep(0),
            before_retry=gate,
        )
    assert calls == [1]
