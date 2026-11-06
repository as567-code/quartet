# ADR-0002: PostgreSQL for the task queue and state

- Status: accepted
- Date: 2026-11-06

## Context

Quartet needs a work queue, file leases, task state, hand-overs and an audit log. These must be consistent with each other: a state change, its event, and a lease change should commit or fail together. Workers may crash at any point, and a crashed worker's task must be reclaimable (FR-6, FR-23, FR-24). Quartet runs locally for one owner, so operational weight matters.

## Decision

Use PostgreSQL 16 as the single store.

- **Queue:** workers claim tasks with `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING ...`. Lease expiry uses the database `now()`, never the host clock. Reaping clears expired leases, bumps `version` (the compare-and-set token, which fences the previous holder), increments `attempt` for runnable states only, and logs a `lease_reaped` event, all in one statement. `claim()` reaps first in its own transaction, so recovery does not depend on a separate reaper loop. Each queue function needs a dedicated session with no open transaction and commits its own.
- **Transitions:** every state change is a compare-and-set on state and version, validated against a static transition table.
- **Audit:** every transition, `create_task()` and lease reap appends to the append-only `events` table. The application role has SELECT and column-level INSERT on `(task_id, type, payload)` only, so `id` and `created_at` are stamped by the database, and no UPDATE, DELETE or TRUNCATE. `task_id` is nullable for task-less events. Application-written payloads are capped at 65,536 bytes of compact JSON.
- **Roles:** `quartet_migrator` owns the database and every table and runs migrations (Alembic connects through `QUARTET_MIGRATOR_DATABASE_URL` and never falls back to the application URL). `quartet_app` has DML only: SELECT/INSERT/UPDATE/DELETE on the tables (also for future migrator-created tables, via default privileges), no CREATE on the schema, the restricted `events` grants above, and SELECT only on `alembic_version`.

## Alternatives considered

- **Redis / RQ.** A fast queue, but state, leases and the audit log would live in a second store with no cross-store transaction, so a crash between the two can leave them inconsistent. Rejected.
- **Celery.** Brings a broker and its own retry and acknowledgement semantics, which would overlap with, and obscure, Quartet's own recovery design (ADR-0001). Rejected.
- **Temporal.** Would own the workflow state and recovery that Quartet must implement itself (ADR-0001). Rejected.
- **SQLite.** Simple, but multi-process writers, `SKIP LOCKED`, advisory locks and role-based privilege separation are unavailable or weaker. Rejected.

## Consequences

- One transactional store gives atomic claim, transition, event and lease updates, and a durable audit trail.
- Postgres is a required dependency and a security-relevant asset: role split, loopback-bound local instance, validated connection URLs (see [../THREAT_MODEL.md](../THREAT_MODEL.md)).
- Throughput is bounded by Postgres; acceptable at the target concurrency (single-digit workers).
- `LISTEN/NOTIFY` wake-ups are optional (P2); polling is the baseline.
- Queue and lease queries are written as explicit SQL, not hidden behind ORM magic; DML uses bind parameters and DDL is composed with `psycopg.sql`.
