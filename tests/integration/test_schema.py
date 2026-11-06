import re
import uuid

import pytest
import sqlalchemy as sa
from alembic import command

from quartet.domain.states import RUNNABLE

pytestmark = pytest.mark.integration

EXPECTED = {
    "repos",
    "tasks",
    "role_steps",
    "artifacts",
    "handovers",
    "llm_calls",
    "tool_calls",
    "file_leases",
    "worktrees",
    "checkpoints",
    "interventions",
    "pull_requests",
    "events",
}


def _names(url: str) -> set[str]:
    eng = sa.create_engine(url)
    try:
        return set(sa.inspect(eng).get_table_names())
    finally:
        eng.dispose()


def test_all_tables_exist_with_created_at(fresh_db):
    eng = sa.create_engine(fresh_db.migrator_url)
    try:
        insp = sa.inspect(eng)
        assert set(insp.get_table_names()) >= EXPECTED
        for t in EXPECTED:
            assert "created_at" in {c["name"] for c in insp.get_columns(t)}, t
        for t in ("tasks", "role_steps", "worktrees", "checkpoints", "pull_requests"):
            assert "updated_at" in {c["name"] for c in insp.get_columns(t)}, t
    finally:
        eng.dispose()


def test_claimable_index_is_partial_on_runnable_states(fresh_db):
    eng = sa.create_engine(fresh_db.migrator_url)
    try:
        with eng.connect() as c:
            ddl = c.execute(
                sa.text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_tasks_claimable'")
            ).scalar_one()
    finally:
        eng.dispose()
    assert "lease_owner IS NULL" in ddl
    predicate_states = set(re.findall(r"'([A-Z_]+)'", ddl))
    assert predicate_states == {s.value for s in RUNNABLE}


def test_leased_index_is_partial_on_lease_owner(fresh_db):
    eng = sa.create_engine(fresh_db.migrator_url)
    try:
        with eng.connect() as c:
            ddl = c.execute(
                sa.text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_tasks_leased'")
            ).scalar_one()
    finally:
        eng.dispose()
    assert "(lease_expires_at)" in ddl and "WHERE (lease_owner IS NOT NULL)" in ddl


def test_events_are_append_only_for_app_role(app_engine, seeded_task):
    with app_engine.begin() as c:
        c.execute(
            sa.text("INSERT INTO events (task_id, type, payload) VALUES (:t, 'x', '{}')"),
            {"t": seeded_task},
        )
    for stmt in ("UPDATE events SET type='y'", "DELETE FROM events", "TRUNCATE events"):
        with (
            pytest.raises(sa.exc.ProgrammingError, match="permission denied"),
            app_engine.begin() as c,
        ):
            c.execute(sa.text(stmt))


@pytest.mark.parametrize(
    "stmt",
    [
        "INSERT INTO events (id, task_id, type, payload) VALUES (999, :t, 'x', '{}')",
        "INSERT INTO events (task_id, type, payload, created_at) "
        "VALUES (:t, 'x', '{}', '2000-01-01T00:00:00Z')",
    ],
    ids=["explicit_id", "explicit_created_at"],
)
def test_app_role_cannot_stamp_event_id_or_created_at(app_engine, seeded_task, stmt):
    with (
        pytest.raises(sa.exc.ProgrammingError, match="permission denied"),
        app_engine.begin() as c,
    ):
        c.execute(sa.text(stmt), {"t": seeded_task})


def test_event_id_and_created_at_are_db_stamped(app_engine, seeded_task):
    from sqlalchemy.orm import Session

    from quartet.db.models import Event
    from quartet.db.repository import append_event
    from quartet.domain.states import TaskState
    from quartet.domain.transitions import transition

    with Session(app_engine) as s, s.begin():
        eid = append_event(s, seeded_task, "note", {"a": 1})
        transition(s, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 0, "moved")
        db_now = s.execute(sa.text("SELECT now()")).scalar_one()
    with Session(app_engine) as s:
        rows = s.query(Event).order_by(Event.id).all()
        assert [r.type for r in rows] == ["note", "moved"] and rows[0].id == eid
        assert all(r.id is not None and r.created_at == db_now for r in rows)


