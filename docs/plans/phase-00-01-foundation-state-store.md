# Phases 0–1: Foundation & PostgreSQL State Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A gated, reproducible Python repo (Phase 0) holding a PostgreSQL-backed task store with a compare-and-set state machine, a `SKIP LOCKED` lease queue on the DB clock, an append-only event log and strict-schema hand-off artifacts (Phase 1).

**Architecture:** A `uv` workspace (`quartet` + stub `quartet-workspace`) on Python 3.12. Typer CLI with lazily loaded pydantic-settings, SQLAlchemy 2.0 typed ORM on psycopg 3, Alembic migrations, and Postgres 16 + Jaeger v2 in docker-compose on loopback only. Every commit passes ruff, mypy, pytest, gitleaks, semgrep, bandit and pip-audit, locally and in a SHA-pinned, zero-permission GitHub Actions workflow.

**Tech Stack:** Python 3.12, uv, Typer, pydantic v2 + pydantic-settings, SQLAlchemy 2.0, psycopg 3, Alembic, pytest (+ pytester), hypothesis, time-machine, ruff, mypy, gitleaks ≥ 8.19, semgrep, bandit, pip-audit, actionlint, zizmor, Docker Compose.

**Spec:** [`docs/PRD.md`](../PRD.md) v0.3 (§5.3, §5.8, §8.6, §9.2, §9.3, §9.5, §10, §11) and [`docs/ROADMAP.md`](../ROADMAP.md) Phases 0–1.

## Global Constraints

- Python `3.12` (`.python-version`). Every command runs through `uv run …`.
- `mypy --strict` over `src/`. `tests/` use the override `disallow_untyped_defs = false`. `ruff` rule sets `E,F,I,B,UP,S,SIM,RUF`.
- Secrets are typed `pydantic.SecretStr`. They are never logged or printed, and never embedded in a logged URL.
- No `shell=True`, `os.system` or string-built SQL. Subprocesses use argument vectors, DML uses bind parameters, and DDL is composed with `psycopg.sql.SQL`/`Identifier`/`Literal`.
- Untrusted `tasks.title` ≤ **512** and `tasks.body_untrusted` ≤ **65,536** UTF-8 **bytes**.
- Docker images are pinned by `@sha256:` digest. Compose ports bind `127.0.0.1` only. Jaeger is **v2** (`cr.jaegertracing.io/jaegertracing/jaeger`).
- GitHub Actions: every `uses:` is pinned to a 40-hex commit SHA (tag in a trailing comment), top-level `permissions: {}`, and per-job permissions are least privilege.
- Lease and expiry times come from **database** `now()`, never the host clock.
- The Postgres enum types are `task_state` and `role`. `tasks.priority` defaults to 100, and a lower value runs first.
- Coverage ≥ 85% on `src/quartet/`.

## Review Focus

1. **Postgres unreachable or wrong credentials.** `quartet doctor` must name the failing host and port, without the password, and exit 1 with no traceback. *Test:* `test_doctor_reports_unreachable_db` (Task 2).
2. **Leftover test databases after a failing test.** The `fresh_db` fixture's finalizer must drop its DB even when the test raises. *Test:* `test_fresh_db_dropped_when_test_fails` (Task 7, runs an inner failing test through `pytester`).
3. **Host clock skew.** Reap and heartbeat must follow DB `now()`. *Test:* `test_lease_logic_ignores_host_clock` (Task 10, `time-machine` shifts the host to 2100).
4. **Multibyte issue text at the size boundary.** The cap counts UTF-8 bytes, not characters. *Test:* `test_body_cap_counts_utf8_bytes` (Task 12).
5. **A heartbeat from a worker whose lease was already reaped** must return `False` and must **not** re-acquire the lease. *Test:* `test_heartbeat_after_reap_returns_false` (Task 10).

---

## File Structure

```
.python-version · pyproject.toml (workspace root) · uv.lock · alembic.ini
.gitignore · .env.example · .pre-commit-config.yaml · Makefile · docker-compose.yml
packages/quartet-workspace/pyproject.toml · packages/quartet-workspace/src/quartet_workspace/__init__.py  (stub)
docker/postgres/init/01-roles.sh      dev-only: creates roles from env via psql -v
scripts/db_bootstrap.py               idempotent role creation (CI)
.semgrep/quartet.yml                  repo semgrep rules
.github/workflows/ci.yml · .github/dependabot.yml
SECURITY.md · docs/THREAT_MODEL.md · docs/EVAL_PROTOCOL.md · docs/adr/000{1,2,3}-*.md
src/quartet/__init__.py · config.py · doctor.py · cli.py
src/quartet/db/__init__.py · session.py · models.py · queue.py · repository.py · alembic/{env.py,versions/0001_initial.py}
src/quartet/domain/__init__.py · states.py · transitions.py · artifacts.py
tests/conftest.py · tests/unit/ · tests/integration/ (+ _claimers.py) · tests/repo_hygiene/ (+ fixtures/)
```

---

## PHASE 0: Foundation & security baseline

### Task 1: Project skeleton and tooling

**Files:**
- Create: `.python-version`, `pyproject.toml`, `packages/quartet-workspace/…` (stub), `.gitignore`, `.env.example`, `src/quartet/__init__.py`, `tests/repo_hygiene/test_gitignore.py`, `tests/unit/test_version.py`

**Interfaces:**
- Produces: `quartet.__version__: str = "0.1.0"`. Console script `quartet = "quartet.cli:app"` (Task 2).

- [ ] **Step 1: Install tools and initialise**

Run: `brew install uv gitleaks actionlint && git init && uv init --package --name quartet --python 3.12 . && uv init --package --name quartet-workspace packages/quartet-workspace`
Expected: the root `pyproject.toml` lists `packages/quartet-workspace` under `[tool.uv.workspace] members`.

- [ ] **Step 2: Add dependencies**

Run: `uv add typer pydantic pydantic-settings "sqlalchemy>=2.0" "psycopg[binary]>=3.2" alembic` then `uv add --dev pytest pytest-cov hypothesis time-machine ruff mypy bandit pip-audit semgrep pyyaml types-PyYAML pre-commit`
Expected: `uv.lock` is created.

- [ ] **Step 3: Configure tools in `pyproject.toml`**

Set:
- `[tool.ruff] target-version = "py312"`, `line-length = 100`, `extend-exclude = ["tests/repo_hygiene/fixtures"]`;
- `[tool.ruff.lint] select = ["E","F","I","B","UP","S","SIM","RUF"]`, with per-file ignore `"tests/**" = ["S101"]`;
- `[tool.mypy] strict = true`, `plugins = ["pydantic.mypy"]`, `exclude = ["tests/repo_hygiene/fixtures/"]`, plus `[[tool.mypy.overrides]] module = "tests.*"` with `disallow_untyped_defs = false`;
- `[tool.pytest.ini_options] markers = ["integration: needs Postgres", "live: needs real API keys"]`, `addopts = "-ra --strict-markers -p pytester"`, `norecursedirs = ["tests/repo_hygiene/fixtures"]`;
- `[tool.coverage.report] fail_under = 85`.

