"""Cost ledger: per-call rows in ``llm_calls``/``tool_calls`` and the task's running spend.

Completeness (P2-R5/R6): a row exists for every call that completed or failed with a known
usage. A call cut off by the step clock has unknown usage and no row; an in-band OpenAI
``failed`` response that is retried and then succeeds records none of its own usage.
"""

import asyncio
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from quartet.db.models import LlmCall, Task, ToolCall
from quartet.db.session import session_scope
from quartet.llm.base import CallRecord, StepRecorder, ToolCallRecord


class DbStepRecorder(StepRecorder):
    """Writes each call from a worker thread in its own transaction."""

    def __init__(self, engine: Engine, *, task_id: UUID, step_id: UUID) -> None:
        self._engine = engine
        self._task_id = task_id
        self._step_id = step_id

    async def llm_call(self, record: CallRecord) -> None:
        await asyncio.to_thread(self._write_llm, record)

    async def tool_call(self, record: ToolCallRecord) -> None:
        await asyncio.to_thread(self._write_tool, record)

    def _write_llm(self, record: CallRecord) -> None:
        usage = record.usage
        with session_scope(self._engine) as session:
            session.add(
                LlmCall(
                    step_id=self._step_id,
                    provider=record.provider.value,
                    requested_model=record.requested_model,
                    model=record.model,
                    request_id=record.request_id,
                    tokens_in=usage.uncached_input_tokens,
                    tokens_out=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_short_tokens
                    + usage.cache_write_long_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    attempts=record.attempts,
                    cost_usd=record.cost_usd,
                    latency_ms=record.latency_ms,
                    stop_reason=record.stop_reason,
                    trace_id=record.trace_id,
                )
            )
            session.flush()
            result = session.execute(
                sa.update(Task)
                .where(Task.id == self._task_id)
                .values(spent_usd=Task.spent_usd.op("+")(record.cost_usd))
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise LookupError("task not found for cost ledger update")

    def _write_tool(self, record: ToolCallRecord) -> None:
        with session_scope(self._engine) as session:
            session.add(
                ToolCall(
                    step_id=self._step_id,
                    tool=record.tool,
                    call_id=record.call_id,
                    args_redacted=record.args_redacted,
                    is_error=record.is_error,
                    duration_ms=record.duration_ms,
                )
            )


def remaining_task_budget(session: Session, task_id: UUID) -> Decimal:
    """``max(budget_usd - spent_usd, 0)`` for the task."""
    row = session.execute(
        sa.select(Task.budget_usd, Task.spent_usd).where(Task.id == task_id)
    ).one_or_none()
    if row is None:
        raise LookupError("task not found")
    remaining: Decimal = row.budget_usd - row.spent_usd
    return max(remaining, Decimal(0))