def test_events_task_id_is_nullable_but_still_a_foreign_key(app_engine):
    ins = sa.text("INSERT INTO events (task_id, type, payload) VALUES (:t, 'worker', '{}')")
    with app_engine.begin() as c:
        c.execute(ins, {"t": None})
    with pytest.raises(sa.exc.IntegrityError), app_engine.begin() as c:
        c.execute(ins, {"t": uuid.uuid4()})
    with app_engine.connect() as c:
        assert (
            c.execute(
                sa.text("SELECT count(*) FROM events WHERE task_id IS NULL AND type = 'worker'")
            ).scalar_one()
            == 1
        )


# Tables quartet_app must never UPDATE or DELETE. Default privileges grant DML on every new
# table, so a new append-only table must REVOKE explicitly AND be listed here.
APPEND_ONLY = {"events"}
READ_ONLY = {"alembic_version"}  # migrator-owned bookkeeping; the app may only SELECT it


def test_app_role_lacks_update_delete_on_exactly_the_append_only_tables(fresh_db):
    eng = sa.create_engine(fresh_db.migrator_url)
    try:
        with eng.connect() as c:
            rows = c.execute(
                sa.text(
                    "SELECT tablename, "
                    "has_table_privilege('quartet_app', 'public.' || tablename, 'UPDATE') AS u, "
                    "has_table_privilege('quartet_app', 'public.' || tablename, 'DELETE') AS d "
                    "FROM pg_tables WHERE schemaname = 'public'"
                )
            ).all()
    finally:
        eng.dispose()
    lacking = {r.tablename for r in rows if not r.u and not r.d}
    full = {r.tablename for r in rows if r.u and r.d}
    assert lacking == APPEND_ONLY | READ_ONLY
    assert full == {r.tablename for r in rows} - APPEND_ONLY - READ_ONLY


def test_app_role_cannot_create_objects_in_the_schema(fresh_db):
    eng = sa.create_engine(fresh_db.migrator_url)
    try:
        with eng.connect() as c:
            assert (
                c.execute(
                    sa.text("SELECT has_schema_privilege('quartet_app', 'public', 'CREATE')")
                ).scalar_one()
                is False
            )
    finally:
        eng.dispose()
    app = sa.create_engine(fresh_db.app_url)
    try:
        with (
            pytest.raises(sa.exc.ProgrammingError, match="permission denied"),
            app.begin() as c,
        ):
            c.execute(sa.text("CREATE TABLE public.zz_app_made (id int)"))
    finally:
        app.dispose()


def test_migration_round_trip(fresh_db, alembic_config):
    cfg = alembic_config(fresh_db.migrator_url)
    command.downgrade(cfg, "base")
    assert not (EXPECTED & _names(fresh_db.migrator_url))
    eng = sa.create_engine(fresh_db.migrator_url)
    try:
        with eng.connect() as c:
            left = c.execute(
                sa.text("SELECT typname FROM pg_type WHERE typname IN ('task_state','role')")
            ).all()
    finally:
        eng.dispose()
    assert left == []
    command.upgrade(cfg, "head")
    assert _names(fresh_db.migrator_url) >= EXPECTED


def test_pg_enum_labels_match_python_enums(app_engine):
    from quartet.domain.states import Role, TaskState

    with app_engine.connect() as c:
        for typ, enum in (("task_state", TaskState), ("role", Role)):
            labels = (
                c.execute(
                    sa.text(
                        "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid "
                        "WHERE t.typname=:n ORDER BY e.enumsortorder"
                    ),
                    {"n": typ},
                )
                .scalars()
                .all()
            )
            assert labels == [m.value for m in enum], typ


def test_states_module_shape():
    from quartet.domain.states import RUNNABLE, TaskState

    assert [s.name for s in TaskState][:3] == ["QUEUED", "SCOPING", "REJECTED"]
    assert TaskState.MERGED not in RUNNABLE and TaskState.PR_DRAFT in RUNNABLE
    assert len(RUNNABLE) == 9