- [ ] **Step 4: Write the failing hygiene tests**

```python
# tests/repo_hygiene/test_gitignore.py
import subprocess
import pytest

@pytest.mark.parametrize("path", [".env", ".env.local", ".env.production", ".quartet/x", "eval/results/raw/run.json"])
def test_sensitive_paths_are_ignored(path: str) -> None:
    assert subprocess.run(["git", "check-ignore", "-q", path], check=False).returncode == 0

def test_env_example_is_not_ignored() -> None:
    assert subprocess.run(["git", "check-ignore", "-q", ".env.example"], check=False).returncode == 1

# tests/unit/test_version.py
import quartet
def test_version() -> None:
    assert quartet.__version__ == "0.1.0"
```

- [ ] **Step 5: Run to verify they fail**

Run: `uv run pytest tests/repo_hygiene tests/unit/test_version.py -v`
Expected: FAIL (the paths are not ignored, `__version__` is missing).

- [ ] **Step 6: Implement**

Write `.gitignore` (Python defaults + `.env`, `.env.*`, `!.env.example`, `.quartet/`, `eval/results/raw/`, `.venv/`, `.coverage`, `htmlcov/`). Write `.env.example` with **names only**: `ANTHROPIC_API_KEY=`, `OPENAI_API_KEY=`, `QUARTET_DATABASE_URL=`, `QUARTET_ADMIN_DATABASE_URL=`, `POSTGRES_PASSWORD=`, `QUARTET_MIGRATOR_PASSWORD=`, `QUARTET_APP_PASSWORD=`. Set `__version__`.

- [ ] **Step 7: Run to verify they pass**

Run: `uv run pytest tests/repo_hygiene tests/unit/test_version.py -v && uv run ruff check . && uv run mypy src tests`
Expected: PASS, 0 findings, 0 errors.

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "chore: uv workspace skeleton, tooling config, hygiene tests"
```

### Task 2: Settings, `doctor`, and CLI

**Files:**
- Create: `src/quartet/config.py`, `src/quartet/doctor.py`, `src/quartet/cli.py`, `tests/unit/test_config.py`, `tests/unit/test_doctor.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Produces:
  - `class Settings(BaseSettings)` with these fields: `database_url: SecretStr` (`QUARTET_DATABASE_URL`), `admin_database_url: SecretStr | None` (`QUARTET_ADMIN_DATABASE_URL`), `anthropic_api_key: SecretStr | None` (`ANTHROPIC_API_KEY`), `openai_api_key: SecretStr | None` (`OPENAI_API_KEY`), `home: Path` (`QUARTET_HOME`, default `~/.quartet` expanded), `otlp_endpoint: str = "http://127.0.0.1:4318"`. It reads `.env` and uses `extra="ignore"`.
  - `def load_settings() -> Settings`. It is called only by commands that need settings, **never** at import time and never for `--version`.
  - `@dataclass(frozen=True) class Check: name: str; ok: bool; detail: str`.
  - `def run_checks(settings: Settings) -> list[Check]`. Check names, in order: `docker`, `git`, `postgres`, `anthropic_key`, `openai_key`.
  - `def redact_url(url: str) -> str` returns the URL with the password replaced by `***`.
  - Typer `app` with an eager `--version` callback and the `doctor` command (exit 0 when all checks are ok, otherwise exit 1).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_config.py
def test_secrets_are_hidden_in_repr(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:hunter2@localhost:5432/q")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-abc")
    s = load_settings()
    assert "hunter2" not in repr(s) and "sk-ant-test-abc" not in repr(s)
    assert s.anthropic_api_key is not None and s.anthropic_api_key.get_secret_value() == "sk-ant-test-abc"

# tests/unit/test_doctor.py
def test_redact_url():
    assert redact_url("postgresql+psycopg://u:hunter2@db:5432/q") == "postgresql+psycopg://u:***@db:5432/q"

def test_doctor_reports_unreachable_db(monkeypatch):           # Review Focus 1
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:hunter2@127.0.0.1:1/q")
    pg = next(c for c in run_checks(load_settings()) if c.name == "postgres")
    assert pg.ok is False and "127.0.0.1:1" in pg.detail and "hunter2" not in pg.detail

