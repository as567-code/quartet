# Quartet Evaluation Protocol

> **DRAFT, not frozen. Frozen by the signed tag `eval-protocol-v1` and its GitHub Release in Phase 9.**

This document is a verbatim copy of sections 6 and 7 of [PRD.md](PRD.md) as of Draft v0.3. Until the freeze, changes are allowed but must be made in the PRD and copied here, so the two never diverge. After the freeze, any change means a new tag. Section cross-references (for example §8.5, FR-31) point into the PRD.

---

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
