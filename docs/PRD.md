# PRD: Quartet: Multi-Agent Coding Orchestrator

| Field | Value |
|---|---|
| Status | Draft v0.3. Architecture critiqued by the Fable advisor, then independently reviewed (42 findings, all addressed). Ready for owner review. |
| Owner | Aditya Swaroop |
| Date | 2026-11-06 |
| Working name | **Quartet**, after its four agent roles (planner, implementer, tester, reviewer). Rename freely. |
| Companion docs | [ROADMAP.md](ROADMAP.md) (phased build plan + security gates) · [plans/phase-00-01-foundation-state-store.md](plans/phase-00-01-foundation-state-store.md) (first executable plan) · [THREAT_MODEL.md](THREAT_MODEL.md) and [EVAL_PROTOCOL.md](EVAL_PROTOCOL.md) (both created in Phase 0) |

---

## 1. Summary

Quartet takes a scoped GitHub issue and carries it to a pull request a human can merge. Four role-specialised LLM agents do the work: a **planner**, an **implementer**, a **tester** and a **reviewer**. They run on both the **Anthropic Claude** and **OpenAI** APIs and touch code only through **MCP** tools. Every task gets its own **git worktree**. Every agent step runs inside a network-isolated sandbox container. Task state lives in **PostgreSQL**: queue, leases, checkpoints, hand-overs and an audit log. That is what lets work be delegated, handed between agents and recovered after a crash. Nothing merges without a recorded human approval.

The project has two jobs. It is a working tool, and it is the **evidence** behind three resume claims. Every number on the resume has to come from Quartet's own **pre-registered** evaluation (§6–§7), with defined baselines and confidence intervals, so the claim holds up in an interview.

## 2. Problem

Single-agent coding loops fail in predictable ways:

1. **One context does everything.** Planning, coding, testing and self-review share a transcript. The agent grades its own work and misses its own blind spots.
2. **Running in parallel causes collisions.** Agents working on overlapping code at the same time produce conflicting changes that surface only at integration time.
3. **There is no durable state.** A crash loses the work. There is no audit trail of what each agent did, why, or what it cost.
4. **Humans become glue.** A person runs the tests, opens the PR, re-runs failed steps and resolves conflicts.
5. **The blast radius is unsafe.** Agents run model-generated code with the developer's credentials and network, and that code can reach CI. Issue text and repo contents can carry prompt injection.

## 3. Goals, non-goals, anti-goals

### Goals
| ID | Goal |
|---|---|
| G1 | Coordinate four role agents (planner → implementer → tester → reviewer) with structured, persisted hand-offs, using **both** Claude and OpenAI through tool calling and **MCP**. |
| G2 | Run tasks in parallel safely: one **git worktree** per task, file leases kept in **Postgres** and enforced at the tool layer, and rebase-and-retest before PR. Integration conflicts must be measurably lower than a naive parallel baseline. |
| G3 | Keep task state durable and shared in **PostgreSQL**. A crashed worker's task resumes from its last role checkpoint. |
| G4 | Enforce **human review checkpoints**. Quartet can open draft PRs. It can never merge without a recorded human approval. |
| G5 | Trace every task end to end (task → role step → LLM call → tool call) with OpenTelemetry, and account for every token and dollar. |
| G6 | **Auto-open pull requests** carrying the test report, review report, cost and trace ID. |
| G7 | Ship a reproducible **evaluation harness** that produces the three headline metrics against pre-registered baselines. |

### Non-goals (v1)
- No GUI or web dashboard (CLI + GitHub + trace UI only). No multi-tenant or hosted service.
- Target repositories are **Python only** in v1. The design must not block other languages later.
- No issues that need product judgment, UI work or several repositories. No model fine-tuning.

### Anti-goals (a failure even if it "works")
- **Inflated metrics.** Choosing the denominator after the fact, letting the system decide what counted as "scoped", reporting a best-of-N run as first-pass, or tuning prompts or benchmarks against the measured suites.
- **An agent that can merge, push to the default branch, reach CI secrets, edit protected paths without a human gate, or read host secrets.**
- **A framework wrapper.** If LangGraph, the OpenAI Agents SDK, the Claude Agent SDK or Temporal owns delegation, hand-over and recovery, the bullet "designed delegation, handover, and recovery workflows" is false. The **orchestration core is Quartet's own code**. Vendor SDK helpers are allowed *below* it: the per-turn tool loop and MCP tool conversion.
- **Unbounded spend.** Any task able to burn money without a hard cap.

## 4. Users and primary scenarios

**Primary user:** a solo developer or small team maintaining Python repos who wants small, well-specified issues worked in parallel and returned as reviewable PRs.

| # | Scenario |
|---|---|
| S1 | `quartet run --issue owner/repo#123`: one issue goes to a draft PR with test and review reports attached. |
| S2 | `quartet run --label quartet --concurrency 4`: labelled issues run in parallel. Overlapping work is serialised by file leases. |
| S3 | A worker is `kill -9`'d during implementation. Another worker reclaims the task after its lease expires, resets the worktree to the last role checkpoint and re-runs only the interrupted role. Completed roles are never re-run or re-billed. |
| S4 | The task exhausts its 3 fix rounds (test failures or review change requests). It moves to `NEEDS_HUMAN` with a written hand-over note. |
| S5 | A diff touches a flagged path such as `conftest.py`. The PR gets `security-review-required` and stays in draft until a human signs off. |
| S6 | `quartet eval run --suite <suite> --config <config>` reproduces the headline metrics and writes a report. |

## 5. Functional requirements

Priority: **P0** is required for v1 and the resume claims. **P1** is strongly desired. **P2** is nice to have.

### 5.1 Task intake
- **FR-1 (P0)** Ingest tasks from an issue reference, from all open issues with a configured label, or from a benchmark suite file.
- **FR-2 (P0)** Store issue title and body as **untrusted** data, capped at 512 and 65,536 UTF-8 bytes respectively. They are never placed in system prompts. They are passed inside clearly delimited data blocks (§8.3).
- **FR-3 (P0)** A **scope check** classifies each task `in_scope` or `out_of_scope` against written criteria and rejects out-of-scope tasks with a reason. For benchmark runs, scope is fixed in the suite file in advance. The system's own verdict is recorded but **never** used to shrink the denominator (§7).

