# Quartet Threat Model

Status: v2 (Phase 2). Source of the security design: [PRD.md](PRD.md) §8 and §11. Every phase updates this file (gate S5, see [ROADMAP.md](ROADMAP.md)).

**Reading the controls.** Each control is labelled **implemented (Phase 0)**, **implemented (Phase 1)** or **planned (Phase N)**. A planned control does not exist yet and must not be relied on. At the end of Phase 1 the repository contains the CLI skeleton, hardened configuration loading, a loopback-only compose stack, CI security gates, and the state store: the schema and its grants, the compare-and-set state machine, the lease queue and the repository helpers. It contains no orchestrator loop, worker processes, sandbox, MCP server, agents or GitHub integration. At the end of Phase 2 the LLM provider layer (§3.7) also exists: the per-role loop, retries, budgets, validation, redaction, telemetry and the cost ledger. The sandbox, MCP server, orchestrator loop and GitHub flow are still planned.

## 1. Assets

| Asset | Why it matters |
|---|---|
| GitHub App private key and installation tokens | Could push to or alter target repos. |
| Anthropic and OpenAI API keys | Spend and abuse. |
| Host filesystem and developer secrets (`~/.ssh`, `~/.aws`, shell env) | Must never be reachable by an agent. |
| Target-repo integrity: default branch, CI, dependencies | Agent-authored code must not reach the default branch without a human. |
| Target-repo CI credentials | Agent-authored code may run in CI, so CI must hold nothing worth stealing. |
| Hidden-test repository | Leaking it would invalidate evaluation results. |
| PostgreSQL database (task DB) | Queue, state, leases, hand-overs, artifacts and the audit log. It stores untrusted issue text at rest (`tasks.title`, `tasks.body_untrusted`, `tasks.external_ref`). |
| Budget | Unbounded model spend is an anti-goal. |

## 2. Trust boundaries

Target architecture (PRD §8.2). Only the Postgres (schema, state machine, lease queue), configuration and CI parts exist at the end of Phase 1; the sandbox, orchestrator loop and GitHub flow are planned.

```
 UNTRUSTED: issue text · repo contents · MCP tool output · LLM output · test output
                 │
                 ▼
 ┌──── sandbox container per role step (no network, no secrets) ──────┐
 │ workspace MCP server · /work (RW) · /mirror (RO) · /quartet (RO)   │
 └────────────────┬───────────────────────────────────────────────────┘
                  │ stdio (MCP) · validated artifacts · post-step diff check
                  ▼
 ┌──── orchestrator (trusted) ────────────────────────────────────────┐
 │ state machine · leases · policy gate · git commit/push ·           │
 │ API keys · GitHub App · Postgres                                    │
 └────────────────┬───────────────────────────────────────────────────┘
                  ▼
   draft PR → target-repo CI (read-only token, no secrets) → human approval → merge
```

Prompt-injection posture (PRD §8.3): assume any issue, repo or tool content is hostile. Delimited data blocks reduce injection but do not prevent it. The real control is capability limits: no credentials, no network, no push or merge, blocked paths refused, flagged paths human-gated, post-step verification, and a CI that holds nothing worth stealing. A fully hijacked agent should be able to damage only its own worktree. All of these capability limits are **planned**; none exists in Phase 0.

## 3. STRIDE by component

S = Spoofing, T = Tampering, R = Repudiation, I = Information disclosure, D = Denial of service, E = Elevation of privilege.

### 3.1 Orchestrator