def test_app_role_has_dml_on_future_tables_and_sequences(fresh_db):
    """Default privileges: a table the migrator creates later is usable by quartet_app."""
    mig = sa.create_engine(fresh_db.migrator_url)
    app = sa.create_engine(fresh_db.app_url)
    try:
        with mig.begin() as c:
            c.execute(sa.text("CREATE TABLE zz_future (id bigserial PRIMARY KEY, v int)"))
        with app.begin() as c:
            c.execute(sa.text("INSERT INTO zz_future (v) VALUES (1)"))
            assert c.execute(sa.text("SELECT count(*) FROM zz_future")).scalar_one() == 1
    finally:
        mig.dispose()
        app.dispose()


def test_defaults_and_check_constraints(app_engine, repo_id):
    ins = sa.text(
        "INSERT INTO tasks (repo_id, source, title, state) VALUES (:r, :s, 't', 'QUEUED')"
    )
    with app_engine.begin() as c:
        c.execute(ins, {"r": repo_id, "s": "manual"})
        row = c.execute(
            sa.text(
                "SELECT priority, version, attempt, budget_usd, spent_usd, risk_level FROM tasks"
            )
        ).one()
    assert (row.priority, row.version, row.attempt) == (100, 0, 0)
    assert str(row.budget_usd) == "5.0000" and row.spent_usd == 0 and row.risk_level is None
    with pytest.raises(sa.exc.IntegrityError), app_engine.begin() as c:
        c.execute(ins, {"r": repo_id, "s": "bogus"})
    with pytest.raises(sa.exc.IntegrityError), app_engine.begin() as c:
        c.execute(
            sa.text("UPDATE tasks SET risk_level='extreme'"),
        )


def test_role_steps_unique_and_fk(app_engine, seeded_task):
    ins = sa.text(
        "INSERT INTO role_steps (task_id, role, round, attempt, status) "
        "VALUES (:t, 'PLANNER', 0, 0, 'running')"
    )
    with app_engine.begin() as c:
        c.execute(ins, {"t": seeded_task})
    with pytest.raises(sa.exc.IntegrityError), app_engine.begin() as c:
        c.execute(ins, {"t": seeded_task})
    with pytest.raises(sa.exc.IntegrityError), app_engine.begin() as c:
        c.execute(ins, {"t": uuid.uuid4()})


def test_make_tasks_created_order_and_priorities(app_engine, make_tasks):
    ids = make_tasks(n=4, priorities=[30, 10, 20, 10])
    with app_engine.connect() as c:
        rows = c.execute(sa.text("SELECT id, priority FROM tasks ORDER BY created_at, id")).all()
    assert [r.id for r in rows] == ids
    assert [r.priority for r in rows] == [30, 10, 20, 10]
    assert len({r.id for r in rows}) == 4


def test_models_match_migrated_schema(fresh_db):
    from quartet.db.models import Base

    eng = sa.create_engine(fresh_db.migrator_url)
    try:
        insp = sa.inspect(eng)
        assert set(Base.metadata.tables) == EXPECTED
        for name, table in Base.metadata.tables.items():
            assert {c.name for c in table.columns} == {c["name"] for c in insp.get_columns(name)}, (
                name
            )
    finally:
        eng.dispose()


def test_models_match_migration(fresh_db):
    """Autogenerate finds no drift between ORM metadata and the migrated schema."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from quartet.db.models import Base

    eng = sa.create_engine(fresh_db.migrator_url)
    try:
        with eng.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            diff = compare_metadata(ctx, Base.metadata)
    finally:
        eng.dispose()
    assert diff == []


def test_app_role_cannot_modify_alembic_version(fresh_db):
    app = sa.create_engine(fresh_db.app_url)
    try:
        with app.connect() as c:
            assert c.execute(sa.text("SELECT count(*) FROM alembic_version")).scalar_one() == 1
        for stmt in (
            "UPDATE alembic_version SET version_num='x'",
            "DELETE FROM alembic_version",
            "INSERT INTO alembic_version (version_num) VALUES ('y')",
            "TRUNCATE alembic_version",
        ):
            with (
                pytest.raises(sa.exc.ProgrammingError, match="permission denied"),
                app.begin() as c,
            ):
                c.execute(sa.text(stmt))
    finally:
        app.dispose()
