# ADR-0003: Client-side MCP, server in a per-role-step sandbox

- Status: accepted
- Amended by ADR-0004 (provider layer).
- Date: 2026-11-06

## Context

Agents must reach the repository only through MCP tools (PRD FR-13) and must never receive shell, network, git-push or GitHub tools (FR-14). Any content an agent sees or produces is untrusted, so the component executing tool calls must run under hard isolation: no network, no secrets, a read-only root filesystem, dropped capabilities (PRD §8.4). Each task has its own git worktree on the host, and lease state lives in Postgres, outside the sandbox.

## Decision

The orchestrator is the MCP **client**. The MCP **server**, the minimal `quartet-workspace` package (MCP server, path guard and lease reader only; no database and no LLM SDKs), runs inside a fresh sandbox container per role step and speaks MCP over stdio (`docker run -i --rm ... quartet-workspace --role <role>`). The container mounts the worktree (RW), the mirror (RO) and the lease directory (RO). Role tool allowlists are enforced twice: by the client-side filter and by the server's `--role` flag. Tool definitions are converted client-side for each vendor.

## Alternatives considered

- **Anthropic server-side MCP connector (`mcp_servers`).** The API calls the MCP server itself, and it reaches only remote servers by URL. It cannot reach a local, per-worktree server in a no-network container, it would require exposing a workspace over the internet, and it is Anthropic-only, while Quartet also drives OpenAI. Rejected.
- **A host-side MCP server.** Simple, but the process holding file access would run on the host with the orchestrator's privileges, so a path-guard bug would expose host files and secrets. Sandboxing it removes that blast radius. Rejected.
- **No MCP (bespoke tool functions in the orchestrator).** Would not meet the requirement to use MCP for real, and would keep tool execution in the trusted process. Rejected.

## Consequences

- The tool-executing process has no network and no credentials; a hijacked agent can damage only its own worktree.
- One container start per role step adds latency, and macOS Docker runs in a VM, so UID and mount behaviour differ from Linux CI and must be tested on both.
- The client must adapt MCP tools to each vendor's tool format; Phase 3 converts MCP tool definitions into strict `ToolSpec`s that fit `check_strict_budget` (ADR-0004).
- Lease enforcement at the tool layer is advisory-plus-enforced, and is complemented by post-step diff verification in the orchestrator (FR-7).
- This is planned work (Phase 3); it does not exist yet.