def test_key_checks_never_include_values(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/q")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    checks = run_checks(load_settings())
    assert all("sk-test-xyz" not in c.detail for c in checks)
    assert next(c for c in checks if c.name == "openai_key").ok is True

# tests/unit/test_cli.py  (typer.testing.CliRunner)
def test_version_flag_needs_no_env(monkeypatch, tmp_path):
    monkeypatch.delenv("QUARTET_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)                         # no .env to read either
    r = CliRunner().invoke(app, ["--version"])
    assert r.exit_code == 0 and r.stdout.strip() == "quartet 0.1.0"

def test_doctor_exit_code_and_no_secret_leak(monkeypatch):
    monkeypatch.setenv("QUARTET_DATABASE_URL", "postgresql+psycopg://u:hunter2@127.0.0.1:1/q")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leakcheck")
    r = CliRunner().invoke(app, ["doctor"])
    assert r.exit_code == 1
    assert "hunter2" not in r.output and "sk-ant-leakcheck" not in r.output
    assert "Traceback" not in r.output
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement `config.py`, `doctor.py`, `cli.py`**

- `docker` check: argv `["docker", "version", "--format", "{{.Server.Version}}"]` with a 5 s timeout.
- `git` check: `git --version`, require ≥ 2.40.
- `postgres` check: strip the `+psycopg` driver suffix, then `psycopg.connect(url, connect_timeout=3)` and `SELECT 1`. On failure, `detail = f"cannot connect to {host}:{port}: {type(exc).__name__}"`.
- Key checks: `ok = value is not None and value.get_secret_value() != ""`, with `detail` either `"present"` or `"missing"`.
- Output: one line per check, `✓`/`✗ name: detail`.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit -v && uv run mypy src tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(cli): lazy settings with SecretStr, doctor checks, --version"
```

### Task 3: Local infrastructure (compose, roles, Makefile)

**Files:**
- Create: `docker-compose.yml`, `docker/postgres/init/01-roles.sh`, `scripts/db_bootstrap.py`, `Makefile`, `tests/repo_hygiene/test_compose.py`

**Interfaces:**
- Produces: Postgres 16 on `127.0.0.1:5432` (database `quartet` owned by `quartet_migrator`, role `quartet_app` with `LOGIN`). Jaeger v2 UI on `127.0.0.1:16686`, OTLP HTTP on `127.0.0.1:4318`. Make targets `setup up down check sec test`.
- `01-roles.sh`: runs `psql -v ON_ERROR_STOP=1 -v mig_pw="$QUARTET_MIGRATOR_PASSWORD" -v app_pw="$QUARTET_APP_PASSWORD"` with SQL using `:'mig_pw'` and `:'app_pw'`, then `ALTER DATABASE quartet OWNER TO quartet_migrator`.
- `scripts/db_bootstrap.py --admin-url <url>` creates both roles idempotently (CI path). Statements are composed with `psycopg.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(name), sql.Literal(pw))`, because DDL takes no bind parameters.

- [ ] **Step 1: Write the failing test**

```python
# tests/repo_hygiene/test_compose.py
import re, yaml
DOC = yaml.safe_load(open("docker-compose.yml"))

def test_images_pinned_by_digest():
    for name, svc in DOC["services"].items():
        assert re.search(r"@sha256:[0-9a-f]{64}$", svc["image"]), f"{name} image not digest-pinned"

def test_jaeger_is_v2():
    assert DOC["services"]["jaeger"]["image"].startswith("cr.jaegertracing.io/jaegertracing/jaeger:2.")

def test_ports_bind_loopback_only():
    for name, svc in DOC["services"].items():
        for p in svc.get("ports", []):
            assert str(p).startswith("127.0.0.1:"), f"{name} exposes {p} beyond loopback"

def test_postgres_has_healthcheck_and_no_inline_passwords():
    pg = DOC["services"]["postgres"]
    assert "healthcheck" in pg
    for key in ("POSTGRES_PASSWORD", "QUARTET_MIGRATOR_PASSWORD", "QUARTET_APP_PASSWORD"):
        assert pg["environment"][key].startswith("${"), key
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/repo_hygiene/test_compose.py -v`
Expected: FAIL (the file does not exist).

- [ ] **Step 3: Implement**

Resolve the digests with `docker pull postgres:16 && docker inspect --format '{{index .RepoDigests 0}}' postgres:16`. Do the same for `cr.jaegertracing.io/jaegertracing/jaeger:<latest 2.x tag>`. Mount `./docker/postgres/init` at `/docker-entrypoint-initdb.d:ro`. The `Makefile` defines:
- `setup`: `uv sync && uv run pre-commit install`
- `up` / `down`: `docker compose up -d --wait` / `docker compose down`
- `check`: `ruff check .`, `ruff format --check .`, `mypy src tests`, `pytest`

- [ ] **Step 4: Run to verify it passes**

Run: `cp .env.example .env` (fill in local passwords), then `uv run pytest tests/repo_hygiene/test_compose.py -v && make up && uv run quartet doctor`
Expected: PASS. `doctor` shows `✓ postgres`.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore(infra): digest-pinned compose on loopback, jaeger v2, db roles, Makefile"
```

### Task 4: Security tooling, pre-commit and CI

**Files:**
- Create: `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `.github/dependabot.yml`, `.semgrep/quartet.yml`, `tests/repo_hygiene/test_ci_workflow.py`, `tests/repo_hygiene/test_semgrep_rules.py`, `tests/repo_hygiene/fixtures/bad_shell.py`, `tests/repo_hygiene/fixtures/bad_sql.py`
- Modify: `Makefile` (`sec` target)

**Interfaces:**
- Produces: `make sec`, which runs:
  - `gitleaks git --redact --no-banner`
  - `uv run semgrep scan --metrics=off --error --config p/python --config p/security-audit --config p/secrets --config .semgrep/quartet.yml --exclude tests/repo_hygiene/fixtures`
  - `uv run bandit -q -r src -ll` (medium and higher fail the gate; lows are triaged in the PR)
  - `uv export --frozen --format requirements-txt --no-emit-workspace -o .quartet/requirements.txt && uv run pip-audit --strict --disable-pip -r .quartet/requirements.txt` (the export is fully pinned and hashed, which `--disable-pip` requires)
- `.semgrep/quartet.yml` rules: `quartet-no-shell-true`, `quartet-no-os-system`, `quartet-no-fstring-sql` (an f-string, `%` or `.format` result passed to `sqlalchemy.text(...)`, `.execute(...)` or `psycopg` `cursor.execute(...)`).
- CI jobs, each with `permissions: contents: read`:
  - `lint` and `types`;
  - `test`: a `postgres:16` service, then `scripts/db_bootstrap.py`, then `pytest -m "integration or not integration"`;
  - `secrets`: gitleaks with `fetch-depth: 0`;
  - `sast` and `deps`;
  - `actions`: actionlint + `uvx zizmor .github/workflows`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/repo_hygiene/test_ci_workflow.py
import re, yaml, pathlib
WF = [yaml.safe_load(p.read_text()) for p in pathlib.Path(".github/workflows").glob("*.yml")]

def test_workflows_exist():
    assert WF, "no workflows found"

def test_top_level_permissions_empty():
    for wf in WF:
        assert wf.get("permissions") == {}

def test_every_job_declares_permissions():
    for wf in WF:
        for name, job in wf["jobs"].items():
            assert "permissions" in job, f"job {name} must declare least-privilege permissions"

def test_actions_pinned_to_full_sha():
    for wf in WF:
        for job in wf["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]

def test_no_pull_request_target():
    for wf in WF:
        triggers = wf.get(True, wf.get("on"))  # PyYAML parses `on:` as True
        assert "pull_request_target" not in (triggers or {})

# tests/repo_hygiene/test_semgrep_rules.py
import subprocess
def test_repo_rules_catch_bad_fixtures():
    r = subprocess.run(["uv", "run", "semgrep", "scan", "--metrics=off", "--error", "--json",
                        "--config", ".semgrep/quartet.yml", "tests/repo_hygiene/fixtures"],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 1
    for rule in ("quartet-no-shell-true", "quartet-no-fstring-sql"):
        assert rule in r.stdout
```

`fixtures/bad_shell.py` contains `subprocess.run("ls", shell=True)`. `fixtures/bad_sql.py` contains `conn.execute(text(f"SELECT * FROM t WHERE id = {x}"))`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/repo_hygiene/test_ci_workflow.py tests/repo_hygiene/test_semgrep_rules.py -v`
Expected: FAIL (no workflows, no rules).

- [ ] **Step 3: Implement**

Resolve each action's commit SHA from its latest release. For example: `TAG=$(gh release view -R actions/checkout --json tagName -q .tagName); gh api repos/actions/checkout/commits/$TAG -q .sha`. Using `commits/<tag>` dereferences annotated tags to the commit. Write `uses: actions/checkout@<sha> # <tag>`. Pre-commit hooks: `ruff`, `ruff-format`, `gitleaks`, `detect-private-key`, `check-added-large-files --maxkb=500`. Dependabot: `pip` (uv), `github-actions` and `docker`, weekly.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/repo_hygiene -v && make sec && actionlint && uvx zizmor .github/workflows`
Expected: PASS, 0 findings.

- [ ] **Step 5: Prove the secret gate works (then discard it)**

Run: `make setup && git switch -c tmp/leak && echo 'token = "ghp_1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P7q8R"' > leak.txt && git add leak.txt && git commit -m leak`
Expected: the pre-commit gitleaks hook **blocks** the commit with rule `github-pat`. Don't use AWS's documented example key: gitleaks allowlists it.
Clean up: `git restore --staged leak.txt && rm -f leak.txt && git switch - && git branch -D tmp/leak`

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "ci: SHA-pinned least-privilege CI with secret, SAST, deps and actions gates"
```

### Task 5: Security and design documents

**Files:**
- Create: `SECURITY.md`, `docs/THREAT_MODEL.md`, `docs/EVAL_PROTOCOL.md`, `docs/adr/0001-hand-rolled-orchestration.md`, `docs/adr/0002-postgres-queue-and-state.md`, `docs/adr/0003-client-side-mcp-in-sandbox.md`, `tests/repo_hygiene/test_docs_links.py`

**Interfaces:**
- `THREAT_MODEL.md` sections: Assets · Trust boundaries (the PRD §8.2 diagram) · STRIDE table per component (orchestrator, sandbox, MCP server, Postgres, GitHub App, target-repo CI, LLM APIs) · OWASP LLM Top-10 (2025) mapping (LLM01, 02, 03, 05, 06, 10 at minimum) · Residual risks · **Threat delta log** (one entry per phase).
- `EVAL_PROTOCOL.md`: PRD §6 and §7 copied, headed **"DRAFT, not frozen. Frozen by the signed tag `eval-protocol-v1` and its GitHub Release in Phase 9."**
- Each ADR: Context · Decision · Alternatives considered · Consequences.

- [ ] **Step 1: Write the failing test**

```python
# tests/repo_hygiene/test_docs_links.py
import re, pathlib
def test_relative_markdown_links_resolve():
    for md in [*pathlib.Path("docs").rglob("*.md"), pathlib.Path("SECURITY.md")]:
        for target in re.findall(r"\]\(([^)#\s]+)", md.read_text()):
            if not target.startswith(("http://", "https://", "mailto:")):
                assert (md.parent / target).exists(), f"{md}: broken link {target}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/repo_hygiene/test_docs_links.py -v`
Expected: FAIL. `SECURITY.md` does not exist yet, and the companion-docs row in `docs/PRD.md` links to `THREAT_MODEL.md` and `EVAL_PROTOCOL.md`, which do not exist either.

- [ ] **Step 3: Write the documents** following the section lists above.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/repo_hygiene -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "docs: threat model v0, eval protocol draft, ADRs 0001-0003, SECURITY.md"
```

### Task 6: Phase 0 gate

- [ ] **Step 1:** Run `make check && make sec`. Expected: all green.
- [ ] **Step 2:** Create the **public** GitHub repo, push, open PR `phase/00-foundation`, and confirm every CI job is green.
- [ ] **Step 3:** Run `/code-review` and `/security-review` on the branch. Fix confirmed findings in follow-up commits.
- [ ] **Step 4:** Protect `main` (ROADMAP "How every phase runs" step 3): PR required, every CI check required, conversation resolution required, force-push and deletion blocked, **no** required-approval rule (a solo author cannot self-approve). Add the Phase 0 entry to the threat-delta log.
- [ ] **Step 5:** The owner self-reviews against the checklist and merges.

---

## PHASE 1: Domain model & PostgreSQL state store

### Task 7: DB session, Alembic, and Postgres test fixtures

**Files:**
- Create: `src/quartet/db/__init__.py`, `src/quartet/db/session.py`, `alembic.ini`, `src/quartet/db/alembic/env.py`, `tests/conftest.py`, `tests/integration/test_fixtures.py`, `tests/integration/test_session.py`

**Interfaces:**
- Produces:
  - `make_engine(url: SecretStr) -> sqlalchemy.Engine` (unwraps the secret internally, `pool_pre_ping=True`, `hide_parameters=True`).
  - `session_scope(engine: Engine) -> Iterator[Session]` (commit on success, roll back on exception).
- Conftest helpers (module-level, importable):
  - `create_migrated_database(admin_url: str) -> MigratedDb`. An admin connection with **autocommit** runs `CREATE DATABASE {name} OWNER quartet_migrator`, and then `alembic upgrade head` runs as the migrator. `MigratedDb` is a frozen dataclass with `name: str`, `migrator_url: str` and `app_url: str`.
  - `drop_database(admin_url: str, name: str) -> None` runs `DROP DATABASE … WITH (FORCE)` with autocommit.
  - `list_test_databases(admin_url: str) -> list[str]` returns names matching `quartet_test_%`.
  - Names are `quartet_test_<uuid4().hex[:12]>`, composed with `psycopg.sql.Identifier`.
- Fixtures:
  - `admin_url` (session) reads `QUARTET_ADMIN_DATABASE_URL`, or **skips** integration tests when it is unset.
  - `migrated_db` (session) creates the DB and drops it in `finally`.
  - `fresh_db` (function) does the same per test, for schema and migration tests.
  - `clean_db` (function, autouse for `integration`) runs `TRUNCATE <all tables> RESTART IDENTITY CASCADE` as the migrator before each test.
  - `app_url` (session) = `migrated_db.app_url`.
  - `app_engine` (session) = `make_engine(SecretStr(app_url))`.
  - `app_session` (function) is a `Session` on `app_engine`, rolled back and closed at teardown.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_fixtures.py
import pytest
pytestmark = pytest.mark.integration
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

def test_fresh_db_dropped_when_test_fails(pytester, admin_url):          # Review Focus 2
    before = set(list_test_databases(admin_url))
    pytester.syspathinsert(REPO_ROOT)
    pytester.makeconftest("from tests.conftest import *  # noqa: F403")
    pytester.makepyfile("import pytest\n@pytest.mark.integration\ndef test_boom(fresh_db):\n    raise RuntimeError('boom')\n")
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    assert set(list_test_databases(admin_url)) == before

# tests/integration/test_session.py
def test_engine_errors_do_not_log_password(caplog):
    eng = make_engine(SecretStr("postgresql+psycopg://u:hunter2@127.0.0.1:1/q"))
    with caplog.at_level("DEBUG"), pytest.raises(sa.exc.OperationalError) as ei:
        eng.connect()
    assert "hunter2" not in caplog.text and "hunter2" not in str(ei.value)

def test_session_scope_rolls_back_on_error(app_engine):
    with pytest.raises(RuntimeError), session_scope(app_engine) as s:
        s.execute(sa.text("SELECT 1")); raise RuntimeError
```

- [ ] **Step 2: Run to verify they fail**

Run: `make up && uv run pytest tests/integration/test_fixtures.py tests/integration/test_session.py -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement** `session.py`, the Alembic `env.py` (URL from `-x url=…`, else `QUARTET_DATABASE_URL`; `target_metadata = Base.metadata`), and the conftest helpers and fixtures above. Use `alembic.command.upgrade(Config("alembic.ini"), "head")` with the URL passed through `cmd_opts`/`attributes`, never through a logged string.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/integration/test_fixtures.py tests/integration/test_session.py -v`
Expected: PASS. Afterwards, `psql "$QUARTET_ADMIN_DATABASE_URL" -c "\l quartet_test_*"` lists nothing.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(db): engine/session helpers, alembic env, isolated test databases"
```

### Task 8: Enums, schema (all PRD §9.3 tables), grants, data fixtures

**Files:**
- Create: `src/quartet/domain/__init__.py`, `src/quartet/domain/states.py`, `src/quartet/db/models.py`, `src/quartet/db/alembic/versions/0001_initial.py`, `tests/integration/test_schema.py`
- Modify: `tests/conftest.py` (data fixtures)

**Interfaces:**
- Produces:
  - `class TaskState(StrEnum)`: `QUEUED, SCOPING, REJECTED, PLANNING, PLAN_REVIEW, WAITING_LEASES, IMPLEMENTING, TESTING, REVIEWING, INTEGRATING, PR_DRAFT, AWAITING_HUMAN, MERGED, CLOSED, NEEDS_HUMAN`.
  - `class Role(StrEnum)`: `PLANNER, IMPLEMENTER, TESTER, REVIEWER, ORCHESTRATOR, HUMAN`.
  - `RUNNABLE: frozenset[TaskState] = {QUEUED, SCOPING, PLANNING, WAITING_LEASES, IMPLEMENTING, TESTING, REVIEWING, INTEGRATING, PR_DRAFT}`.
- ORM classes `Repo`, `Task`, `RoleStep`, `Artifact`, `Handover`, `LlmCall`, `ToolCall`, `FileLease`, `Worktree`, `Checkpoint`, `Intervention`, `PullRequest`, `Event`, with columns exactly as in PRD §9.3. Every table has `created_at timestamptz NOT NULL DEFAULT now()`. `tasks`, `role_steps`, `worktrees`, `checkpoints` and `pull_requests` also have `updated_at`. `Task.state` and `Task.resume_state` use the PG enum **`task_state`**, and role columns use the PG enum **`role`**. `Task.priority` defaults to 100, `version` to 0 and `attempt` to 0. `RoleStep` has `UniqueConstraint("task_id", "role", "round", "attempt")`.
- The migration (written by hand, not autogenerated) ends with:
  - `CREATE INDEX ix_tasks_claimable ON tasks (priority, created_at) WHERE lease_owner IS NULL AND state IN ('QUEUED','SCOPING','PLANNING','WAITING_LEASES','IMPLEMENTING','TESTING','REVIEWING','INTEGRATING','PR_DRAFT')`
  - `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO quartet_app`
  - `GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO quartet_app`
  - `ALTER DEFAULT PRIVILEGES FOR ROLE quartet_migrator IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quartet_app` (and the same for sequences)
  - `REVOKE UPDATE, DELETE, TRUNCATE ON events FROM quartet_app`
- Data fixtures (conftest; local imports of `quartet.db.models`; every one **commits** through the app role):
  - `repo_id() -> UUID`
  - `seeded_task(repo_id) -> UUID` (state `QUEUED`, version 0)
  - `make_tasks(n=1, priorities: list[int] | None = None, state=TaskState.QUEUED) -> list[UUID]` (a factory fixture; created in list order, so `created_at` is increasing)

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_schema.py
import pytest, sqlalchemy as sa
pytestmark = pytest.mark.integration
EXPECTED = {"repos","tasks","role_steps","artifacts","handovers","llm_calls","tool_calls",
            "file_leases","worktrees","checkpoints","interventions","pull_requests","events"}

def test_all_tables_exist_with_created_at(fresh_db):
    insp = sa.inspect(sa.create_engine(fresh_db.migrator_url))
    assert EXPECTED <= set(insp.get_table_names())
    for t in EXPECTED:
        assert "created_at" in {c["name"] for c in insp.get_columns(t)}, t

def test_claimable_index_is_partial_on_runnable_states(fresh_db):
    with sa.create_engine(fresh_db.migrator_url).connect() as c:
        ddl = c.execute(sa.text("SELECT indexdef FROM pg_indexes WHERE indexname='ix_tasks_claimable'")).scalar_one()
    assert "lease_owner IS NULL" in ddl and "'PR_DRAFT'" in ddl and "'MERGED'" not in ddl

def test_events_are_append_only_for_app_role(app_engine, seeded_task):
    with app_engine.begin() as c:
        c.execute(sa.text("INSERT INTO events (task_id, type, payload) VALUES (:t, 'x', '{}')"), {"t": seeded_task})
    for stmt in ("UPDATE events SET type='y'", "DELETE FROM events", "TRUNCATE events"):
        with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
            with app_engine.begin() as c:
                c.execute(sa.text(stmt))

def test_migration_round_trip(fresh_db):
    cfg = alembic_config(fresh_db.migrator_url)          # conftest helper from Task 7
    command.downgrade(cfg, "base")
    assert not (EXPECTED & set(sa.inspect(sa.create_engine(fresh_db.migrator_url)).get_table_names()))
    command.upgrade(cfg, "head")
    assert EXPECTED <= set(sa.inspect(sa.create_engine(fresh_db.migrator_url)).get_table_names())
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/integration/test_schema.py -v`
Expected: FAIL (no tables, no `states.py`).

- [ ] **Step 3: Implement** `states.py`, `models.py`, `0001_initial.py` (with a downgrade that drops everything, including the enum types) and the data fixtures.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/integration/test_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(db): enums, initial schema, partial claim index, append-only events grants"
```

### Task 9: State machine with compare-and-set transitions

**Files:**
- Create: `src/quartet/domain/transitions.py`, `tests/unit/test_transitions.py`, `tests/integration/test_transition_cas.py`

**Interfaces:**
- Consumes: `TaskState` (Task 8).
- Produces:
  - `TERMINAL: frozenset[TaskState] = {REJECTED, MERGED, CLOSED}`.
  - `ALLOWED: Mapping[TaskState, frozenset[TaskState]]` (a `types.MappingProxyType`), built as **base edges ∪ {NEEDS_HUMAN, CLOSED} for every non-terminal state, with self-loops removed**. Terminal states have no exits.

    | From | Base edges |
    |---|---|
    | QUEUED | SCOPING |
    | SCOPING | PLANNING, REJECTED |
    | PLANNING | PLAN_REVIEW, WAITING_LEASES |
    | PLAN_REVIEW | WAITING_LEASES |
    | WAITING_LEASES | IMPLEMENTING |
    | IMPLEMENTING | TESTING, WAITING_LEASES |
    | TESTING | REVIEWING, IMPLEMENTING |
    | REVIEWING | IMPLEMENTING, INTEGRATING |
    | INTEGRATING | PR_DRAFT, AWAITING_HUMAN |
    | PR_DRAFT | AWAITING_HUMAN |
    | AWAITING_HUMAN | MERGED |
    | NEEDS_HUMAN | every non-terminal state except itself (the DB check below narrows this to `resume_state`) |

  - `class IllegalTransition(Exception)`, `class StaleTransition(Exception)`.
  - `def check_transition(from_: TaskState, to: TaskState) -> None` (pure; raises `IllegalTransition`).
  - `def transition(session: Session, task_id: UUID, from_: TaskState, to: TaskState, expected_version: int, event_type: str, payload: Mapping[str, Any] | None = None) -> int`, in this order:
    1. `check_transition` (no SQL yet);
    2. if `from_ is NEEDS_HUMAN and to is not CLOSED`: `SELECT resume_state … FOR UPDATE`, and raise `IllegalTransition` unless it equals `to`;
    3. `UPDATE tasks SET state = CAST(:to AS task_state), version = version + 1, updated_at = now(), resume_state = CASE WHEN :to = 'NEEDS_HUMAN' THEN CAST(:from AS task_state) WHEN :from = 'NEEDS_HUMAN' THEN NULL ELSE resume_state END WHERE id = :id AND state = CAST(:from AS task_state) AND version = :v RETURNING version`, raising `StaleTransition` if no row comes back;
    4. insert an `events` row (`type = event_type`, payload ∪ `{"from": from_, "to": to}`) in the **same** transaction;
    5. return the new version.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_transitions.py
NON_TERMINAL = set(TaskState) - TERMINAL

def test_terminal_states_have_no_exits():
    assert all(ALLOWED[s] == frozenset() for s in TERMINAL)

def test_no_self_loops():
    assert all(s not in ALLOWED[s] for s in TaskState)

def test_every_non_terminal_can_close_and_escalate():
    for s in NON_TERMINAL:
        assert TaskState.CLOSED in ALLOWED[s]
        if s is not TaskState.NEEDS_HUMAN:
            assert TaskState.NEEDS_HUMAN in ALLOWED[s]

def test_all_states_reachable_from_queued():
    seen, todo = {TaskState.QUEUED}, [TaskState.QUEUED]
    while todo:
        for n in ALLOWED[todo.pop()] - seen:
            seen.add(n); todo.append(n)
    assert seen == set(TaskState)

def test_every_non_terminal_can_reach_a_terminal():
    for s in NON_TERMINAL:
        seen, todo = {s}, [s]
        while todo:
            for n in ALLOWED[todo.pop()] - seen:
                seen.add(n); todo.append(n)
        assert seen & TERMINAL, s

@pytest.mark.parametrize("a", list(TaskState))
@pytest.mark.parametrize("b", list(TaskState))
def test_check_transition_matches_table(a, b):
    if b in ALLOWED[a]:
        check_transition(a, b)
    else:
        with pytest.raises(IllegalTransition):
            check_transition(a, b)

def test_happy_path_edges_present():
    path = ["QUEUED","SCOPING","PLANNING","WAITING_LEASES","IMPLEMENTING","TESTING",
            "REVIEWING","INTEGRATING","PR_DRAFT","AWAITING_HUMAN","MERGED"]
    for a, b in zip(path, path[1:]):
        assert TaskState(b) in ALLOWED[TaskState(a)]

# tests/integration/test_transition_cas.py  (pytest.mark.integration)
def test_cas_increments_version_and_logs_event(app_session, seeded_task):
    assert transition(app_session, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 0, "scoping_started") == 1
    ev = app_session.execute(sa.text("SELECT type, payload FROM events WHERE task_id=:t"), {"t": seeded_task}).one()
    assert ev.type == "scoping_started" and ev.payload["to"] == "SCOPING"

def test_stale_version_raises_and_writes_nothing(app_session, seeded_task):
    n_before = app_session.execute(sa.text("SELECT count(*) FROM events")).scalar_one()
    with pytest.raises(StaleTransition):
        transition(app_session, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 7, "x")
    app_session.rollback()
    row = app_session.execute(sa.text("SELECT state, version FROM tasks WHERE id=:t"), {"t": seeded_task}).one()
    assert (row.state, row.version) == ("QUEUED", 0)
    assert app_session.execute(sa.text("SELECT count(*) FROM events")).scalar_one() == n_before

def test_concurrent_writers_exactly_one_wins(app_engine, seeded_task):
    # two threads, each with its own Session, attempt QUEUED->SCOPING at version 0 behind a Barrier(2)
    # assert sorted(outcomes) == ["ok", "stale"]
    ...

def test_needs_human_resumes_only_to_recorded_state(app_session, seeded_task):
    v = transition(app_session, seeded_task, TaskState.QUEUED, TaskState.SCOPING, 0, "s")
    v = transition(app_session, seeded_task, TaskState.SCOPING, TaskState.NEEDS_HUMAN, v, "breach")
    with pytest.raises(IllegalTransition):
        transition(app_session, seeded_task, TaskState.NEEDS_HUMAN, TaskState.PLANNING, v, "resume")
    v = transition(app_session, seeded_task, TaskState.NEEDS_HUMAN, TaskState.SCOPING, v, "resume")
    assert app_session.execute(sa.text("SELECT resume_state FROM tasks WHERE id=:t"), {"t": seeded_task}).scalar() is None

@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
def test_random_legal_walk_versions_and_events(app_engine, repo_id, data):
    with session_scope(app_engine) as s:
        tid = new_task(s, repo_id)                       # local helper: inserts a QUEUED task
        state, version = TaskState.QUEUED, 0
        for step in range(1, 16):
            options = sorted(ALLOWED[state] - {TaskState.NEEDS_HUMAN})  # resume needs bookkeeping; covered above
            if not options:
                break
            nxt = data.draw(st.sampled_from(options))
            version = transition(s, tid, state, nxt, version, "walk")
            assert version == step
            state = nxt
        n = s.execute(sa.text("SELECT count(*) FROM events WHERE task_id=:t AND type='walk'"), {"t": tid}).scalar_one()
        assert n == version
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_transitions.py tests/integration/test_transition_cas.py -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement** `transitions.py` as specified.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit/test_transitions.py tests/integration/test_transition_cas.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(domain): CAS state machine with resume_state and event log"
```

### Task 10: Lease queue (claim, heartbeat, release, reap)

**Files:**
- Create: `src/quartet/db/queue.py`, `tests/integration/_claimers.py`, `tests/integration/test_queue.py`

**Interfaces:**
- Consumes: `RUNNABLE`, `TaskState` (Task 8). The `make_tasks` and `app_url` fixtures.
- Produces:
  - `@dataclass(frozen=True) class ClaimedTask: task_id: UUID; state: TaskState; version: int; attempt: int; lease_expires_at: datetime`.
  - `claim(session, worker_id: str, lease_seconds: int) -> ClaimedTask | None` (commits).
  - `heartbeat(session, task_id: UUID, worker_id: str, lease_seconds: int) -> bool`, which runs `UPDATE tasks SET lease_expires_at = now() + make_interval(secs => :s) WHERE id = :id AND lease_owner = :w AND lease_expires_at > now()` and returns whether a row was updated.
  - `release(session, task_id: UUID, worker_id: str) -> None` (clears the lease only if owned by `worker_id`).
  - `reap_expired(session) -> list[UUID]`, which runs `UPDATE tasks SET lease_owner = NULL, lease_expires_at = NULL, attempt = attempt + 1 WHERE lease_owner IS NOT NULL AND lease_expires_at <= now() RETURNING id`.
- Claim query (an exact algorithm, because the lock and type semantics matter):

```sql
UPDATE tasks SET lease_owner = :worker, lease_expires_at = now() + make_interval(secs => :lease)
WHERE id = (
  SELECT id FROM tasks
  WHERE lease_owner IS NULL AND state = ANY(CAST(:runnable AS task_state[]))
  ORDER BY priority ASC, created_at ASC
  LIMIT 1
  FOR UPDATE SKIP LOCKED)
RETURNING id, state, version, attempt, lease_expires_at;
```

- `tests/integration/_claimers.py`: a module-level `def claim_all(app_url: str, worker_id: str) -> list[str]`, which loops `claim()` until `None`, plus `def run_claimers(app_url: str, processes: int) -> list[str]`, which uses `multiprocessing.get_context("spawn").Pool` (macOS-safe).

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_queue.py  (pytest.mark.integration)
def test_claim_orders_by_priority_then_age(app_session, make_tasks):
    low, high = make_tasks(n=2, priorities=[200, 10])
    assert claim(app_session, "w1", 30).task_id == high

def test_each_task_claimed_exactly_once_across_processes(app_url, make_tasks):
    ids = {str(i) for i in make_tasks(n=200)}
    claimed = run_claimers(app_url, processes=8)
    assert len(claimed) == len(set(claimed)) == 200 and set(claimed) == ids

def test_heartbeat_extends_live_lease(app_session, make_tasks):
    make_tasks(n=1); c = claim(app_session, "w1", 5)
    assert heartbeat(app_session, c.task_id, "w1", 60) is True

def test_heartbeat_after_reap_returns_false(app_session, make_tasks):   # Review Focus 5
    make_tasks(n=1); c = claim(app_session, "w1", 1)
    app_session.execute(sa.text("UPDATE tasks SET lease_expires_at = now() - interval '1 second'")); app_session.commit()
    assert reap_expired(app_session) == [c.task_id]
    assert heartbeat(app_session, c.task_id, "w1", 60) is False
    assert app_session.execute(sa.text("SELECT lease_owner FROM tasks")).scalar() is None

def test_reap_increments_attempt(app_session, make_tasks):
    make_tasks(n=1); c = claim(app_session, "w1", 1)
    app_session.execute(sa.text("UPDATE tasks SET lease_expires_at = now() - interval '1 second'")); app_session.commit()
    reap_expired(app_session)
    assert claim(app_session, "w2", 30).attempt == c.attempt + 1

def test_lease_logic_ignores_host_clock(app_session, make_tasks):       # Review Focus 3
    make_tasks(n=1); c = claim(app_session, "w1", 3600)
    with time_machine.travel(datetime(2100, 1, 1, tzinfo=UTC), tick=False):
        assert reap_expired(app_session) == []                  # DB clock says the lease is valid
        assert heartbeat(app_session, c.task_id, "w1", 60) is True

def test_terminal_and_needs_human_tasks_are_never_claimed(app_session, make_tasks):
    make_tasks(n=1, state=TaskState.MERGED); make_tasks(n=1, state=TaskState.NEEDS_HUMAN)
    assert claim(app_session, "w1", 30) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/integration/test_queue.py -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement** `queue.py` with the SQL above through `sa.text()` and bind parameters (`:runnable` is a `list[str]` of state names), plus `_claimers.py`.

- [ ] **Step 4: Run the tests, then prove the index is used**

Run: `uv run pytest tests/integration/test_queue.py -v`
Expected: PASS.
Then, in a scratch DB from `create_migrated_database`: insert 20,000 tasks (90% `MERGED`, 10% `QUEUED`), run `ANALYZE tasks`, then `EXPLAIN (ANALYZE)` the inner `SELECT`.
Expected: the output shows `Index Scan using ix_tasks_claimable`. Paste it into the phase PR.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(db): SKIP LOCKED lease queue with heartbeat and reaper on DB clock"
```

### Task 11: Strict-schema hand-off artifacts

**Files:**
- Create: `src/quartet/domain/artifacts.py`, `tests/unit/test_artifacts.py`
- Modify: `tests/conftest.py` (`sample_plan` fixture)

**Interfaces:**
- Produces Pydantic v2 models with `model_config = ConfigDict(extra="forbid", frozen=True)`. **No field has a default.** Optional fields are typed `T | None` and stay required. `schema_version: Literal[1]` is emitted as `{"const": 1, "type": "integer"}`; both vendors' acceptance of it is proven live in Phase 2's `quartet llm smoke`. The models:
  - `PredictedFile(path_glob: str, mode: Literal["read","write"])`
  - `PlanStep(description: str, files: list[str])`
  - `Plan(schema_version: Literal[1], summary: str, steps: list[PlanStep], predicted_files: list[PredictedFile], acceptance_criteria: list[str], test_strategy: str, risk_level: Literal["low","medium","high"], out_of_scope_reason: str | None)`
  - `PatchSummary(schema_version: Literal[1], summary: str, files_changed: list[str], rationale: str, known_limitations: list[str])`
  - `TestReport(schema_version: Literal[1], passed: int, failed: int, errors: int, skipped: int, failing_tests: list[str], new_tests: list[str], suite_command: list[str], output_excerpt: str, post_rebase: bool)`
  - `Finding(file: str, line: int | None, severity: Literal["critical","high","medium","low","nit"], rationale: str, suggestion: str | None)`
  - `ReviewReport(schema_version: Literal[1], verdict: Literal["approve","request_changes"], findings: list[Finding], injection_suspected: bool)`
  - `HandoverNote(schema_version: Literal[1], goal: str, attempted: list[str], current_state: str, evidence: list[str], suggested_next_step: str)`
  - `ConflictReport(schema_version: Literal[1], base_sha: str, onto_sha: str, conflicting_files: list[str], resolution_attempted: bool)`
  - `ContextDigest(schema_version: Literal[1], goal: str, constraints: list[str], open_questions: list[str], artifact_ids: list[str])`
  - `ARTIFACT_KINDS: Mapping[str, type[BaseModel]]` = `{"plan": Plan, "patch_summary": PatchSummary, "test_report": TestReport, "review_report": ReviewReport, "handover_note": HandoverNote, "conflict_report": ConflictReport}`
  - `def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]`. It starts from `model.model_json_schema()` and walks every object node, including those under `$defs`, to set `additionalProperties: false` and `required = list(properties)`. It raises `ValueError` if any of `minLength, maxLength, minimum, maximum, exclusiveMinimum, exclusiveMaximum, multipleOf, pattern, minItems, maxItems` appears, or if a `$ref` cycle exists.
  - Text caps live in validators, never in the schema: each `str` field ≤ 16 KiB of UTF-8 (rejected above that), except `output_excerpt`, which is truncated to ≤ 8 KiB with a `…[truncated]` suffix.
- Fixture `sample_plan() -> Plan` (a minimal valid plan).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_artifacts.py
FORBIDDEN = {"minLength","maxLength","minimum","maximum","exclusiveMinimum","exclusiveMaximum",
             "multipleOf","pattern","minItems","maxItems"}

def _walk(node):
    yield node
    if isinstance(node, dict):
        for v in node.values(): yield from _walk(v)
    elif isinstance(node, list):
        for v in node: yield from _walk(v)

@pytest.mark.parametrize("model", [*ARTIFACT_KINDS.values(), ContextDigest])
def test_schema_is_strict_for_both_vendors(model):
    schema = strict_json_schema(model)
    objects = [n for n in _walk(schema) if isinstance(n, dict) and n.get("type") == "object"]
    assert objects
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])
    keys = {k for n in _walk(schema) if isinstance(n, dict) for k in n}
    assert not (FORBIDDEN & keys)

def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        ReviewReport.model_validate({"schema_version": 1, "verdict": "approve", "findings": [],
                                     "injection_suspected": False, "merge_now": True})

def test_optional_fields_still_required():
    with pytest.raises(ValidationError):   # out_of_scope_reason omitted
        Plan.model_validate({"schema_version": 1, "summary": "s", "steps": [], "predicted_files": [],
                             "acceptance_criteria": [], "test_strategy": "t", "risk_level": "low"})

def test_wrong_schema_version_rejected(sample_plan):
    with pytest.raises(ValidationError):
        Plan.model_validate({**sample_plan.model_dump(), "schema_version": 2})

def test_output_excerpt_truncated_not_rejected():
    r = TestReport.model_validate({"schema_version": 1, "passed": 1, "failed": 0, "errors": 0, "skipped": 0,
                                   "failing_tests": [], "new_tests": [], "suite_command": ["pytest"],
                                   "output_excerpt": "x" * 20_000, "post_rebase": False})
    assert len(r.output_excerpt.encode()) <= 8 * 1024 and r.output_excerpt.endswith("…[truncated]")

def test_strict_schema_rejects_constrained_models():
    class Bad(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: Annotated[str, Field(min_length=1)]
    with pytest.raises(ValueError):
        strict_json_schema(Bad)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/unit/test_artifacts.py -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement** `artifacts.py` and the `sample_plan` fixture.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/unit/test_artifacts.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(domain): versioned strict-schema hand-off artifacts"
```