### 5.2 Planning
- **FR-4 (P0)** The planner (read-only tools) emits a schema-validated **Plan** with these fields: steps, `predicted_files` (path globs, each `read` or `write`), acceptance criteria, test strategy, `risk_level`.
- **FR-5 (P1)** An optional **plan checkpoint** requires human approval when `risk_level = high` or `predicted_files` touches a *blocked* path (§8.5). It is off by default for benchmark runs.

### 5.3 Delegation and scheduling
- **FR-6 (P0)** Workers claim tasks with `UPDATE … WHERE id = (SELECT id … WHERE lease_owner IS NULL AND state = ANY(…) ORDER BY priority, created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING …`. The claim sets `lease_owner` and `lease_expires_at` (DB `now()` + lease). A heartbeat renews the lease. A **reaper** clears expired leases and increments `attempt`. A partial index covers unleased tasks in runnable states. `LISTEN/NOTIFY` wake-ups are P2.
- **FR-7 (P0)** **File leases, three layers.** All lease state is stored in Postgres.
  1. *Scheduling (advisory).* Before implementation, the scheduler acquires write leases on the Plan's `predicted_files` inside a transaction holding `pg_advisory_xact_lock(1, hashtext(repo_id::text))`. A task whose write set overlaps an active lease **waits** in `WAITING_LEASES`. It is never failed for this.
  2. *Tool-layer enforcement.* The workspace MCP server refuses writes outside the task's current lease globs (`LEASE_REQUIRED`). The agent can then call the orchestrator-native tool `request_write_access(path, reason)`. The orchestrator grants the lease if the path is free, or checkpoints and re-queues the task if it is not. Every such event is recorded as a **lease miss** (secondary metric: planner prediction accuracy).
  3. *Post-step verification.* After every role step, the orchestrator diffs the worktree against the previous checkpoint. If any changed path is outside the leased globs, or a tester step changed a non-test path, the step **fails** and the event counts as a policy violation. This closes the gap where code run by `run_tests` could write files directly.
- **FR-8 (P0)** Concurrency is configurable: worker processes, a per-repo limit, and a global LLM-spend rate limit.

### 5.4 Worktrees
- **FR-9 (P0)** Quartet keeps a **bare mirror** of each target repo under `~/.quartet/mirrors/<repo>.git`, with no hooks. Lifecycle:
  - Create: `git worktree add ~/.quartet/worktrees/<task_id> -b quartet/<task_id>-<slug> <base_sha>`, then `git worktree lock --reason task-<task_id> <worktree_path>` while a lease is live.
  - Clean up: `git worktree unlock <path>`, `git worktree remove --force <path>`, `git worktree prune`.
  - Every host-side git call runs as an argument vector with `-c core.hooksPath=/dev/null -c core.fsmonitor=false`.
  - Branch slugs come from an allowlist of characters and are validated with `git check-ref-format`.

  Known gotchas, each with a test:
  - a new worktree holds only tracked files, so dependencies come from the sandbox image, never from copying `.venv` or `.env`;
  - a branch can be checked out in only one worktree;
  - submodules need `git submodule update --init` per worktree;
  - the worktree's `.git` file holds an **absolute host path**, so the sandbox sets `GIT_DIR`/`GIT_WORK_TREE` explicitly (FR-13).
- **FR-10 (P0)** **Role-boundary checkpoints.** When a role completes and passes post-step verification, the orchestrator (never an agent) commits the worktree to the task branch, records the SHA on the role-step row, and writes the role's artifact and a `handovers` row in **one** transaction. Checkpoints are **not** taken mid-turn, because an LLM turn plus its tool side effects cannot be replayed safely.
- **FR-11 (P0)** **Rebase and retest before PR.** The worker takes the per-repo **integration lock**, `pg_advisory_lock(2, hashtext(repo_id::text))`. This is a session-level lock on a dedicated connection, released in `finally` or automatically if the worker dies. Under it: fetch, rebase onto the latest default branch, re-run the full suite in the sandbox, and record pre- and post-rebase results. A conflict found at rebase (textual) or by a post-rebase test failure (semantic) is **recorded as an integration conflict** (§6 M2) **before** any automated resolution. One bounded `resolve_conflict` implementer step follows. If it fails, the task moves to `NEEDS_HUMAN` with a `ConflictReport`.

### 5.5 Agents and tools
- **FR-12 (P0)** Four roles. Each has its own system prompt, tool allowlist, model, effort setting and output schema. All of it is configuration, not code:

| Role | Default model (config) | MCP tools | Output |
|---|---|---|---|
| Planner | Anthropic `claude-opus-5-5`, effort `high` | `list_files`, `read_file`, `search_code`, `git_log` (read-only) | `Plan` |
| Implementer | Anthropic `claude-opus-5-5`, effort `high` | read tools + `write_file`, `str_replace`, `run_tests`, `run_check` (ruff, mypy) + orchestrator-native `request_write_access` | `PatchSummary` |
| Tester | Anthropic `claude-opus-5-5`, effort `medium` | read tools + `write_file`/`str_replace` **limited to test paths** + `run_tests`, `coverage` | `TestReport` |
| Reviewer | OpenAI `gpt-6.1-sol` (Responses API; escalation option `gpt-6-astra`; IDs and prices confirmed from `GET /v1/models` and the OpenAI pricing page in Phase 2) | read tools + `git_diff` (read-only) | `ReviewReport` |

  - The **tester is part deterministic, part LLM.** The deterministic runner executes the existing suite in the sandbox and parses results into the `TestReport`. The LLM part writes **new** tests for the Plan's acceptance criteria. Benchmark hidden tests never enter Quartet's mirrors, worktrees or mounts (§7.1).
  - The **reviewer runs on a different vendor** to reduce correlated blind spots. Ablation A4 (§7.2) tests whether that pays off, and the result is reported either way.
  - Cost levers such as running the implementer on `claude-sonnet-5-5` are an **owner decision**, recorded in config and in the eval report. They are never applied silently.
