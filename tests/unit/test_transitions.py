import itertools

import pytest

from quartet.domain.states import TaskState
from quartet.domain.transitions import ALLOWED, TERMINAL, IllegalTransition, check_transition

NON_TERMINAL = set(TaskState) - TERMINAL


def _reach(start: TaskState) -> set[TaskState]:
    seen, todo = {start}, [start]
    while todo:
        for n in ALLOWED[todo.pop()] - seen:
            seen.add(n)
            todo.append(n)
    return seen


def test_terminal_states_have_no_exits():
    assert {TaskState.REJECTED, TaskState.MERGED, TaskState.CLOSED} == TERMINAL
    assert all(ALLOWED[s] == frozenset() for s in TERMINAL)


def test_no_self_loops():
    assert all(s not in ALLOWED[s] for s in TaskState)


def test_every_non_terminal_can_close_and_escalate():
    for s in NON_TERMINAL:
        assert TaskState.CLOSED in ALLOWED[s]
        if s is not TaskState.NEEDS_HUMAN:
            assert TaskState.NEEDS_HUMAN in ALLOWED[s]


def test_needs_human_can_resume_to_any_other_non_terminal():
    assert ALLOWED[TaskState.NEEDS_HUMAN] == (NON_TERMINAL - {TaskState.NEEDS_HUMAN}) | {
        TaskState.CLOSED
    }


def test_allowed_is_read_only():
    with pytest.raises(TypeError):
        ALLOWED[TaskState.QUEUED] = frozenset()  # type: ignore[index]


def test_all_states_reachable_from_queued():
    assert _reach(TaskState.QUEUED) == set(TaskState)


def test_every_non_terminal_can_reach_a_terminal():
    for s in NON_TERMINAL:
        assert _reach(s) & TERMINAL, s


@pytest.mark.parametrize("a", list(TaskState))
@pytest.mark.parametrize("b", list(TaskState))
def test_check_transition_matches_table(a, b):
    if b in ALLOWED[a]:
        check_transition(a, b)
    else:
        with pytest.raises(IllegalTransition):
            check_transition(a, b)


def test_happy_path_edges_present():
    path = [
        "QUEUED",
        "SCOPING",
        "PLANNING",
        "WAITING_LEASES",
        "IMPLEMENTING",
        "TESTING",
        "REVIEWING",
        "INTEGRATING",
        "PR_DRAFT",
        "AWAITING_HUMAN",
        "MERGED",
    ]
    for a, b in itertools.pairwise(path):
        assert TaskState(b) in ALLOWED[TaskState(a)]