### Task 12: Repository helpers and untrusted-input caps

**Files:**
- Create: `src/quartet/db/repository.py`, `tests/integration/test_repository.py`

**Interfaces:**
- Consumes: the Task 8 models and fixtures, Task 11 `ARTIFACT_KINDS`, `ContextDigest`, `sample_plan`.
- Produces:
  - `class UntrustedInputTooLarge(ValueError)`.
  - `create_repo(session, owner: str, name: str, default_branch: str, mirror_path: Path) -> UUID`.
  - `create_task(session, repo_id: UUID, source: Literal["github_issue","suite","manual"], external_ref: str, title: str, body_untrusted: str, priority: int = 100, budget_usd: Decimal = Decimal("5.00")) -> UUID`. It strips NUL characters, then caps `title` at 512 B and `body_untrusted` at 65,536 B of UTF-8, raising `UntrustedInputTooLarge`. It writes a `task_created` event in the same transaction.
  - `append_event(session, task_id: UUID, type_: str, payload: Mapping[str, Any]) -> int`.
  - `save_artifact(session, task_id: UUID, artifact: BaseModel) -> UUID`. The kind is resolved by exact type from `ARTIFACT_KINDS`, and an unknown type raises `TypeError`.
  - `record_handover(session, task_id: UUID, from_role: Role, to_role: Role, artifact_id: UUID, digest: ContextDigest, attempt: int) -> UUID`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_repository.py  (pytest.mark.integration)
