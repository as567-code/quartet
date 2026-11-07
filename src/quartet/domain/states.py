"""Task states and agent roles (PRD §9.2). Values equal names and match the PG enum labels."""

from enum import StrEnum


class TaskState(StrEnum):
    QUEUED = "QUEUED"
    SCOPING = "SCOPING"
    REJECTED = "REJECTED"
    PLANNING = "PLANNING"
    PLAN_REVIEW = "PLAN_REVIEW"
    WAITING_LEASES = "WAITING_LEASES"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    INTEGRATING = "INTEGRATING"
    PR_DRAFT = "PR_DRAFT"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    MERGED = "MERGED"
    CLOSED = "CLOSED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class Role(StrEnum):
    PLANNER = "PLANNER"
    IMPLEMENTER = "IMPLEMENTER"
    TESTER = "TESTER"
    REVIEWER = "REVIEWER"
    ORCHESTRATOR = "ORCHESTRATOR"
    HUMAN = "HUMAN"


# States a worker may claim (the partial ix_tasks_claimable index mirrors this set).
RUNNABLE: frozenset[TaskState] = frozenset(
    {
        TaskState.QUEUED,
        TaskState.SCOPING,
        TaskState.PLANNING,
        TaskState.WAITING_LEASES,
        TaskState.IMPLEMENTING,
        TaskState.TESTING,
        TaskState.REVIEWING,
        TaskState.INTEGRATING,
        TaskState.PR_DRAFT,
    }
)