| | Threat | Control | Status |
|---|---|---|---|
| S | A process impersonates a worker and claims tasks | Workers authenticate to Postgres as `quartet_app`; lease ownership checks | login role (Phase 0); `SKIP LOCKED` claim and owner-checked heartbeat and release implemented (Phase 1). `worker_id` is a caller-chosen label, not an authenticated identity: any holder of the `quartet_app` password can act as any worker. Worker processes planned (Phase 6) |
| T | Illegal state transition or lost update | Compare-and-set on state and version against a static edge table; entering `NEEDS_HUMAN` records `resume_state` and leaving it (other than to `CLOSED`) is allowed only to that state | implemented (Phase 1) |
| T | Hostile `.env` or env-file injects settings | Env file is never read from the cwd; explicit `QUARTET_ENV_FILE` or `~/.quartet/.env`; must be a regular file owned by the user and not group/world-writable | implemented (Phase 0) |
| T | Hostile DB URL redirects the connection or smuggles libpq options | URL parsed with libpq's own parser; scheme and query keys allowlisted (`sslmode`, `connect_timeout`, `application_name`); `passfile`, `service`, `options` and similar rejected | implemented (Phase 0) |
| T | A worker whose lease expired keeps acting on its task (split brain) | Reaping an expired lease bumps `version`, the compare-and-set token, so the previous holder's transitions fail; heartbeat refuses an expired lease; `claim()` reaps before claiming | implemented (Phase 1). Fencing starts at the reap: a holder whose lease has expired but not yet been reaped can still transition until the next `claim()` or `reap_expired()` |
| R | An action cannot be attributed | Append-only `events` table; decision logging (`quartet intervene`) | append-only `events` implemented (Phase 1): every `transition()`, `create_task()` and lease reap appends an event; `quartet_app` has SELECT and column-level INSERT on `(task_id, type, payload)` only, so `id` and `created_at` are stamped by the database, and no UPDATE, DELETE or TRUNCATE. The migrator and the superuser can still alter it. Decision logging planned (Phase 7) |
| I | Secrets leak through logs, repr or URLs | Secrets typed as `SecretStr`; never logged or embedded in a logged URL | implemented (Phase 0) for settings; implemented (Phase 2) for the LLM layer's errors, logs and spans (exact known-secret replacement, then pattern redaction; the escaped-quote gap is in section 6); full trace and log redaction planned (Phase 8) |
| D | Runaway spend or loops | Per-step and per-task caps, `max_fix_rounds`, kill switch | per-step caps (turns, tool calls, tokens, USD, wall clock): implemented (Phase 2); per-task cap, `max_fix_rounds` and kill switch: planned (Phase 6) |
| E | Agent output triggers privileged actions | Artifacts are schema-validated; commits, pushes and PRs are orchestrator-only actions | planned (Phase 2, Phase 5, Phase 7) |

### 3.2 Sandbox (per role step)

| | Threat | Control | Status |
|---|---|---|---|
| T | Code under test modifies the host or other worktrees | Only the task's worktree is writable; mirror and leases mounted read-only | planned (Phase 3) |
| I | Agent reads host secrets or reaches the network | `--network none`, no host env passthrough, no Docker socket, no API keys in the sandbox | planned (Phase 3) |
| D | Fork bombs, memory exhaustion, hangs | `--pids-limit`, `--memory`, `--cpus`, hard timeout | planned (Phase 3) |
| E | Container escape or privilege gain | `--cap-drop ALL`, `no-new-privileges`, read-only rootfs, non-root user, digest-pinned image, `trivy` and `hadolint` on built images (S6) | planned (Phase 3) |
| T | Test code writes files outside its lease | Post-step diff verification | planned (Phase 5, Phase 6) |

No sandbox exists in Phase 0.

### 3.3 Workspace MCP server (`quartet-workspace`)

| | Threat | Control | Status |
|---|---|---|---|
| T | Path traversal or symlink escape out of `/work` | Path guard in the minimal server | planned (Phase 3) |
| T | Writes to blocked or unleased paths | Lease reader and blocked-path refusal (`LEASE_REQUIRED`) | planned (Phase 3, Phase 6) |
| E | Agent obtains tools beyond its role | Role allowlist enforced by the client filter and by the server `--role` flag | planned (Phase 3) |
| E | Agent obtains shell, network, git-push or ref-arbitrary git tools | No such tools exist; `git_log` and `git_diff` take no ref arguments; fixed executable allowlist without a shell | planned (Phase 3) |
| I | Server holds secrets | Package has no DB access, no LLM SDKs, no credentials | planned (Phase 3) |

### 3.4 PostgreSQL