def test_body_cap_counts_utf8_bytes(app_session, repo_id):            # Review Focus 4
    ok = "é" * (65_536 // 2)            # 2 bytes each -> exactly 65,536 bytes, 32,768 chars
    create_task(app_session, repo_id, "manual", "r#1", "t", ok)
    with pytest.raises(UntrustedInputTooLarge):
        create_task(app_session, repo_id, "manual", "r#2", "t", ok + "é")

def test_title_cap(app_session, repo_id):
    with pytest.raises(UntrustedInputTooLarge):
        create_task(app_session, repo_id, "manual", "r#5", "x" * 513, "b")

def test_create_task_writes_event(app_session, repo_id):
    tid = create_task(app_session, repo_id, "manual", "r#3", "t", "b")
    assert app_session.execute(sa.text("SELECT type FROM events WHERE task_id=:t"), {"t": tid}).scalar() == "task_created"

def test_handover_round_trip(app_session, seeded_task, sample_plan):
    aid = save_artifact(app_session, seeded_task, sample_plan)
    digest = ContextDigest(schema_version=1, goal="g", constraints=[], open_questions=[], artifact_ids=[str(aid)])
    hid = record_handover(app_session, seeded_task, Role.PLANNER, Role.IMPLEMENTER, aid, digest, 0)
    row = app_session.get(Handover, hid)
    assert row.to_role is Role.IMPLEMENTER and row.context_digest["goal"] == "g"

def test_unknown_artifact_type_rejected(app_session, seeded_task):
    class NotAnArtifact(BaseModel): x: int
    with pytest.raises(TypeError):
        save_artifact(app_session, seeded_task, NotAnArtifact(x=1))

def test_untrusted_body_with_sql_metacharacters_is_stored_verbatim(app_session, repo_id):
    evil = "'); DROP TABLE tasks; -- {payload} %s $1"
    tid = create_task(app_session, repo_id, "manual", "r#4", "t", evil)
    assert app_session.get(Task, tid).body_untrusted == evil

def test_nul_bytes_are_stripped(app_session, repo_id):     # Postgres text cannot store NUL
    tid = create_task(app_session, repo_id, "manual", "r#6", "t\x00x", "a\x00b")
    t = app_session.get(Task, tid)
    assert (t.title, t.body_untrusted) == ("tx", "ab")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/integration/test_repository.py -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement** `repository.py` with ORM inserts only.

- [ ] **Step 4: Run to verify they pass, then run the whole suite**

Run: `uv run pytest -v --cov=quartet --cov-report=term-missing`
Expected: all PASS, coverage ≥ 85%.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(db): repository helpers with UTF-8 byte caps on untrusted input"
```

### Task 13: Phase 1 gate

- [ ] **Step 1:** Run `make check && make sec`. Expected: all green, including the repo semgrep rules.
- [ ] **Step 2:** Open PR `phase/01-state-store` with the Task 10 `EXPLAIN (ANALYZE)` output and confirm CI is green.
- [ ] **Step 3:** Run `/code-review` and `/security-review`, with the reviewers pointed at transaction boundaries, lock ordering, the grant and default-privilege set, `resume_state` handling and every place untrusted text is stored.
- [ ] **Step 4:** Add a Phase 1 threat-delta entry (new asset: the task DB; new controls: role split, append-only events, byte caps, DB-clock leases). The owner merges.