- **FR-13 (P0)** Agents reach the repo **only through MCP**:
  - Quartet ships a separate, minimal package, **`quartet-workspace`** (MCP server + path guard + lease reader only; no DB, no LLM SDKs). It is built on the official MCP Python SDK v2 (`mcp>=2,<3`; `from mcp.server.mcpserver import MCPServer`; stdio transport).
  - Each **role step** runs in a fresh sandbox container (`docker run -i --rm … quartet-workspace --role <role>`) that serves as the MCP stdio server. Mounts:
    - the worktree at `/work` (RW);
    - the mirror at `/mirror` (RO);
    - the task's lease directory at `/quartet` (RO).
  - The container sets `GIT_DIR=/mirror/worktrees/<task_id>` and `GIT_WORK_TREE=/work`. The admin dir's `commondir` is relative, so git resolves the common dir correctly. The image's `/etc/gitconfig` sets `safe.directory=*`, which is acceptable because the container only ever sees Quartet's own mounts. On Linux the container runs as the host UID:GID, so files keep their owner.
  - The orchestrator is the MCP client. On the Anthropic side, the per-role loop uses the SDK tool runner (`client.beta.messages.tool_runner`) with MCP tools converted by `anthropic.lib.tools.mcp.async_mcp_tool`. Phase 3 verifies this against the pinned `mcp` version, with a small hand-written adapter as the fallback. On the OpenAI side, MCP tool definitions are translated into Responses API function tools.
  - Role allowlists are enforced **twice**: by the client-side filter, and by the server's `--role` flag.
  - The server-side Anthropic MCP connector (`mcp_servers`) is deliberately **not** used, because it reaches only remote URL servers (ADR-0003).
- **FR-14 (P0)** Agents **never** receive git-push, GitHub, network or arbitrary-shell tools. `git_log` and `git_diff` take **no ref arguments**: they are fixed to `<base_sha>..HEAD` of the task's own worktree. `run_tests` and `run_check` run a fixed allowlist of executables with argument vectors (no shell, no operators). Commits, pushes and PRs are orchestrator actions.
- **FR-15 (P0)** Every artifact is a versioned Pydantic v2 model. **One** model source emits the strict JSON Schema for both vendors (`additionalProperties: false`, every field `required`, optional fields as `T | None` with no default):
  - Anthropic uses structured outputs (`output_config.format` / `client.messages.parse()`) and `strict: true` tools.
  - OpenAI uses `text.format` json_schema with `strict` and strict function tools.
  - An invalid or refused artifact counts as a role failure. It is retried at most twice, then the task moves to `NEEDS_HUMAN`.
- **FR-16 (P0)** **Budgets and circuit breakers.** Each role step has hard caps on wall-clock time, LLM turns, tool calls, tokens and USD. Each task has a USD cap and `max_fix_rounds = 3`, which counts test-failure loops and review loops together. A global kill switch exists (`quartet stop --all`). Any breach moves the task to `NEEDS_HUMAN` with a hand-over note. Breaches are **never** silently retried.

### 5.6 Testing
- **FR-17 (P0)** `run_tests` executes in the sandbox (§8.4) and returns structured results: counts, failing test IDs, truncated output. The output is marked untrusted.
- **FR-18 (P0)** "Tests pass" means the full suite passes in the sandbox **after** rebase. A **test-integrity guard** in the policy gate flags these changes:
  - deleting or renaming an existing test function;
  - adding `skip`/`xfail` markers;
  - **any** edit inside the body of an existing test function;
  - any change to test configuration (§8.5 flagged tier).

  A flagged PR gets `security-review-required` and stays in draft until a human signs off. The sign-off is part of the merge review, not an extra intervention.

### 5.7 Review and the fix loop
- **FR-19 (P0)** The reviewer gets the diff, Plan, TestReport and read-only repo access. It returns `approve` or `request_changes` with findings (file, line, severity, rationale). Its prompt includes a check for prompt-injection artifacts in the diff.
- **FR-20 (P0)** On test failure or `request_changes`, a **fresh** implementer conversation starts from the Plan, the latest TestReport or ReviewReport, and the current diff. No transcript is carried over. The loop is bounded by `max_fix_rounds`.
- **FR-21 (P0)** A deterministic **policy gate** runs alongside the LLM reviewer. It covers: post-step verification (FR-7), diff size limits, blocked and flagged paths (§8.5), the test-integrity guard (FR-18), `gitleaks` + `semgrep` on the diff, and **no new or changed dependencies** without an explicit flag. Every violation is counted (secondary metric).

### 5.8 Hand-over and recovery
- **FR-22 (P0)** **Hand-over is a first-class record.** Every role transition writes a `handovers` row with: `from_role`, `to_role`, artifact ID, a context digest (what the next role needs: goal, constraints, open questions), and the attempt number. The next role's conversation is built **only** from the task spec and these hand-overs. When automation gives up, a `HandoverNote` addressed to the human states the goal, what was tried, the current state, the failing evidence and a suggested next step.
- **FR-23 (P0)** **Recovery restarts the interrupted role, never the turn.** On reclaim, the worker resets the worktree to the last role-checkpoint SHA (`git reset --hard <sha> && git clean -fdx`) and re-runs that role from its hand-over input. LLM transcripts are stored for audit and tracing, **not** for resuming. This also keeps every conversation append-only, which current Claude models require (edited history invalidates preserved thinking blocks). Verified by the kill-9 test (S3).
- **FR-24 (P0)** Every state transition is a compare-and-set (`UPDATE tasks SET state=:to, version=version+1 WHERE id=:id AND state=:from AND version=:v`) validated against a static transition table (§9.2), and appends to an immutable `events` table. Entering `NEEDS_HUMAN` records `resume_state`. Leaving `NEEDS_HUMAN` (other than to `CLOSED`) is only allowed to that recorded state.

### 5.9 Human checkpoints and PRs
- **FR-25 (P0)** When automated gates pass, Quartet pushes the branch and opens a **draft** PR. The body carries: summary, linked issue, Plan, TestReport (pre- and post-rebase), review findings, policy-gate results, cost and trace ID. Quartet marks the PR "ready for review" only when every gate is green and no `security-review-required` label is open.
- **FR-26 (P0)** **Merge requires a human.** Target-repo branch protection requires one approving review and the required status checks, with **no bypass actors**. The PR author is the Quartet GitHub App, so the owner's approval is a genuine second party. The App has **no** admin or bypass permission. `quartet merge <task>` is only a convenience: through the API it checks that a human approval exists on the **current head SHA**, and it refuses otherwise. Tests prove that a merge attempt with the App's token is rejected.
- **FR-27 (P1)** `quartet approve-plan`, `quartet reject --note` and `quartet intervene <task> <kind>` record decisions and interventions (actor, time, note) in Postgres.

