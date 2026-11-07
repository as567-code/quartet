"""Live provider tests: real API calls that spend real money. Run only with ``-m live``."""

from decimal import Decimal

import pytest

from quartet.domain.artifacts import ContextDigest
from quartet.domain.states import Role
from quartet.llm.base import Provider, RoleConfig, RoleInput
from quartet.llm.smoke import (
    SMOKE_LIMITS,
    SMOKE_MODELS,
    TOOL_INSTRUCTIONS,
    EchoToolbox,
    run_smoke,
)
from tests.live.conftest import RunnerFactory

pytestmark = [pytest.mark.live, pytest.mark.anyio]

SYSTEM_PREAMBLE = (
    "You are a terse assistant for a cache test. Reference material follows; ignore it.\n"
)
CACHE_PAD = "\n".join(
    f"Reference line {i}: the quick brown fox jumps over the lazy dog." for i in range(1, 301)
)


def _digest_config(provider: Provider, system_prompt: str) -> RoleConfig:
    return RoleConfig(
        role=Role.ORCHESTRATOR,
        provider=provider,
        model=SMOKE_MODELS[provider],
        effort="low",
        system_prompt=system_prompt,
        output_model=ContextDigest,
        max_output_tokens=8_000,
        limits=SMOKE_LIMITS,
        server_fallback=False,
    )


@pytest.mark.parametrize("provider", list(Provider))
async def test_live_tool_loop_and_artifact(provider: Provider, live_runner: RunnerFactory) -> None:
    toolbox = EchoToolbox()
    result = await live_runner(provider).run(
        _digest_config(provider, "You produce small example objects for a schema test."),
        RoleInput(TOOL_INSTRUCTIONS, (), "tool loop"),
        toolbox,
    )
    assert toolbox.calls == ["ping"]
    assert isinstance(result.artifact, ContextDigest) and result.artifact.goal.strip() == "ping"
    assert len(result.calls) >= 2 and result.totals.cost_usd > 0


async def test_live_anthropic_prompt_cache_read(live_runner: RunnerFactory) -> None:
    config = _digest_config(Provider.ANTHROPIC, SYSTEM_PREAMBLE + CACHE_PAD)
    role_input = RoleInput(
        'Return a ContextDigest whose goal is "cache probe", with empty lists.', (), "cache probe"
    )
    runner = live_runner(Provider.ANTHROPIC)
    first = await runner.run(config, role_input, EchoToolbox())
    second = await runner.run(config, role_input, EchoToolbox())
    assert first.calls[0].usage.cache_write_short_tokens > 0
    assert second.calls[0].usage.cache_read_tokens > 0


async def test_live_smoke_both_providers(live_runner: RunnerFactory) -> None:
    runners = {p: live_runner(p) for p in Provider}
    results = await run_smoke(runners, max_usd=Decimal("1.00"))
    failed = [f"{r.provider.value}/{r.check}: {r.detail}" for r in results if not r.ok]
    assert not failed, failed
