# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**. Do not open a public issue.

- Once the repository is public: use GitHub's private vulnerability reporting (a private security advisory on this repository).
- Until then: contact the repository owner directly through the channel you already use to reach them. No email address is published here on purpose.

Include what you found, how to reproduce it, and the impact you expect. Expect an acknowledgement, then a fix or a written triage decision. This is a solo-maintained, pre-1.0 project, so there is no response-time guarantee.

## Supported versions

Quartet is pre-1.0. Only the `main` branch is supported. There are no maintained release branches.

## Scope

In scope:

- the orchestrator (state machine, scheduler, policy gate, configuration and secret handling);
- the sandbox (the per-role-step container and its flags);
- the workspace MCP server (`quartet-workspace`);
- CI and supply chain (GitHub Actions workflows, pinned images and dependencies, compose files).

Out of scope:

- vulnerabilities in third-party dependencies, models or services themselves (report those upstream; dependency advisories we are exposed to are tracked through `pip-audit`);
- issues that need an attacker who already controls the owner's machine or credentials;
- model behaviour that does not cross a Quartet control (for example, a model producing a wrong patch that a human reviewer then merges);
- local development settings such as the loopback-bound dev database in `docker-compose.yml`.

## Threat model

The assets, trust boundaries, per-component analysis, known gaps and per-phase changes are documented in [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md). That file separates controls that exist today from controls that are planned.