### 5.10 Observability
- **FR-28 (P0)** OpenTelemetry span tree: `task`, then `role:<name>`, then `llm.call` and `tool.call`. The Anthropic SDK's native OpenTelemetry spans (beta, `gen_ai.*` attributes) nest under Quartet's spans. OpenAI calls are wrapped in Quartet spans that follow the same GenAI conventions. Spans export over OTLP to local **Jaeger v2**.
- **FR-29 (P0)** Every LLM call writes a row to `llm_calls` with: provider, model, input, output and cache tokens, cost, latency, stop reason and trace ID. `quartet status` and `quartet cost --task <id> [--by role|model]` read from it.
- **FR-30 (P0)** **Redaction.** Secrets, tokens and env values are scrubbed from logs and spans. Exporting prompt and response content to traces is **off by default** and allowed only locally.

### 5.11 Evaluation harness
- **FR-31 (P0)** `quartet eval run` executes a frozen suite under a named config. Every result file records the model IDs, Quartet git SHA, suite tag, sandbox image digests and date, and the harness writes a per-task CSV plus a Markdown report.
- **FR-32 (P0)** The harness computes M1 to M3 exactly as defined in §6, with confidence intervals, and an **integration simulator** merges completed branches into `main` in completion order for M2.

## 6. Success metrics (definitions are binding and pre-registered)

The resume numbers are **targets**. The resume shows the **measured** values from the final report, whatever they are. A lower, reproducible number beats a round one. These definitions are frozen in `docs/EVAL_PROTOCOL.md` and the protocol tag is pushed publicly before any measured run (§7.3).

### M1: First-pass completion ≥ 70% on scoped GitHub issues
- **Population:** the `scoped-60` suite (§7.1). The denominator is **all 60**, including rejections, timeouts and budget breaches. M1 is an **absolute rate**. It needs no baseline, and B-single is an optional reference point only.
- **First-pass:** the task's head commit when it **first finishes `INTEGRATING`**, from one orchestrator run with **zero human interventions**. In local mode (the SWE-bench split) this is the exported patch. In GitHub mode it is the head of the first PR Quartet opens. Quartet's internal bounded loops (≤ 3 fix rounds, one conflict resolution) count as part of the system, and the report says so. A task that never reaches that point counts as not completed.
- **Completed:** the hidden acceptance tests (FAIL_TO_PASS) pass **and** the existing tests (PASS_TO_PASS) still pass, judged in an isolated harness (§7.3) on that commit.
- **Which run counts, fixed in advance:** for the SWE-bench split, the single `full` run of those 30 tasks. For the self-authored split, the `full` concurrency-4 run of `fixture-30`, the same run M3 uses. Each task has exactly one first-pass result, so there is no "better of two" choice.
- **Reported:** overall rate and the rate per split, each with a **Wilson 95% CI** (about ±12 points at N = 60), plus cost per completed task.

### M2: Integration conflicts reduced ≥ 50%
- **Unit of analysis:** a **batch** of k = 4 tasks on one repo, run concurrently. There are ≥ 12 batches per arm, drawn from a **pre-registered** overlap-cluster design, with the same batches and seeds in every arm. Tasks repeat across batches, so the CI is a **bootstrap over batches**.
- **Integration conflict:** an event where a task's changes collide with changes integrated by another task in the same batch. It is counted **wherever it is detected and before any automated resolution**:
  - (a) a textual conflict at rebase or at merge;
  - (b) a semantic conflict: the branch passed its own tests, but the suite fails after rebase or merge.

  Because a conflict found at rebase still counts, rebase alone cannot reach zero.
- **Arms (same agents, prompts and models; each task in its own worktree):**
  - `B-naive`: no file leases, no rebase-and-retest. All branches come from the batch's base SHA and are merged in completion order.
  - `B-rebase-only`: rebase-and-retest + integration lock, no leases.
  - `full`: Postgres file-lease scheduling + tool-layer enforcement + rebase-and-retest + integration lock.
- **Metric:** conflicts per 100 integrated tasks. **Headline:** reduction = 1 − (full ÷ B-naive). **Decomposition:** B-rebase-only vs B-naive shows the share due to rebase, and full vs B-rebase-only shows the share due to leases. **Trade-off:** batch wall-clock time and throughput per arm, because leases serialise overlapping work.
- **Calibration:** the cluster design is piloted **once, on `dev-15` repos only**, so that B-naive is expected to conflict in ≥ 30% of tasks. The design is then frozen. It is never redesigned against the measured suites.

### M3: Manual interventions reduced ≥ 40% in parallel execution
- **Manual intervention:** any human action, beyond the single mandatory merge review (including any security sign-off), needed to take a task from intake to a merge-ready PR. That covers: re-running a role, editing code, resolving a conflict, running tests by hand, creating or editing a PR by hand, restarting a stuck or crashed task, or resolving a `NEEDS_HUMAN`. Each is logged with `quartet intervene`, and the obvious ones are detected automatically (a non-bot commit on a `quartet/*` branch, a PR opened by a non-bot user).
- **Arms, both at concurrency 4 on `fixture-30` with the same pre-registered fault-injection schedule (3 random worker kills per run):**
  - `B-noauto`: an ablation with these off: the tester role (deterministic and LLM), auto-PR, automatic retry on test failure, and crash auto-recovery. Every other part is identical.
  - `full`: everything on.
- **Intervention protocol (in EVAL_PROTOCOL):** the owner operates both arms using a written checklist. It fixes the trigger conditions (for example, a task idle > 10 minutes, `NEEDS_HUMAN`, failing tests), a maximum response delay, and scripted actions per trigger. Every action is logged through the CLI with a timestamp.
- **Metric:** interventions per task. Reduction = 1 − (full ÷ B-noauto), with a bootstrap CI. **Robustness check:** the metric is also reported **excluding** PR-creation and manual-test-run interventions, so the result does not rest on definitional wins.

### Secondary metrics (reported, not claimed)
Cost per completed task (USD), median wall-clock time, tokens by role and model, fix-round distribution, lease-miss rate (planner prediction accuracy), policy-violation counts, recovery success in kill tests (target 100%), and **security incidents during evals (target 0)**: no sandbox escape, no secret in any diff, log or span, no unflagged change to a blocked or flagged path.

## 7. Evaluation methodology

### 7.1 Suites (authored and tagged **before Phase 5**, before any prompt is written)
| Suite | Composition | Used for |
|---|---|---|
| `scoped-60` | **30 SWE-bench Verified tasks** from 3–4 Python repos, chosen by a pre-registered filter (difficulty `"<15 min fix"` or `"15 min - 1 hour"`, gold patch ≤ 3 files) and sampled with a fixed seed, **plus the 30 self-authored `fixture-30` issues**. | M1 |
| `fixture-30` | 30 self-authored issues on 2–3 forked small OSS Python repos, grouped into pre-registered **overlap clusters**. Each issue has hidden acceptance tests. | M1 (self-authored split), M2, M3, end-to-end GitHub flow |
| `dev-15` | 15 issues on **different** repos from `fixture-30`. | Prompt and config iteration, M2 cluster calibration, the Phase 9 pilot. Tuning **never** touches the measured suites. |

