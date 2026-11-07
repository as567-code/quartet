"""Typed retry policy: the only retry layer (SDK clients are built with ``max_retries=0``).

Backoff is full jitter, and every sleep is bounded by the step's wall-clock budget. Only the SDK
error types named in ``classify`` are handled; anything else propagates unchanged. Errors are
decided inside the ``except`` block and raised after it exits, so the unredacted SDK exception
never rides along in ``__context__``; they carry redacted text only (FR-30).

A retryable failure that carries a billed turn is handed to ``on_billed`` (so the caller records
it) before the retry decision, and ``before_retry`` (the budget gate) runs before every further
HTTP attempt (FR-C1).
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anthropic
import openai

from quartet.llm.base import BudgetExceeded, ProviderError
from quartet.llm.budget import StepBudget
from quartet.telemetry.redaction import Redactor


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_seconds: float = 1.0
    cap_seconds: float = 30.0
    max_retry_after_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retryable: bool
    retry_after: float | None
    status: int | None
    code: str | None
    request_id: str | None


_RETRYABLE_STATUSES = frozenset({408, 409, 429})


def _parse_seconds(raw: str | None, *, scale: float) -> float | None:
    """A non-negative number of seconds, else ``None`` (an HTTP-date is ``None``)."""
    if raw is None:
        return None
    try:
        value = float(raw.strip()) * scale
    except ValueError:
        return None
    return value if 0.0 <= value < float("inf") else None


def _retry_after(headers: "dict[str, str] | object") -> float | None:
    get = getattr(headers, "get", None)
    if get is None:
        return None
    ms = _parse_seconds(get("retry-after-ms"), scale=0.001)
    if ms is not None:
        return ms
    return _parse_seconds(get("retry-after"), scale=1.0)


def _status_retryable(exc: anthropic.APIStatusError | openai.APIStatusError) -> bool:
    should_retry = exc.response.headers.get("x-should-retry")
    if should_retry is not None:
        lowered = should_retry.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    if isinstance(exc, openai.RateLimitError) and exc.code == "insufficient_quota":
        return False
    status = exc.status_code
    return status in _RETRYABLE_STATUSES or status >= 500


def classify(exc: Exception) -> RetryDecision | None:
    """Classify a provider failure, or ``None`` when ``exc`` is not one."""
    if isinstance(exc, anthropic.APIStatusError | openai.APIStatusError):
        code = getattr(exc, "code", None)
        return RetryDecision(
            retryable=_status_retryable(exc),
            retry_after=_retry_after(exc.response.headers),
            status=exc.status_code,
            code=None if code is None else str(code),
            request_id=exc.request_id,
        )
    if isinstance(exc, anthropic.APIConnectionError | openai.APIConnectionError):
        return RetryDecision(True, None, None, None, None)
    if isinstance(exc, ProviderError):
        return RetryDecision(exc.retryable, None, exc.status, exc.code, exc.request_id)
    if isinstance(exc, anthropic.APIError | openai.APIError):
        code = getattr(exc, "code", None)
        return RetryDecision(False, None, None, None if code is None else str(code), None)
    return None


def _fail(
    exc: Exception, decision: RetryDecision, message: str, *, retryable: bool, keep_turn: bool
) -> ProviderError:
    return ProviderError(
        message,
        retryable=retryable,
        status=decision.status,
        code=decision.code,
        request_id=decision.request_id,
        turn=exc.turn if keep_turn and isinstance(exc, ProviderError) else None,
    )


async def call_with_retries[T](
    attempt: Callable[[float], Awaitable[T]],
    *,
    budget: StepBudget,
    policy: RetryPolicy,
    redactor: Redactor,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
    on_billed: Callable[[object], Awaitable[None]] | None = None,
    before_retry: Callable[[], None] | None = None,
) -> tuple[T, int]:
    n = 0
    while True:
        n += 1
        budget.check_wall_clock()
        if n > 1 and before_retry is not None:
            before_retry()
        failure: Exception | None = None
        billed: object | None = None
        delay = 0.0
        try:
            return await attempt(budget.remaining_seconds()), n
        except (anthropic.APIError, openai.APIError, ProviderError) as exc:
            # Decide inside the block, raise outside it: raising here would leave the
            # unredacted SDK exception in ``__context__``.
            decision = classify(exc)
            if decision is None:  # pragma: no cover - the except clause names only classified types
                raise
            text = (
                f"{type(exc).__name__} (status {decision.status}, code {decision.code}): "
                f"{redactor.redact(str(exc))}"
            )
            # A retryable failure's billed turn goes to ``on_billed`` and is then dropped from
            # the error, so the caller never records it twice. A final failure keeps its turn.
            if decision.retryable and on_billed is not None and isinstance(exc, ProviderError):
                billed = exc.turn
            keep_turn = billed is None
            if not decision.retryable:
                failure = _fail(exc, decision, text, retryable=False, keep_turn=keep_turn)
            elif n >= policy.max_attempts:
                failure = _fail(
                    exc,
                    decision,
                    f"gave up after {n} attempts: {text}",
                    retryable=True,
                    keep_turn=keep_turn,
                )
            elif (
                decision.retry_after is not None
                and decision.retry_after > policy.max_retry_after_seconds
            ):
                failure = _fail(
                    exc,
                    decision,
                    f"retry-after {decision.retry_after:g}s exceeds the "
                    f"{policy.max_retry_after_seconds:g}s cap: {text}",
                    retryable=True,
                    keep_turn=keep_turn,
                )
            else:
                if decision.retry_after is not None:
                    delay = decision.retry_after
                else:
                    delay = rand() * min(policy.cap_seconds, policy.base_seconds * 2 ** (n - 1))
                if delay >= budget.remaining_seconds():
                    failure = BudgetExceeded(
                        "wall_clock",
                        budget.totals().elapsed_seconds,
                        budget.limits.max_wall_seconds,
                    )
        if billed is not None and on_billed is not None:
            await on_billed(billed)
        if failure is not None:
            raise failure
        await sleep(delay)
