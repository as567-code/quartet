"""``quartet llm``: provider checks. These reach the real vendors and spend real money."""

import asyncio
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated

import anthropic
import httpx2
import openai
import typer

from quartet.config import InsecureEnvFile, InvalidSettings, Settings, load_settings
from quartet.llm.anthropic_runner import make_anthropic_client
from quartet.llm.base import Provider, RoleRunner
from quartet.llm.catalog import ModelCatalog
from quartet.llm.openai_runner import make_openai_client
from quartet.llm.runner import MissingApiKey, build_role_runner
from quartet.llm.smoke import run_smoke

llm_app = typer.Typer(help="Provider checks (these spend real money).", no_args_is_help=True)


class ProviderChoice(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    ALL = "all"


def _providers(choice: ProviderChoice) -> list[Provider]:
    return list(Provider) if choice is ProviderChoice.ALL else [Provider(choice.value)]


def _load() -> Settings:
    try:
        return load_settings()
    except (InsecureEnvFile, InvalidSettings) as exc:
        typer.echo(f"✗ settings: {exc}")
        raise typer.Exit(code=1) from None


def _make_clients(
    settings: Settings, providers: list[Provider], http_client: httpx2.AsyncClient | None = None
) -> tuple[anthropic.AsyncAnthropic | None, openai.AsyncOpenAI | None]:
    """Vendor clients for the chosen providers; tests replace this to use a mock transport."""
    anthropic_client = openai_client = None
    if Provider.ANTHROPIC in providers:
        key = settings.anthropic_api_key
        if key is None or not key.get_secret_value():
            raise MissingApiKey("ANTHROPIC_API_KEY is not set")
        anthropic_client = make_anthropic_client(key, http_client=http_client)
    if Provider.OPENAI in providers:
        key = settings.openai_api_key
        if key is None or not key.get_secret_value():
            raise MissingApiKey("OPENAI_API_KEY is not set")
        openai_client = make_openai_client(key, http_client=http_client)
    return anthropic_client, openai_client


def _make_runners(settings: Settings, providers: list[Provider]) -> dict[Provider, RoleRunner]:
    """One runner per provider; tests replace this with scripted runners."""
    return {p: build_role_runner(settings, p) for p in providers}


def _failure(exc: Exception) -> str:
    """Error class and HTTP status only: SDK messages may echo request details."""
    status = getattr(exc, "status_code", None)
    return type(exc).__name__ + (f" (HTTP {status})" if status else "")


async def _verify_anthropic(client: anthropic.AsyncAnthropic, catalog: ModelCatalog) -> list[bool]:
    outcomes: list[bool] = []
    for info in (m for m in catalog if m.provider is Provider.ANTHROPIC):
        try:
            model = await client.models.retrieve(info.model)
            beta = await client.beta.models.retrieve(info.model)
        except anthropic.APIError as exc:
            typer.echo(f"✗ {info.model}: {_failure(exc)}")
            outcomes.append(False)
            continue
        problems: list[str] = []
        if model.max_input_tokens != info.context_window:
            problems.append(
                f"context window {model.max_input_tokens} != catalog {info.context_window}"
            )
        if model.max_tokens != info.max_output_tokens:
            problems.append(f"max output {model.max_tokens} != catalog {info.max_output_tokens}")
        allowed = list(beta.allowed_fallback_models or [])
        missing = [fb for fb in info.fallback_models if fb not in allowed]
        if missing:
            problems.append(f"fallback models not allowed: {', '.join(missing)}")
        shown = f"allowed fallbacks: {', '.join(allowed) or 'none'}"
        if problems:
            typer.echo(f"✗ {info.model}: {'; '.join(problems)} ({shown})")
        else:
            typer.echo(f"✓ {info.model}: limits match the catalog ({shown})")
        outcomes.append(not problems)
    return outcomes


async def _verify_openai(client: openai.AsyncOpenAI, catalog: ModelCatalog) -> list[bool]:
    outcomes: list[bool] = []
    for info in (m for m in catalog if m.provider is Provider.OPENAI):
        try:
            await client.models.retrieve(info.model)
        except openai.OpenAIError as exc:
            typer.echo(f"✗ {info.model}: {_failure(exc)}")
            outcomes.append(False)
        else:
            typer.echo(f"✓ {info.model}: available")
            outcomes.append(True)
    return outcomes


@llm_app.command("verify-models")
def verify_models(
    provider: Annotated[ProviderChoice, typer.Option(help="Which provider to check.")] = (
        ProviderChoice.ALL
    ),
) -> None:
    """Check each catalog model against the vendor's model listing."""
    providers = _providers(provider)
    settings = _load()
    try:
        anthropic_client, openai_client = _make_clients(settings, providers)
    except MissingApiKey as exc:
        typer.echo(f"✗ {exc}")
        raise typer.Exit(code=1) from None
    catalog = ModelCatalog.load()

    async def verify() -> list[bool]:
        outcomes: list[bool] = []
        if anthropic_client is not None:
            outcomes += await _verify_anthropic(anthropic_client, catalog)
        if openai_client is not None:
            outcomes += await _verify_openai(openai_client, catalog)
        return outcomes

    if not all(asyncio.run(verify())):
        raise typer.Exit(code=1)


@llm_app.command("smoke")
def smoke(
    provider: Annotated[ProviderChoice, typer.Option(help="Which provider to check.")] = (
        ProviderChoice.ALL
    ),
    max_usd: Annotated[str, typer.Option(help="Stop spending once this many USD are used.")] = (
        "1.00"
    ),
) -> None:
    """Run every artifact schema and a tool round trip against the real vendors."""
    try:
        budget = Decimal(max_usd)
    except InvalidOperation:
        typer.echo("✗ --max-usd must be a decimal number")
        raise typer.Exit(code=1) from None
    if not budget.is_finite() or budget <= 0:
        typer.echo("✗ --max-usd must be a positive number")
        raise typer.Exit(code=1)
    providers = _providers(provider)
    settings = _load()
    try:
        runners = _make_runners(settings, providers)
    except MissingApiKey as exc:
        typer.echo(f"✗ {exc}")
        raise typer.Exit(code=1) from None
    results = asyncio.run(run_smoke(runners, max_usd=budget))
    for r in results:
        typer.echo(
            f"{'✓' if r.ok else '✗'} {r.provider.value} {r.check}: {r.detail} (${r.cost_usd})"
        )
    total = sum((r.cost_usd for r in results), Decimal(0))
    typer.echo(f"total cost: ${total}")
    if not all(r.ok for r in results):
        raise typer.Exit(code=1)