| | Threat | Control | Status |
|---|---|---|---|
| S | Unauthenticated or remote access to the database | Compose binds `127.0.0.1` only; passwords are supplied by the owner in the git-ignored `.env` (they were generated randomly when this environment was set up, but the repo contains no generator and no strength check; strength is the owner's responsibility) | implemented (Phase 0) |
| T | Application role alters the schema or audit log | Role split: `quartet_migrator` owns the database, `quartet_app` is a separate login role; passwords passed through psql variables, not interpolated into SQL text; DML-only grants | role split implemented (Phase 0). Implemented (Phase 1): `quartet_app` has SELECT/INSERT/UPDATE/DELETE on the tables and no CREATE on the schema; `events` is append-only and DB-stamped (see 3.1); `alembic_version` is read-only; migrations run as `quartet_migrator` through `QUARTET_MIGRATOR_DATABASE_URL` and never fall back to the app URL. Default privileges extend DML to every future migrator-created table, so a new append-only table must REVOKE explicitly (a test pins the expected set) |
| T | SQL injection | Bind parameters for DML; DDL composed with `psycopg.sql.SQL`/`Identifier`/`Literal`; semgrep repo rules reject f-string SQL | rules and CI gate implemented (Phase 0); every queue, transition and repository statement uses bind parameters, implemented (Phase 1) |
| I | Image or supply-chain compromise of the database image | Image pinned by `@sha256:` digest | implemented (Phase 0) |
| D | Lease or claim starvation | Database `now()` for expiry; reaper | implemented (Phase 1): lease arithmetic uses the database clock only; `claim()` reaps expired leases itself, so recovery does not depend on a reaper loop; a reap bumps `attempt` only for runnable states and logs `lease_reaped`. Worker heartbeat scheduling planned (Phase 6) |
| T/D | Untrusted task text or event payloads corrupt or bloat the store | UTF-8 byte caps: `title` 512, `body_untrusted` 65,536, `external_ref` 512; NUL characters stripped; text that cannot be encoded as UTF-8 (lone surrogates) rejected; error messages never echo content; event payloads written by `append_event()` and `transition()` capped at 65,536 bytes of compact JSON | implemented (Phase 1): checked in Python before any write, with DB CHECK constraints on `title` and `body_untrusted` as defence in depth (`external_ref` is capped in Python only). Aggregate artifact size is unbounded until Phase 2 |

### 3.5 GitHub App

| | Threat | Control | Status |
|---|---|---|---|
| S | Stolen long-lived token | App (not PAT) with short-lived installation tokens passed only in the environment of the single `git push` | planned (Phase 7) |
| E | App merges or bypasses protections | Scopes limited to `contents: write`, `pull_requests: write`, `issues: read`, `metadata: read`; no administration, no workflows, no bypass; merge only with a human approval on the current head SHA | planned (Phase 7) |
| I | Private key leaks | Key kept outside the repo and the sandbox; `gitleaks` on every commit and in CI | gitleaks implemented (Phase 0); App key handling planned (Phase 7) |

No GitHub integration exists in Phase 0.

### 3.6 Target-repo CI

| | Threat | Control | Status |
|---|---|---|---|
| E | Agent-authored code exfiltrates CI secrets or escalates through workflows | Default `GITHUB_TOKEN` read-only; no secrets or variables reachable from `pull_request`; no self-hosted runners; `pull_request` only, never `pull_request_target`; checked by a test | planned (Phase 7) |
| T | Agent edits workflow files or CI config | `.github/**` blocked path in the policy gate | planned (Phase 5) |

Quartet's **own** repository CI is a different thing and is implemented (Phase 0): top-level `permissions: {}` with per-job least privilege, every action pinned to a 40-hex commit SHA, checksum-verified downloaded binaries, `persist-credentials: false`, and `actionlint` + `zizmor` over the workflows. That CI has never run on GitHub yet (see Residual risks).

### 3.7 LLM APIs (Anthropic, OpenAI)

| | Threat | Control | Status |
|---|---|---|---|
| I | Sensitive data sent to providers | Only the task spec, hand-overs and repo content in the worktree are sent; no host secrets or env reach the model context; prompt/response export to traces off by default | provider data handling implemented (Phase 2): OpenAI `store=false`, and no prompt or response content in span attributes (tested); no content export option exists yet. Context selection (what is sent, no host secrets) planned (Phase 3); trace content export planned (Phase 8) |
| T | Hostile output (including injected instructions) steers the system | Output treated as untrusted; strict schemas; capability limits; independent deterministic policy gate | planned (Phase 2, Phase 5) |
| D | Provider outage or runaway cost | Hard budgets, circuit breakers, spend rate limit | implemented (Phase 2) for per-step budgets and bounded retries; circuit breakers and spend rate limit planned (Phase 6) |
| I | Telemetry leaks to a remote endpoint | OTLP endpoint must be http(s) on loopback (`127.0.0.1`, `localhost`, `::1`) | implemented (Phase 0) |

## 4. OWASP Top 10 for LLM Applications (2025) mapping

| ID | Risk | Quartet controls (PRD) | Delivered |
|---|---|---|---|
| LLM01 | Prompt Injection | Untrusted text only in delimited data blocks, never in system prompts (FR-2, §8.3); capability limits; reviewer checks the diff for injection artifacts (FR-19); red-team suite (Phase 10) | FR-2 storage caps: implemented (Phase 1); untrusted-data delimiter wrapper with a per-step nonce: implemented (Phase 2); reviewer: Phase 5; sandbox capability limits: Phase 3; red-team: Phase 10 |
| LLM02 | Sensitive Information Disclosure | No secrets in the sandbox or MCP server (FR-13, §8.4); redaction of logs and spans, prompt export off by default (FR-30); `gitleaks` and `semgrep` on diffs (FR-21); `SecretStr` settings | Settings and gitleaks in repo CI: Phase 0; redaction of the LLM layer: implemented (Phase 2); sandbox: Phase 3; diff scanning: Phase 5; full redaction: Phase 8 |
| LLM03 | Supply Chain | Digest-pinned images; SHA-pinned actions; `pip-audit`, Dependabot, `trivy`, `hadolint` (gates S3, S4, S6); no new dependencies without an explicit flag (FR-21); dependency manifests are blocked paths (§8.5) | Pinning and `pip-audit`: Phase 0; image scanning: Phase 3; dependency-change gate: Phase 5 |
| LLM05 | Improper Output Handling | Strict Pydantic artifact schemas for both vendors, invalid output is a role failure (FR-15); tools run argument vectors without a shell (FR-14); DML parameterised; no `shell=True` rule | Repo rules: Phase 0; artifact schemas: implemented (Phase 1); artifact and tool-input validation: implemented (Phase 2); tool allowlists: Phase 3 |
| LLM06 | Excessive Agency | No git-push, GitHub, network or shell tools (FR-14); orchestrator-only commits, pushes and PRs; blocked and flagged paths (§8.5); human merge with no bypass actors (FR-26) | Tool limits: Phase 3; policy gate: Phase 5; merge gate: Phase 7 |
| LLM10 | Unbounded Consumption | Per-step and per-task caps, `max_fix_rounds = 3`, global spend limiter, kill switch (FR-16, §8.7); sandbox resource limits | Per-step provider budgets: implemented (Phase 2); sandbox limits: Phase 3; per-task limiter and kill switch: Phase 6 |

LLM04 (Data and Model Poisoning), LLM07 (System Prompt Leakage), LLM08 (Vector and Embedding Weaknesses) and LLM09 (Misinformation) are not separately mapped at v0. Quartet fine-tunes no model, uses no vector store, and keeps no secrets in prompts (system prompts are configuration, not secrets). Misinformation is handled through tests and review, with a human merge, rather than a dedicated control. These are to be revisited when the prompt and agent code exists.

## 5. Residual risks

Stated as they stand at the end of Phase 1:

- **CI has still never run on GitHub.** The workflows are statically checked (`actionlint`, `zizmor`) and locally tested, but their behaviour on GitHub-hosted runners, including SHA pins and checksum verification, is unproven until the first push.
- **macOS external-volume ownership is disabled.** The repository lives on an external volume where file ownership is disabled, so the env-file owner check (uid equals current user) is effectively vacuous there. The permission-bit check still applies. On a normal macOS or Linux volume the ownership check is meaningful.
- **Local-dev passwords live in `.env`.** They are owner-supplied (generated randomly at setup, but the repo has no generator and no strength check). The file is git-ignored, so `gitleaks git` (which scans commits) does not scan it; it only guards against the file being committed. Anyone who can read the file can use the local database. The database is bound to loopback only.
- **Makefile selects the repo's `.env`.** Config never reads a `.env` implicitly, but the Makefile sets `QUARTET_ENV_FILE` to the checkout's own `.env`, so `make` targets must not be run from an untrusted checkout.
- **Postgres superuser password in the container environment.** `docker-compose.yml` passes `POSTGRES_PASSWORD` (and the role passwords) to the container as environment variables, visible through `docker inspect` to anyone with Docker access on the host.
- **`sslmode=disable` is allowed.** The database-URL allowlist permits `sslmode`, including `disable`. This is acceptable for the loopback-only dev database and would not be for a remote one.
- **gitleaks allowlist for `docs/plans/`.** The path `^docs/plans/` is allowlisted because the plan contains a deliberately fake token. A real secret committed under that path would not be flagged by gitleaks.
- **Most of the security model is not built.** The sandbox, MCP server, policy gate, GitHub integration, budgets and red-team suite are planned. Until they exist, no claim in the PRD about agent confinement is a property of the running system.
- **Prompt injection cannot be fully prevented.** Mitigation relies on capability limits (planned) and a human merge, not on prompting.
- **Docker on macOS runs in a VM.** UID and mount behaviour differ from Linux CI; sandbox tests must run in both (Phase 3).
- **The app role can UPDATE `tasks`.** This is by design (the state machine and the queue run as `quartet_app`), but it means a leaked app credential can rewrite state, version, leases or budgets directly, bypassing the transition table, and such writes produce no event. Only `events` and `alembic_version` are protected from the app role.
- **Queue session discipline is partly on the caller.** Queue functions refuse a session that already has an open transaction and own their own transaction, but keeping queue operations and `transition()` on separate sessions is a calling convention until the Phase 6 worker loop exists.
- **Fencing starts at the reap.** A worker whose lease has expired but not yet been reaped can still complete a transition; it is fenced once any `claim()` or `reap_expired()` runs.
- **Aggregate artifact size is unbounded** until Phase 2 adds a total-bytes cap per task. (closed in Phase 2; see section 6)
- **`uvx semgrep` and `uvx zizmor` are version-pinned, not hash-locked.** A compromised release under the same version on PyPI would run in CI and locally.
- **OTLP endpoint validation is minimal.** Hardening of the validator is deferred to Phase 8 (ruling R24).

## 6. Threat delta log

One entry per phase. Each phase PR adds an entry: new assets, new boundaries, new mitigations, residual risk (gate S5).

### Phase 0: Foundation and security baseline

What exists now (not future plans):

- **Secrets in settings.** Secret values are `pydantic.SecretStr`, never logged or printed.
- **Env-file loading.** `.env` is never read from the current directory. The file is resolved as: explicit argument, then `QUARTET_ENV_FILE`, then `~/.quartet/.env` if it exists, else none. It must be a regular file owned by the current user and not group- or world-writable, otherwise loading fails with `InsecureEnvFile` (ruling R21). The Makefile exports `QUARTET_ENV_FILE` for its targets.
- **Database URL validation.** URLs use scheme `postgresql` or `postgresql+psycopg` and are validated by libpq's own parser against a key allowlist (`sslmode`, `connect_timeout`, `application_name`). Keys such as `passfile`, `service` and `options` are rejected (ruling R22).
- **Telemetry endpoint.** The OTLP endpoint must be http(s) on a loopback host.
- **Compose stack.** Images are pinned by digest. Ports bind `127.0.0.1` only. Jaeger is v2.
- **Postgres roles.** `quartet_migrator` (database owner) and `quartet_app` are separate login roles; passwords reach psql through variables rather than SQL text. No table grants exist yet; the DML-only grants arrive with the Phase 1 migration.
- **Repository CI (least privilege).** Top-level `permissions: {}`; actions pinned to 40-hex commit SHAs; downloaded binaries checksum-verified; `persist-credentials: false`.
- **Security scanning.** `gitleaks git` over commit history (default rules; the only allowlist is the path `^docs/plans/`, because the plan contains a deliberately fake token); `semgrep` with repo rules (no `shell=True`, no `os.system`, no f-string SQL); `bandit`; `pip-audit`; `actionlint` and `zizmor` for workflows; ruff `S` rules.

New assets: the local Postgres instance and its `.env`. New boundaries: the process-to-env-file and process-to-database edges, and the repo-to-CI edge. Not yet present: sandbox, MCP server, agents, GitHub App, policy gate.

Residual risks: see section 5, in particular that CI has never run on GitHub, that the env-file ownership check is vacuous on this volume, and that local-dev passwords sit in `.env`.

### Phase 1: State store

What exists now (not future plans):

- **New asset: the task DB.** Postgres now holds tasks, their state and leases, artifacts, hand-overs and the audit log, including untrusted issue text at rest (`title`, `body_untrusted`, `external_ref`).
- **Compare-and-set state machine.** Every transition is a compare-and-set on state and version against a static edge table; `resume_state` is recorded on entering `NEEDS_HUMAN` and enforced on leaving it.
- **Append-only, DB-stamped audit log.** `quartet_app` has SELECT and column-level INSERT on `events (task_id, type, payload)` only: `id` and `created_at` always come from the database, and UPDATE, DELETE and TRUNCATE are revoked. Event payloads written by the application (`append_event()`, `transition()`) are capped at 65,536 bytes of compact JSON; the SQL-written `lease_reaped` event carries only the previous `lease_owner` and the new `attempt`.
- **Least-privilege grants.** `quartet_app` has DML only (no CREATE on the schema); `alembic_version` is read-only for it; migrations run as `quartet_migrator` through `QUARTET_MIGRATOR_DATABASE_URL`, with no fallback to the application URL. A test pins the set of tables the app role cannot UPDATE or DELETE, and the migration template reminds authors that default privileges grant DML on new tables.
- **DB-clock leases with fencing on reap.** Claims use `FOR UPDATE SKIP LOCKED` and the database clock. `claim()` reaps expired leases first (so recovery never waits for a reaper loop); a reap bumps `version`, bumps `attempt` only for runnable states, and logs a `lease_reaped` event with the previous owner. Heartbeat refuses an expired lease. Queue functions require a dedicated session with no open transaction and roll back on any error.
- **Untrusted-input handling.** UTF-8 byte caps (title 512, body 65,536, external_ref 512), NUL stripping, rejection of lone surrogates, error messages that never echo content, `source` validated against its allowed values before any write; DB CHECK constraints on title and body length as defence in depth.

New boundaries: the orchestrator-to-database edge now carries untrusted text and is split by role (app versus migrator).

Interface decisions recorded for Phase 6:

- `ClaimedTask.version` is the fencing token: workers pass it to `transition()` as `expected_version`, and a reap invalidates it.
- `transition()` stays lease-agnostic: lease-less edges (`AWAITING_HUMAN -> MERGED`, CLI actions) use the same function; fencing comes from the version bump, not from a lease check.
- `events.task_id` is nullable, for task-less worker and kill-switch events.

Residual risks: the app role can UPDATE `tasks` by design; queue operations rely on callers honouring the dedicated-session rule; fencing starts only at the reap; aggregate artifact size is unbounded until Phase 2; `uvx semgrep` and `zizmor` are version-pinned, not hash-locked; CI has still never run on GitHub; OTLP validator hardening is deferred to Phase 8 (R24). See section 5.

### Phase 2: LLM provider layer

What exists now (not future plans):

- **Two adapters, one loop.** Anthropic uses the beta Messages client, streamed, with strict tools and structured output; `fallbacks` is sent only when a role sets it. OpenAI uses the Responses API over the raw event stream, with `store=False`, strict flat function tools and `text.format`. Both run through one loop (`llm/loop.py`), per [ADR-0004](adr/0004-provider-layer.md).
- **Per-step budgets.** Caps on turns, tool calls, tokens, USD and wall clock, checked before every call. A step can exceed a cap by at most one call (P2-R6). Retries and tool runs share the step clock.
- **Bounded retries.** SDK retries are off. Quartet retries 408, 409, 429, 5xx and connection errors, up to four attempts, with jittered backoff. It honours `retry-after` up to 60 s, and raises `BudgetExceeded` instead of sleeping past the step clock. Other 4xx statuses are not retried.
- **Tool-input validation.** Each tool input is validated with `jsonschema` before the tool runs. An invalid or unknown call is answered with `is_error` and is not executed. Tool output is capped at 64 KiB and wrapped as untrusted data.
- **Untrusted-data delimiters.** Issue text, artifacts, tool output and task text sit in `<untrusted_data_<nonce> kind="…">` blocks. The nonce is random and generated once per step. Any `<` that could open or close such a block is escaped.
- **Artifact validation.** Output is validated against the Pydantic schema. A schema-invalid answer gets at most two repair turns, then `ArtifactInvalid`. A refusal raises `ModelRefused`, and partial refused content is discarded.
- **Redaction of the LLM layer.** Log records are redacted when they are created, including rendered tracebacks and stack text. The hook is a process-wide record factory installed by `build_role_runner`, so it covers records emitted after a runner is built. Exception and span text is redacted before it is stored or raised. Known secrets (API keys and database passwords) are replaced exactly, in plain and percent-encoded form, and well-known shapes are then matched by pattern. Pattern redaction is defence in depth behind the exact replacement.
- **Telemetry without content.** The `role:<name>`, `llm.call` and `tool.call` spans carry metadata only: model, token counts, cost, finish reason, and a sanitised tool name and call ID. Model-chosen identifiers are sanitised before they reach a span or a ledger row. The Anthropic SDK's own spans are set to metadata-only.
- **Cost ledger.** Each call with known usage writes one `llm_calls` row and adds its cost to `tasks.spent_usd` in the same transaction. Tool calls write `tool_calls` rows with redacted, capped arguments. Prices come from the sourced `pricing.toml`. A model missing from that table is costed at the provider's highest rate and flagged as estimated, and the step then stops.
- **Artifact size caps.** Each artifact is capped at 256 KiB and a task's artifacts at 4 MiB in total. This closes the Phase 1 residual "aggregate artifact size is unbounded".
- **Smoke checks and cassettes, built but not run live.** `quartet llm smoke` has a dollar cap and one tool round trip per provider. The cassette recorder and replayer keep response headers on an allowlist and never store request headers, and both are unit-tested offline. No live call, cassette recording or `verify-models` run has happened yet (the Phase 2 live-verification gate).

New assets: the provider API keys, held in memory in the SDK clients and in the redactor's set of known secrets; the recorded cassettes, which hold synthetic prompts only and none are committed yet; and the `llm_calls` and `tool_calls` rows.

New boundary: orchestrator to vendor APIs, over TLS. Quartet sets no base URL, so each SDK's default https endpoint is used. Each SDK also reads its own base-URL environment variable if one is set, and Quartet does not validate it.

Residual risks:

- **Prompt injection is reduced, not prevented.** Delimiters and redaction reduce it. The controls are capability limits (Phase 3) and the human merge (Phase 7).
- **Budget overshoot.** A step can exceed a cap by at most one call (P2-R6).
- **Cost over-estimate.** Cost is over-estimated for calls a fallback served, because every fallback attempt is billed (P2-R5). Eval configurations keep fallback off.
- **Vendor retention.** OpenAI keeps abuse-monitoring logs for up to 30 days even with `store=false`. Anthropic retains data under its own policy.
- **No content export.** Opt-in trace content export is not built (Phase 8).
- **SDK spans.** Native SDK spans depend on the pinned SDK majors. Quartet configures no OpenAI SDK telemetry, and no test covers any spans that SDK may emit.
- **Unrecorded billed calls.** The ledger has rows only for calls with known usage. Two cases are billed by the vendor but not recorded: a call cut off by the step clock, and a retryable in-band OpenAI `failed` response that a retry then replaced.
- **Escaped-quote redaction gap (parked).** Pattern redaction is defence in depth. A few escaped-quote shapes can still leak through it. Examples are `password=ab\"cd ef` in an unquoted value, and a JSON-embedded credential such as `{"error": "{\"password\": \"…\"}"}`. Exact known-secret replacement covers configured keys and passwords. Structured redaction is Phase 8.
- **Cassettes.** Committed cassettes contain synthetic prompts only (policy). Credential scrubbing does not cover content, so a future recording with real content needs its own review.
- **SDK debug logging.** Quartet sets no SDK log level. If someone enables DEBUG on a third-party SDK logger, request content, including prompts, can reach the logs. Logging configuration is Phase 8.
- **Retries.** At most four attempts, bounded by the step clock.
- **Unverified live behaviour.** Live cache behaviour, the model catalog's limits and fallback lists, and the OpenAI SDK against the live API are unproven until the Phase 2 live-verification gate. Prices are a dated snapshot in `pricing.toml`, which that gate re-verifies.
