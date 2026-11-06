# Quartet Threat Model

Status: v0 (Phase 0). Source of the security design: [PRD.md](PRD.md) §8 and §11. Every phase updates this file (gate S5, see [ROADMAP.md](ROADMAP.md)).

**Reading the controls.** Each control is labelled **implemented (Phase 0)** or **planned (Phase N)**. A planned control does not exist yet and must not be relied on. At the end of Phase 0 the repository contains the CLI skeleton, hardened configuration loading, a loopback-only compose stack, and CI security gates. It contains no orchestrator, sandbox, MCP server, agents or GitHub integration.

## 1. Assets

| Asset | Why it matters |
|---|---|
| GitHub App private key and installation tokens | Could push to or alter target repos. |
| Anthropic and OpenAI API keys | Spend and abuse. |
| Host filesystem and developer secrets (`~/.ssh`, `~/.aws`, shell env) | Must never be reachable by an agent. |
| Target-repo integrity: default branch, CI, dependencies | Agent-authored code must not reach the default branch without a human. |
| Target-repo CI credentials | Agent-authored code may run in CI, so CI must hold nothing worth stealing. |
| Hidden-test repository | Leaking it would invalidate evaluation results. |
| PostgreSQL database | Queue, state, leases and the audit log. |
| Budget | Unbounded model spend is an anti-goal. |

## 2. Trust boundaries

Target architecture (PRD §8.2). Only the Postgres, configuration and CI parts exist in Phase 0; the sandbox, orchestrator logic and GitHub flow are planned.

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
| S | A process impersonates a worker and claims tasks | Workers authenticate to Postgres as `quartet_app` | role exists, implemented (Phase 0); worker identity and lease ownership planned (Phase 1, Phase 6) |
| T | Illegal state transition or lost update | Compare-and-set transitions against a static table | planned (Phase 1) |
| T | Hostile `.env` or env-file injects settings | Env file is never read from the cwd; explicit `QUARTET_ENV_FILE` or `~/.quartet/.env`; must be a regular file owned by the user and not group/world-writable | implemented (Phase 0) |
| T | Hostile DB URL redirects the connection or smuggles libpq options | URL parsed with libpq's own parser; scheme and query keys allowlisted (`sslmode`, `connect_timeout`, `application_name`); `passfile`, `service`, `options` and similar rejected | implemented (Phase 0) |
| R | An action cannot be attributed | Immutable `events` table; decision logging (`quartet intervene`) | planned (Phase 1, Phase 7) |
| I | Secrets leak through logs, repr or URLs | Secrets typed as `SecretStr`; never logged or embedded in a logged URL | implemented (Phase 0) for settings; trace and log redaction planned (Phase 8) |
| D | Runaway spend or loops | Per-step and per-task caps, `max_fix_rounds`, kill switch | planned (Phase 2, Phase 6) |
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
| S | Unauthenticated or remote access to the database | Compose binds `127.0.0.1` only; passwords are random, local and kept in the git-ignored `.env` | implemented (Phase 0) |
| T | Application role alters the schema or audit log | Role split: `quartet_migrator` owns the schema, `quartet_app` is DML-only; passwords passed through psql variables, not interpolated into SQL text | role split implemented (Phase 0); no UPDATE or DELETE on `events` planned (Phase 1) |
| T | SQL injection | Bind parameters for DML; DDL composed with `psycopg.sql.SQL`/`Identifier`/`Literal`; semgrep repo rules reject f-string SQL | rules and CI gate implemented (Phase 0); queries themselves arrive in Phase 1 |
| I | Image or supply-chain compromise of the database image | Image pinned by `@sha256:` digest | implemented (Phase 0) |
| D | Lease or claim starvation | Database `now()` for expiry; reaper | planned (Phase 1, Phase 6) |

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
| I | Sensitive data sent to providers | Only the task spec, hand-overs and repo content in the worktree are sent; no host secrets or env reach the model context; prompt/response export to traces off by default | planned (Phase 2, Phase 3, Phase 8) |
| T | Hostile output (including injected instructions) steers the system | Output treated as untrusted; strict schemas; capability limits; independent deterministic policy gate | planned (Phase 2, Phase 5) |
| D | Provider outage or runaway cost | Hard budgets, circuit breakers, spend rate limit | planned (Phase 2, Phase 6) |
| I | Telemetry leaks to a remote endpoint | OTLP endpoint must be http(s) on loopback (`127.0.0.1`, `localhost`, `::1`) | implemented (Phase 0) |

## 4. OWASP Top 10 for LLM Applications (2025) mapping

