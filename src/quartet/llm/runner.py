"""The role-runner facade: validates a config, builds a fresh adapter and runs one step."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

import httpx2

from quartet.config import Settings
from quartet.llm.anthropic_runner import AnthropicAdapter, make_anthropic_client
from quartet.llm.base import (
    NULL_RECORDER,
    Provider,
    RoleConfig,
    RoleInput,
    RoleResult,
    RoleRunner,
    StepRecorder,
    Toolbox,
)
from quartet.llm.catalog import ModelCatalog, validate_role_config
from quartet.llm.loop import LoopDeps, ProviderAdapter, run_step
from quartet.llm.openai_runner import OpenAIAdapter, make_openai_client
from quartet.llm.pricing import PricingTable
from quartet.llm.retry import RetryPolicy
from quartet.telemetry.redaction import Redactor, install_log_redaction, redactor_from_settings


class MissingApiKey(RuntimeError):
    """A provider key is not configured. The message names the environment variable only."""


_KEY_VARIABLES = {Provider.ANTHROPIC: "ANTHROPIC_API_KEY", Provider.OPENAI: "OPENAI_API_KEY"}


class ProviderRoleRunner(RoleRunner):
    def __init__(
        self,
        adapter_factory: Callable[[], ProviderAdapter],
        *,
        provider: Provider,
        pricing: PricingTable,
        catalog: ModelCatalog,
        redactor: Redactor,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._adapter_factory = adapter_factory
        self.provider = provider
        self._pricing = pricing
        self._catalog = catalog
        self._redactor = redactor
        self._policy = retry_policy or RetryPolicy()
        self._clock = clock
        self._sleep = sleep
        self._rand = rand

    async def run(
        self,
        config: RoleConfig,
        handover_input: RoleInput,
        tools: Toolbox,
        *,
        recorder: StepRecorder = NULL_RECORDER,
    ) -> RoleResult:
        if config.provider != self.provider:
            raise ValueError(
                f"config is for provider {config.provider.value}, "
                f"this runner is for {self.provider.value}"
            )
        validate_role_config(config, self._catalog, self._pricing)
        deps = LoopDeps(
            pricing=self._pricing,
            redactor=self._redactor,
            retry_policy=self._policy,
            clock=self._clock,
            sleep=self._sleep,
            rand=self._rand,
        )
        return await run_step(
            self._adapter_factory(), config, handover_input, tools, recorder, deps
        )


def build_role_runner(
    settings: Settings, provider: Provider, *, http_client: httpx2.AsyncClient | None = None
) -> ProviderRoleRunner:
    redactor = redactor_from_settings(settings)
    install_log_redaction(redactor)
    key = settings.anthropic_api_key if provider is Provider.ANTHROPIC else settings.openai_api_key
    if key is None or not key.get_secret_value():
        raise MissingApiKey(f"{_KEY_VARIABLES[provider]} is not set")
    factory: Callable[[], ProviderAdapter]
    if provider is Provider.ANTHROPIC:
        anthropic_client = make_anthropic_client(key, http_client=http_client)
        factory = lambda: AnthropicAdapter(anthropic_client)  # noqa: E731
    else:
        openai_client = make_openai_client(key, http_client=http_client)
        factory = lambda: OpenAIAdapter(openai_client)  # noqa: E731
    return ProviderRoleRunner(
        factory,
        provider=provider,
        pricing=PricingTable.load(),
        catalog=ModelCatalog.load(),
        redactor=redactor,
    )