- **Hidden tests** live in a **separate private repository** that Quartet never clones, mirrors or mounts. The harness pulls them into the judging container only (§7.3). A Phase 9 test asserts that no hidden-test path or ref appears in any Quartet mirror.
- **Why the hybrid:** SWE-bench Verified is the recognisable name, but its issues and fixes are public and likely in model training data. The self-authored split was written **before** Quartet's prompts existed and after the models' training cutoffs, so it is the contamination-resistant evidence. Each split is reported separately.
- Suite files record task IDs, base SHAs, scope criteria, seed and cluster assignments. They are tagged `suites-v1` and pushed. Any change means a new tag.

### 7.2 Configurations
| Config | Purpose |
|---|---|
| `full` | The full system |
| `B-naive`, `B-rebase-only` | M2 arms |
| `B-noauto` | M3 baseline |
| `B-single` (optional) | One implementer agent with the same tools and budget, as a reference point for M1 |
| `A4-same-vendor` (optional) | Reviewer on Claude instead of OpenAI, run on 20 tasks |

### 7.3 Rigor rules
- **Pre-registration:** before the first measured run, a **signed** tag `eval-protocol-v1`, covering EVAL_PROTOCOL.md and the suite tags, is pushed to the public Quartet repo with a GitHub Release, whose server-side timestamp is the evidence. A Zenodo DOI is optional. Commit timestamps alone are not accepted as evidence, because authors can set them.
- **Judging isolation:**
  - *Self-authored split:* a fresh container is started from the task's sandbox image, the task's diff is applied to the base commit, and the hidden tests are copied in. The tests run with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -c <harness>/pytest.ini --rootdir <harness> --noconftest -p no:cacheprovider`, so PR-supplied pytest configuration, conftest files and plugins cannot affect the verdict. Hidden tests are written to be self-contained.
  - *SWE-bench split:* judged by the **official `swebench` harness**.
- Pin everything in each result file (FR-31). Keep **every** run in `eval/results/`, including bad ones. The report lists all runs.
- **Variance (optional):** 2 extra `full` repeats on a seeded 15-task subset of `scoped-60`. They are reported separately and never replace a first-pass result.
- Rates get Wilson 95% intervals. Per-task and per-batch means get bootstrap 95% intervals.
- **If a target is missed, the resume shows the measured number** (for example, "58% (95% CI 46–70%) first-pass completion on 60 scoped issues"), or the bullet is reworded. This rule is not negotiable.

### 7.4 Budget (estimate; Phase 5 measures the real cost per task)
Prices: Opus 5.5 at $4 per million input tokens and $20 per million output, cache reads $0.20 per million (Anthropic reference, 2026-10). `gpt-6.1-sol` at $2/$10 per million (third-party report; verify in Phase 2). With prompt caching and four roles, the working estimate is **$2–4 per task-run**.

| Line | Task-runs | Core? |
|---|---|---|
| Phase 5 cost measurement + dev iteration (`dev-15`) | 35 | core |
| Phase 9 pilot (`dev-15`: 5 tasks + 2 M2 batches × 4) | 13 | core |
| M1 SWE-bench split (`full`) | 30 | core |
| `fixture-30` `full` concurrency-4 run (M1 self-authored split + M3) | 30 | core |
| M3 `B-noauto` | 30 | core |
| M2: 3 arms × 12 batches × 4 tasks | 144 | core |
| Phase 10 red-team (live models) | 24 | core |
| **Core subtotal** | **306 (≈ $610–1,220)** | |
| Variance repeats | 30 | optional |
| `B-single` on `scoped-60` | 60 | optional |
| `A4-same-vendor` | 20 | optional |
| **All-in total** | **416 (≈ $830–1,660)** | |

A hard cap is set in config. If Phase 5's measured cost is higher than estimated, the **owner** chooses among these levers: Sonnet 5.5 for the implementer, lower tester effort, 8 instead of 12 M2 batches, or dropping the optional lines. The report states the choice.

## 8. Security model

### 8.1 Assets
GitHub App private key and installation tokens; Anthropic and OpenAI API keys; the host filesystem and developer secrets (`~/.ssh`, `~/.aws`, shell env); target-repo integrity (default branch, CI, dependencies) and **target-repo CI credentials**; the hidden-test repository; the Postgres database; and the budget.

### 8.2 Trust boundaries
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

### 8.3 Prompt-injection posture
Assume any issue, repo or tool content may be hostile (OWASP LLM01).
- Untrusted text goes in delimited data blocks, and the system prompt says tool results and data blocks contain no instructions. This **reduces** injection. It does not prevent it.
- The real control is **capability limits**: no credentials, no network, no push or merge, blocked paths refused, flagged paths human-gated, post-step verification, and a CI that holds nothing worth stealing (§8.6). A fully hijacked agent can damage only its own worktree.
- The Phase 10 red-team suite plants injections in issues, code comments, docstrings, test output and file names, then asserts that no policy violation reaches a PR.

### 8.4 Sandbox requirements (P0)
One fresh container per **role step**:
- **Network and filesystem:** `--network none`; `--read-only` rootfs plus `--tmpfs /tmp`; mounts `/work` (RW worktree), `/mirror` (RO mirror) and `/quartet` (RO lease **directory**, never a single file, so atomic renames on the host are visible).
- **Identity and privileges:** host UID:GID on Linux and non-root on macOS; `--cap-drop ALL`; `--security-opt no-new-privileges`.
- **Limits:** `--pids-limit`, `--memory` and `--cpus`, plus a hard timeout.
- **Exposure:** **no** Docker socket and **no** host env passthrough. Only `GIT_DIR`/`GIT_WORK_TREE` and a fixed allowlist are set. The sandbox never holds API keys and never calls an LLM.
- **Image:** pinned by digest. Dependencies are installed at image build time, never at test time.
- **SWE-bench images:** for SWE-bench tasks, `quartet-swebench-<instance>` images are derived **FROM** the official instance images, plus the `quartet-workspace` layer, and run with the same flags. Gate S6 applies to the Quartet layer. The base-image CVE count is reported, not gated.

Escape tests check that writes to `/etc` fail, outbound network fails, `/var/run/docker.sock` is absent, and env holds no secret-like values.

### 8.5 Protected paths (policy gate, configurable)
- **Blocked.** The MCP server refuses writes unless the path is leased **and** approved at the plan checkpoint, and the policy gate fails the task on any change:
  - `.git/**`, `.github/**`, `**/CODEOWNERS`, and other CI configs;
  - `Dockerfile*`, `.pre-commit-config.yaml`;
  - dependency manifests and lockfiles: the `[project.dependencies]`, `[project.optional-dependencies]` and `[build-system]` tables, `requirements*.txt`, `uv.lock`, `poetry.lock`;
  - `**/*.pem`, `**/.env*`, `sitecustomize.py`, `usercustomize.py`, `*.pth`.
- **Flagged.** Writes are allowed, but the PR gets `security-review-required` and stays in draft until a human signs off:
  - `conftest.py` at any level, `pytest.ini`, `tox.ini`, `setup.cfg`, `setup.py`, `noxfile.py`, `Makefile`, `[tool.pytest*]` tables;
  - existing test functions (FR-18);
  - auth and crypto modules (configurable globs).
- Secret-like content anywhere in the diff blocks the PR.

### 8.6 Credentials and CI
- **GitHub App (not a PAT):** installed only on target repos, with `contents: write`, `pull_requests: write`, `issues: read` and `metadata: read`. No `administration`, no `workflows`, no bypass. Installation tokens are short-lived and only ever passed in the environment of the single `git push`.
- **Target-repo CI hardening**, checked by a Phase 7 test using the owner's token:
  - default workflow `GITHUB_TOKEN` permissions set to **read**;
  - "Allow GitHub Actions to create and approve pull requests" **off**;
  - **no** repository or organisation secrets or variables reachable from `pull_request` workflows;
  - no self-hosted runners;
  - workflows triggered by `pull_request` only, never `pull_request_target`.

  Agent-authored code may run in CI, so CI must hold nothing worth stealing.
- **API keys:** loaded from the macOS Keychain or a git-ignored `.env` that `gitleaks` scans. Keys never reach the sandbox or the MCP server.
- **Postgres:** the `quartet_app` role has DML only (no UPDATE or DELETE on `events`), and `quartet_migrator` owns the schema. All SQL is parameterised. DDL (which cannot take bind parameters) is composed with `psycopg.sql.SQL`/`Identifier`/`Literal`.

### 8.7 Bounded consumption (OWASP LLM10)
Per-role and per-task caps (FR-16), a global spend rate limiter and the kill switch.

## 9. Architecture

### 9.1 Components
```
quartet CLI ──► orchestrator worker processes (N)
                 ├─ scheduler ........ claim (SKIP LOCKED), file leases, integration lock, reaper
                 ├─ state machine .... CAS transitions, events, handovers, resume_state
                 ├─ role runner ...... per-role conversation ─► provider layer ─► Anthropic | OpenAI
                 │                       └─► MCP client ──stdio──► [sandbox: quartet-workspace]
                 ├─ git manager ...... mirror, worktrees, checkpoint commits, rebase+retest
                 ├─ policy gate ...... post-step diff check, protected paths, test integrity, scans, deps
                 ├─ GitHub adapter ... App auth, issue intake, push, draft PR, ready, merge check
                 └─ telemetry ........ OTel spans, cost ledger, redaction