| ID | Risk | Quartet controls (PRD) | Delivered |
|---|---|---|---|
| LLM01 | Prompt Injection | Untrusted text only in delimited data blocks, never in system prompts (FR-2, §8.3); capability limits; reviewer checks the diff for injection artifacts (FR-19); red-team suite (Phase 10) | FR-2 storage caps: Phase 1; prompts and reviewer: Phase 2 and 5; sandbox capability limits: Phase 3; red-team: Phase 10 |
| LLM02 | Sensitive Information Disclosure | No secrets in the sandbox or MCP server (FR-13, §8.4); redaction of logs and spans, prompt export off by default (FR-30); `gitleaks` and `semgrep` on diffs (FR-21); `SecretStr` settings | Settings and gitleaks in repo CI: Phase 0; sandbox: Phase 3; diff scanning: Phase 5; redaction: Phase 8 |
| LLM03 | Supply Chain | Digest-pinned images; SHA-pinned actions; `pip-audit`, Dependabot, `trivy`, `hadolint` (gates S3, S4, S6); no new dependencies without an explicit flag (FR-21); dependency manifests are blocked paths (§8.5) | Pinning and `pip-audit`: Phase 0; image scanning: Phase 3; dependency-change gate: Phase 5 |
| LLM05 | Improper Output Handling | Strict Pydantic artifact schemas for both vendors, invalid output is a role failure (FR-15); tools run argument vectors without a shell (FR-14); DML parameterised; no `shell=True` rule | Repo rules: Phase 0; schemas: Phase 1 and 2; tool allowlists: Phase 3 |
| LLM06 | Excessive Agency | No git-push, GitHub, network or shell tools (FR-14); orchestrator-only commits, pushes and PRs; blocked and flagged paths (§8.5); human merge with no bypass actors (FR-26) | Tool limits: Phase 3; policy gate: Phase 5; merge gate: Phase 7 |
| LLM10 | Unbounded Consumption | Per-step and per-task caps, `max_fix_rounds = 3`, global spend limiter, kill switch (FR-16, §8.7); sandbox resource limits | Provider budgets: Phase 2; sandbox limits: Phase 3; limiter and kill switch: Phase 6 |

LLM04 (Data and Model Poisoning), LLM07 (System Prompt Leakage), LLM08 (Vector and Embedding Weaknesses) and LLM09 (Misinformation) are not separately mapped at v0. Quartet fine-tunes no model, uses no vector store, and keeps no secrets in prompts (system prompts are configuration, not secrets). Misinformation is handled through tests and review, with a human merge, rather than a dedicated control. These are to be revisited when the prompt and agent code exists.

## 5. Residual risks

Stated as they stand at the end of Phase 0:

- **CI has never run on GitHub.** The workflows are statically checked (`actionlint`, `zizmor`) and locally tested, but their behaviour on GitHub-hosted runners, including SHA pins and checksum verification, is unproven until the first push.
- **macOS external-volume ownership is disabled.** The repository lives on an external volume where file ownership is disabled, so the env-file owner check (uid equals current user) is effectively vacuous there. The permission-bit check still applies. On a normal macOS or Linux volume the ownership check is meaningful.
- **Local-dev passwords live in `.env`.** They are random and the file is git-ignored and gitleaks-scanned, but anyone who can read the file can use the local database. The database is bound to loopback only.
- **gitleaks allowlist for `docs/plans/`.** The path `^docs/plans/` is allowlisted because the plan contains a deliberately fake token. A real secret committed under that path would not be flagged by gitleaks.
- **Most of the security model is not built.** The sandbox, MCP server, policy gate, GitHub integration, budgets and red-team suite are planned. Until they exist, no claim in the PRD about agent confinement is a property of the running system.
- **Prompt injection cannot be fully prevented.** Mitigation relies on capability limits (planned) and a human merge, not on prompting.
- **Docker on macOS runs in a VM.** UID and mount behaviour differ from Linux CI; sandbox tests must run in both (Phase 3).

## 6. Threat delta log

One entry per phase. Each phase PR adds an entry: new assets, new boundaries, new mitigations, residual risk (gate S5).

### Phase 0: Foundation and security baseline

What exists now (not future plans):

- **Secrets in settings.** Secret values are `pydantic.SecretStr`, never logged or printed.
- **Env-file loading.** `.env` is never read from the current directory. The file is resolved as: explicit argument, then `QUARTET_ENV_FILE`, then `~/.quartet/.env` if it exists, else none. It must be a regular file owned by the current user and not group- or world-writable, otherwise loading fails with `InsecureEnvFile` (ruling R21). The Makefile exports `QUARTET_ENV_FILE` for its targets.
- **Database URL validation.** URLs use scheme `postgresql` or `postgresql+psycopg` and are validated by libpq's own parser against a key allowlist (`sslmode`, `connect_timeout`, `application_name`). Keys such as `passfile`, `service` and `options` are rejected (ruling R22).
- **Telemetry endpoint.** The OTLP endpoint must be http(s) on a loopback host.
- **Compose stack.** Images are pinned by digest. Ports bind `127.0.0.1` only. Jaeger is v2.
- **Postgres roles.** `quartet_migrator` (schema owner) and `quartet_app` (DML) are separate roles; passwords reach psql through variables rather than SQL text.
- **Repository CI (least privilege).** Top-level `permissions: {}`; actions pinned to 40-hex commit SHAs; downloaded binaries checksum-verified; `persist-credentials: false`.
- **Security scanning.** `gitleaks` (default rules; the only allowlist is the path `^docs/plans/`, because the plan contains a deliberately fake token); `semgrep` with repo rules (no `shell=True`, no `os.system`, no f-string SQL); `bandit`; `pip-audit`; `actionlint` and `zizmor` for workflows; ruff `S` rules.

New assets: the local Postgres instance and its `.env`. New boundaries: the process-to-env-file and process-to-database edges, and the repo-to-CI edge. Not yet present: sandbox, MCP server, agents, GitHub App, policy gate.

Residual risks: see section 5, in particular that CI has never run on GitHub, that the env-file ownership check is vacuous on this volume, and that local-dev passwords sit in `.env`.
