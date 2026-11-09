# Quartet

Multi-agent coding orchestrator.

## Local setup

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker, git 2.40 or newer, and gitleaks 8.30.1 (the CI pin; `make secrets` refuses any other version).

```sh
make setup              # uv sync and the pre-commit hooks
cp .env.example .env    # then fill it in (see below)
chmod 600 .env          # config refuses a group- or world-writable env file
make up                 # Postgres 16 and Jaeger, bound to 127.0.0.1 only
make migrate            # alembic upgrade head, as quartet_migrator
make doctor             # environment health checks
make check              # ruff, mypy, pytest with coverage (integration tests need `make up`)
make sec                # gitleaks, semgrep, bandit, pip-audit
```

What goes in `.env`:

- `POSTGRES_PASSWORD`, `QUARTET_MIGRATOR_PASSWORD`, `QUARTET_APP_PASSWORD`: choose long random values.
- `QUARTET_DATABASE_URL`: the application role, e.g. `postgresql+psycopg://quartet_app:<app password>@127.0.0.1:5432/quartet`.
- `QUARTET_MIGRATOR_DATABASE_URL`: the schema owner, used only by Alembic (`make migrate`), e.g. `postgresql+psycopg://quartet_migrator:<migrator password>@127.0.0.1:5432/quartet`. Migrations never fall back to the application URL.
- `QUARTET_ADMIN_DATABASE_URL`: the `postgres` superuser on database `postgres`; the integration tests use it to create and drop throwaway `quartet_test_*` databases.
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`: needed for `quartet llm verify-models`, `quartet llm smoke` and `pytest -m live` (and checked by `make doctor`); `make check` never uses them.

Passwords containing URL-reserved characters must be percent-encoded inside the URLs.

`docker/postgres/init/01-roles.sh` creates the two roles only when the data volume is first initialised. After changing a role password in `.env`, re-initialise with `docker compose down -v` (this deletes all local database data), then `make up` and `make migrate`.

The Makefile points `QUARTET_ENV_FILE` at this checkout's `.env`; never run `make` targets in an untrusted checkout. Outside `make`, Quartet reads only an explicit `QUARTET_ENV_FILE` or `~/.quartet/.env`, never a `.env` in the current directory.

## Provider checks (spend real money)

These commands call the Anthropic and OpenAI APIs and are billed to the keys in `.env`. Run them only when you mean to spend.

```sh
uv run quartet llm verify-models          # checks models.toml against each vendor's model listing
uv run quartet llm smoke --max-usd 1.00   # one call per artifact schema, plus a tool round trip, per provider
uv run pytest -m live                     # live tests: tool loop, prompt-cache read, smoke
```

The smoke run stops at its `--max-usd` cap (1.00), and `pytest -m live` runs the same smoke check with that cap. Each run in the other live tests is capped at $0.25; the cache test makes two runs. Within `pytest -m live`, the caps add up to $2.00 at most; the expected spend is well below that (an estimate).