PostgreSQL 16 ◄── all state              Jaeger v2 ◄── OTLP
```

### 9.2 Task state machine
```
QUEUED → SCOPING ─(out_of_scope)→ REJECTED
           └→ PLANNING → [PLAN_REVIEW]* → WAITING_LEASES → IMPLEMENTING → TESTING → REVIEWING
IMPLEMENTING ─(lease unavailable)→ WAITING_LEASES
TESTING   ─(tests fail, fix round < 3)→ IMPLEMENTING
REVIEWING ─(request_changes, fix round < 3)→ IMPLEMENTING
REVIEWING ─(approve)→ INTEGRATING ─(GitHub mode)→ PR_DRAFT → AWAITING_HUMAN → MERGED
                                  └(local mode)→ AWAITING_HUMAN
every non-terminal state except NEEDS_HUMAN ─(cap breach · rounds exhausted · unrecoverable)→ NEEDS_HUMAN
every non-terminal state ─(human closes · kill switch with --close)→ CLOSED
NEEDS_HUMAN ─(human resolves)→ exactly the recorded resume_state
Terminal: REJECTED, MERGED, CLOSED          * PLAN_REVIEW only when FR-5 triggers
```
The exact edge table is pinned in `docs/plans/phase-00-01-foundation-state-store.md`, Task 9.

### 9.3 Data model (PostgreSQL 16, SQLAlchemy 2.0 typed ORM + Alembic; queue and lease queries in explicit Core SQL)
Every table has `id` and `created_at timestamptz DEFAULT now()`. Mutable tables also have `updated_at`.

| Table | Key columns |
|---|---|
| `repos` | `owner`, `name`, `default_branch`, `mirror_path` |
| `tasks` | `id uuid`, `repo_id`, `source`, `external_ref`, `title`, `body_untrusted`, `state task_state`, `resume_state task_state NULL`, `version`, `priority` (lower first), `attempt`, `risk_level`, `lease_owner`, `lease_expires_at`, `budget_usd`, `spent_usd` |
| `role_steps` | `task_id`, `role`, `round`, `attempt`, `status`, `UNIQUE (task_id, role, round, attempt)`, `checkpoint_sha`, `output_artifact_id`, `error` |
| `artifacts` | `task_id`, `kind` (plan, patch_summary, test_report, review_report, conflict_report, handover_note), `schema_version`, `payload jsonb` |
| `handovers` | `task_id`, `from_role`, `to_role`, `artifact_id`, `context_digest jsonb`, `attempt` |
| `llm_calls` | `step_id`, `provider`, `model`, token counts (in, out, cache read, cache write), `cost_usd`, `latency_ms`, `stop_reason`, `trace_id` |
| `tool_calls` | `step_id`, `tool`, `args_redacted jsonb`, `is_error`, `duration_ms` |
| `file_leases` | `task_id`, `repo_id`, `path_glob`, `mode`, `acquired_at`, `released_at`, `source` (plan, extension) |
| `worktrees` | `task_id`, `path`, `branch`, `base_sha`, `head_sha`, `status` |
| `checkpoints` | `task_id`, `kind` (plan, merge, security), `status`, `decided_by`, `decided_at`, `note` |
| `interventions` | `task_id`, `kind`, `actor`, `source` (cli, auto) |
| `pull_requests` | `task_id`, `number`, `url`, `state`, `head_sha`, `draft` |
| `events` | append-only audit log: `bigserial id`, `task_id`, `type`, `payload jsonb` (`quartet_app` has no UPDATE, DELETE or TRUNCATE) |

Claim index: `ix_tasks_claimable ON tasks (priority, created_at) WHERE lease_owner IS NULL AND state IN (<runnable states>)`.

### 9.4 Provider layer
One internal interface, `RoleRunner.run(role_config, handover_input, tools) -> RoleResult` (artifact + usage + trace ID), with two adapters:

**Anthropic** (`anthropic` SDK):
- Messages API through the beta tool runner, `strict: true` tools and structured outputs.
- Adaptive thinking is the default on Opus 5.5, so the adapter does not send a `thinking` disable. Effort is set per role through `output_config.effort`; it must be set explicitly because Opus 5.5 defaults to `medium`.
- **Never** send `temperature`/`top_p`/`top_k`, and **never** prefill.
- `tool_choice` is `auto` only, because forced tool choice returns a 400 on Opus 5.5 and Sonnet 5.5.
- Streaming for long turns with `eager_input_streaming: true` on client tools, each parsed input validated against its schema.
- Handle the `refusal` stop reason and enable server-side `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`).
- Byte-stable tools and system prompt per role, with the issue text last, so prompt caching works. `usage.cache_read_input_tokens` is checked in tests.
- Parallel tool results are returned in one user message, with `is_error` set on failures.

**OpenAI** (`openai` SDK, Responses API):
- Flat function tools (`{type:"function", name, parameters, strict:true}`). `function_call` items are answered with `function_call_output` keyed by `call_id`.
- Continuation resends the input list, so Quartet keeps the full transcript itself. `previous_response_id` is optional.
- Structured output through `text.format` json_schema strict, and the `reasoning` effort set per role.

**FakeProvider** replays recorded turns so every unit and integration test runs without API keys. `quartet llm smoke` proves that each vendor accepts every artifact schema, including `schema_version` (emitted as `const`).

### 9.5 Repository layout (target, uv workspace)
```
pyproject.toml (workspace root) · uv.lock · .python-version (3.12) · alembic.ini · Makefile
docker-compose.yml (postgres, jaeger v2) · docker/postgres/init/01-roles.sh · scripts/db_bootstrap.py
src/quartet/
  cli.py · config.py · doctor.py
  db/           models.py · session.py · queue.py · repository.py · alembic/
  domain/       states.py · transitions.py · artifacts.py (pydantic, versioned)
  llm/          base.py · anthropic_runner.py · openai_runner.py · fake.py · pricing.py · budget.py
  mcp_client/   client.py · tool_policy.py
  sandbox/      docker_runner.py · images/workspace.Dockerfile
  gitops/       mirror.py · worktrees.py · checkpoints.py · rebase.py
  agents/       planner.py · implementer.py · tester.py · reviewer.py · prompts/
  orchestrator/ worker.py · scheduler.py · leases.py · recovery.py · handover.py · policy_gate.py
  github/       app_auth.py · intake.py · pull_requests.py
  telemetry/    tracing.py · cost_ledger.py · redaction.py
  eval/         suites.py · runner.py · judge.py · integration_sim.py · metrics.py · report.py
