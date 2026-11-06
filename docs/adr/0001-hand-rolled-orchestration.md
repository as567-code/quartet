# ADR-0001: Hand-rolled orchestration core

- Status: accepted
- Date: 2026-11-06

## Context

Quartet coordinates four role agents (planner, implementer, tester, reviewer) over a shared task state. The PRD ([../PRD.md](../PRD.md), §3) lists "a framework wrapper" as an anti-goal: if a framework owns delegation, hand-over and recovery, the claim that Quartet "designed delegation, handover, and recovery workflows" is false. The orchestration core must therefore be Quartet's own code. Vendor SDK helpers remain acceptable below that core, for the per-turn tool loop and MCP tool conversion.

Requirements that shape the decision: Postgres is the single store for queue, leases, state and audit (FR-6, FR-7, FR-24); recovery restarts the interrupted role from its last git checkpoint, never mid-turn (FR-23); hand-overs are first-class rows (FR-22); two vendors (Anthropic and OpenAI) must be driven through one schema (FR-15).

## Decision

Write the state machine, worker loop, lease handling, hand-over records and recovery logic in Quartet itself. Use vendor SDKs only for the provider layer: the Anthropic tool runner for the per-role tool loop, a custom loop for OpenAI, and the official `mcp` SDK for the client and server.

## Alternatives considered

- **Temporal.** Durable execution and retries are strong, but it would own exactly the recovery and delegation design the project claims, add a server to operate, and replay semantics differ from the role-checkpoint model (FR-23). Rejected.
- **LangGraph.** Graph checkpointing overlaps with our checkpoints and hand-overs, and its persistence would become a second source of truth next to Postgres. Rejected as the orchestration owner.
- **OpenAI Agents SDK.** Ties delegation to one vendor's runtime, while Quartet deliberately uses two vendors. Rejected.
- **Claude Agent SDK.** Provides an agent loop with its own tool and permission model, which conflicts with the requirement that agents reach the repo only through the sandboxed MCP server and never hold shell, network or git-push tools (FR-13, FR-14). Rejected as the orchestration owner; the plain Anthropic SDK is used below the core.

## Consequences

- Quartet owns recovery logic: reclaim, worktree reset to the last checkpoint, and re-running the interrupted role. This is more code and more tests (the kill-9 test), and bugs here are ours. That cost is the point of the project.
- Resume-claim rationale: because the core is hand-written, the resume statements about delegation, hand-over and recovery are true and traceable to code (PRD §13).
- Conversations stay append-only. Transcripts are stored for audit and tracing, not for resuming.
- Vendor SDK upgrades affect only the provider layer, which is covered by adapter contract tests and a FakeProvider.
- We forgo features frameworks give for free (visual graphs, hosted durability); none is required for v1.