packages/quartet-workspace/src/quartet_workspace/   server.py · pathguard.py · leasefile.py · tools/
tests/ unit/ · integration/ · e2e/ · security/ · repo_hygiene/
eval/ suites/ · results/ · reports/
docs/ PRD.md · ROADMAP.md · THREAT_MODEL.md · EVAL_PROTOCOL.md · adr/ · plans/
```

## 10. Technology decisions

| Area | Decision | Why |
|---|---|---|
| Language | Python **3.12** pinned via `uv` (`.python-version`) | Matches the resume. 3.14 is installed locally but some wheels may lag. |
| Packaging | `uv` workspace (`quartet` + `quartet-workspace`) + `uv.lock` | Reproducible. The sandbox image ships only the minimal package. |
| DB | PostgreSQL 16 (Docker) · SQLAlchemy 2.0 typed ORM · psycopg 3 · Alembic | Typed models + real migrations. Explicit SQL for the queue. |
| Orchestration | **Hand-written** state machine, worker loop, leases and recovery | Anti-goal §3. Temporal, LangGraph and the Agent SDKs were considered and rejected (ADR-0001). |
| Per-role tool loop | Anthropic SDK tool runner. A custom loop for OpenAI. | Thin helpers below the orchestration core |
| MCP | Official `mcp` Python SDK v2 (`mcp>=2,<3`, 2.0.0 released 2026-07-28; `MCPServer`, `Client`, stdio). The in-memory `Client` is used for tests. | Matches the resume. Current major version. |
| Schemas | Pydantic v2 | One source of truth for artifacts and both vendors' strict schemas |
| CLI | Typer | Simple and typed |
| Sandbox | Docker via CLI subprocess with an argument vector (no shell) | Already installed. Flags are easy to assert in tests. |
| GitHub | GitHub App + `githubkit` | Least privilege, short-lived tokens |
| Tracing | OpenTelemetry SDK + OTLP → **Jaeger v2** (`cr.jaegertracing.io/jaegertracing/jaeger`; v1 reached EOL) | Standard, local, free |
| Quality | ruff · mypy `--strict` · pytest · pytest-cov · hypothesis · time-machine | Gates in every phase |
| Security tooling | gitleaks · semgrep · bandit · pip-audit · trivy · hadolint · actionlint · zizmor | §11 |

## 11. Quality and security gates (every phase)

Every phase in [ROADMAP.md](ROADMAP.md) ends with **all** of these, **plus** its own phase-specific security checks. A phase is not done until they pass in CI on the phase PR.

| Gate | Check | Pass criterion |
|---|---|---|
| Q1 | `ruff check` + `ruff format --check` | 0 findings |
| Q2 | `mypy --strict src/` (tests with a relaxed override) | 0 errors |
| Q3 | `pytest` (unit + this phase's integration tests) | 100% pass. Coverage ≥ 85% on `src/`. |
| S1 | `gitleaks` (pre-commit + CI, full history) | 0 findings |
| S2 | `semgrep` (`p/python`, `p/security-audit`, `p/secrets` + repo rules) · `bandit -r src` · ruff `S` rules | 0 high or medium findings. Each low finding is triaged in writing. |
| S3 | `pip-audit` on the locked dependency set + Dependabot enabled | 0 known vulnerabilities, or a documented exception with an expiry date |
| S4 | CI hardening: actions pinned to full SHAs, top-level `permissions: {}`, `actionlint` + `zizmor` | 0 findings |
| S5 | **Threat delta:** the phase PR updates `THREAT_MODEL.md` (new assets, boundaries, mitigations, residual risk) | Reviewed and committed |
| R1 | Claude Code **`/code-review`** and **`/security-review`** on the phase branch | Every confirmed finding is fixed, or ticketed with a justification |
| R2 | Owner self-review against the phase checklist + a recorded demo of the exit criteria | Signed off in the phase PR |
| S6 | (Phase 3 onward) `trivy image` + `hadolint` on Quartet-built images | 0 critical or high findings in Quartet layers |

## 12. Risks and mitigations

| Risk | L | I | Mitigation |
|---|---|---|---|
| M1 lands below 70% | M | H | Honest reporting (§7.3). Iterate prompts only on `dev-15`. |
| Agent code poisons target-repo CI | M | H | §8.6 CI hardening with a test, flagged paths (§8.5), human review before CI results are trusted |
| SWE-bench harness integration is heavy | H | M | Start with a 5-task smoke test. Use the official `swebench` harness and derived images. The self-authored split can stand alone. |
| API cost overrun | M | M | Measure in Phase 5. Hard caps (FR-16). Prompt caching. The owner picks the levers (§7.4). |
| Docker on macOS (VM) behaves differently from Linux CI | M | M | Run sandbox tests in Linux CI as well as locally. Document UID and mount differences. |
| A prompt injection causes harm | M | H | Capability limits (§8.3). Red-team suite. Human merge gate. |
| Provider API or model changes mid-project | M | M | Pinned IDs. Adapter contract tests. FakeProvider tests. |
| MCP v2 and Anthropic MCP helpers incompatible | L | M | Verified in Phase 3. Fallback: convert MCP tools to SDK tools by hand (about 30 lines). |
| Scope creep (UI, languages) | H | H | Non-goals (§3). Cut list in ROADMAP. |

## 13. Resume-claim traceability

| Resume phrase | Requirements | Evidence |
|---|---|---|
| "planner, implementer, tester, and reviewer coding agents" | FR-12, FR-15, FR-19–20 | `src/quartet/agents/*`, artifact schemas, a Jaeger trace showing all four roles |
| "Claude and OpenAI APIs with tool calling" | FR-12, §9.4 | Both runners. `llm_calls` rows per provider. |
| "and MCP" | FR-13 | The `quartet-workspace` MCP server (`mcp` v2) + client. Every repo action goes through MCP. |
| "70%+ first-pass task completion on scoped GitHub issues" | FR-3, FR-31, M1, §7 | `eval/reports/scoped-60.md` with CI and per-split rates |
| "delegation, handover, and recovery workflows" | FR-6–8, FR-22–24 | Scheduler, `handovers` table, kill-9 recovery test |
| "isolated Git worktrees" | FR-9–11 | `gitops/worktrees.py`. Each role-step sandbox mounts only its worktree. |
| "shared task state in PostgreSQL" | §9.3, FR-7 | Schema, queue and lease tables |
| "cutting integration conflicts 50%" | FR-7, FR-11, M2 | `eval/reports/fixture-30-conflicts.md` (three arms + decomposition) |
| "enforcing human review checkpoints before merge" | FR-5, FR-25–27 | Branch protection, `checkpoints` table, the refused-merge test |
| "Traced, tested, and auto-opened pull requests" | FR-17–18, FR-25, FR-28–30 | Jaeger traces, test reports in PR bodies, draft PRs |
| "reducing manual intervention 40% across parallel task execution" | FR-8, FR-27, M3 | `eval/reports/fixture-30-interventions.md` (B-noauto vs full, with the robustness check) |

## 14. Anticipated interview questions (the design must answer each)
- *"70% of what?"* All 60 pre-registered tasks, first-pass as defined in §6, per-split CIs and a per-task CSV in the repo.
- *"When did you fix the definitions?"* The signed `eval-protocol-v1` tag and GitHub Release predate every result file.
- *"Isn't SWE-bench contaminated?"* Yes. Half the suite is self-authored, written before the prompts existed, with hidden tests in a repo Quartet never sees. Each split is reported separately.
- *"Rebase alone removes conflicts. What did leases add?"* The three-arm decomposition answers it. Conflicts found at rebase still count, so rebase cannot hide them.
- *"What's the cost of leases?"* Throughput per arm is reported next to the conflict rate.
- *"What if the planner predicts the wrong files?"* The tool layer enforces leases, the post-step diff check catches anything else, and lease misses are measured.
- *"Couldn't test code write files or rig pytest?"* Post-step verification, flagged test config, and judging with `--noconftest` plus the harness's own config.
- *"How do you resume mid-LLM-turn?"* We don't. The role restarts from its last git checkpoint, which also keeps every conversation append-only.
- *"Why Postgres and not Redis or Temporal?"* One transactional store for queue, leases, state and audit. Temporal would own the recovery design the bullet claims (ADR-0001).
- *"Can an agent merge or steal CI secrets?"* No. The refused-merge test, the App's scopes, read-only CI tokens and no CI secrets.
- *"Did you really use MCP?"* Yes: a real `mcp` v2 server in each sandbox and a real MCP client. ADR-0003 explains why the server-side connector wasn't used.
- *"Does the cross-vendor reviewer help?"* The A4 ablation result, if it was run, whatever it shows.

## 15. Builder decisions (left open on purpose)
Exact prompt wording (iterate on `dev-15` only) · which forked OSS repos to use (they must meet the overlap-cluster requirement) · Jaeger vs Phoenix or Langfuse for the trace UI (all take OTLP).

## 16. Open questions for the owner
1. **Eval budget:** is about $610–1,220 (core) acceptable, or $830–1,660 with the optional lines? If not, which lever (§7.4)?
2. **GitHub home:** which account or org hosts the public Quartet repo, the forked fixture repos, the private hidden-test repo and the Quartet GitHub App?
3. **Public repo:** will Quartet and its eval results be public? Recommended, and pre-registration (§7.3) depends on a public tag.
