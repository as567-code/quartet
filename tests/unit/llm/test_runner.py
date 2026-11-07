from dataclasses import replace
from decimal import Decimal

import pytest

from quartet.config import Settings
from quartet.domain.artifacts import ARTIFACT_MODELS, ContextDigest, Plan
from quartet.llm.base import EMPTY_TOOLBOX, Provider, ToolSpec
from quartet.llm.fake import FakeProvider
from quartet.llm.runner import MissingApiKey, ProviderRoleRunner, build_role_runner
from quartet.llm.smoke import SMOKE_LIMITS, SMOKE_MODELS, EchoToolbox, run_smoke
from tests.unit.llm._records import ROLE_INPUT, cfg, plan_json, runner, scripted_runner
from tests.unit.llm._sse import text_message
from tests.unit.llm.conftest import Capture

pytestmark = pytest.mark.anyio


async def test_config_validated_before_any_call():
    fake = FakeProvider([])
    created: list[FakeProvider] = []

    def factory() -> FakeProvider:
        created.append(fake)
        return fake

    with pytest.raises(ValueError, match="max_output_tokens"):
        await runner(factory).run(
            replace(cfg(), max_output_tokens=500_000), ROLE_INPUT, EMPTY_TOOLBOX
        )
    assert fake.calls == 0 and created == []


async def test_provider_mismatch_rejected():
    fake = FakeProvider([])
    with pytest.raises(ValueError, match="openai"):
        await runner(lambda: fake).run(
            cfg(provider=Provider.OPENAI, model="gpt-6.1-sol"), ROLE_INPUT, EMPTY_TOOLBOX
        )
    assert fake.calls == 0


def test_build_role_runner_requires_key(settings_without_keys):
    with pytest.raises(MissingApiKey, match="ANTHROPIC_API_KEY") as info:
        build_role_runner(settings_without_keys, Provider.ANTHROPIC)
    assert str(info.value) == "ANTHROPIC_API_KEY is not set"
    with pytest.raises(MissingApiKey, match="OPENAI_API_KEY"):
        build_role_runner(settings_without_keys, Provider.OPENAI)


@pytest.mark.parametrize("provider", list(Provider))
def test_build_role_runner_with_key(settings_with_keys, provider):
    built = build_role_runner(settings_with_keys, provider)
    assert isinstance(built, ProviderRoleRunner) and built.provider is provider


async def test_built_runner_runs_a_step_over_the_given_client(
    settings_with_keys: Settings, capture: Capture
) -> None:
    built = build_role_runner(
        settings_with_keys,
        Provider.ANTHROPIC,
        http_client=capture.client(text_message(plan_json())),
    )
    result = await built.run(cfg(), ROLE_INPUT, EMPTY_TOOLBOX)
    assert isinstance(result.artifact, Plan) and len(capture.bodies) == 1


async def test_each_run_gets_a_fresh_adapter():
    adapters: list[FakeProvider] = []

    def factory() -> FakeProvider:
        from quartet.llm.fake import FakeTurn

        adapters.append(FakeProvider([FakeTurn(final=plan_json())]))
        return adapters[-1]

    r = runner(factory)
    await r.run(cfg(), ROLE_INPUT, EMPTY_TOOLBOX)
    await r.run(cfg(), ROLE_INPUT, EMPTY_TOOLBOX)
    assert len(adapters) == 2 and adapters[0] is not adapters[1]


async def test_smoke_covers_every_artifact_model_and_tool_check():
    results = await run_smoke({p: scripted_runner(p) for p in Provider}, max_usd=Decimal("1"))
    assert len(results) == 2 * (len(ARTIFACT_MODELS) + 1)
    assert all(r.ok for r in results), [r for r in results if not r.ok]
    names = [r.check for r in results if r.provider is Provider.OPENAI]
    assert names == [m.__name__ for m in ARTIFACT_MODELS] + ["tool_round_trip"]
    assert all(r.cost_usd == Decimal("0.01") for r in results)


async def test_smoke_stops_at_budget():
    runners = {p: scripted_runner(p, cost_per_check=Decimal("0.30")) for p in Provider}
    results = await run_smoke(runners, max_usd=Decimal("0.50"))
    assert [r.ok for r in results[:2]] == [True, True]
    rest = results[2:]
    assert rest and all(not r.ok for r in rest)
    assert all(r.detail == "skipped: smoke budget reached" and r.cost_usd == 0 for r in rest)


async def test_smoke_reports_a_failing_check_without_stopping():
    from quartet.llm.fake import FakeTurn

    bad = runner(lambda: FakeProvider([FakeTurn(final="not json")] * 3))
    results = await run_smoke({Provider.ANTHROPIC: bad}, max_usd=Decimal("1"))
    assert len(results) == len(ARTIFACT_MODELS) + 1 and not any(r.ok for r in results)
    assert results[0].detail.startswith("ArtifactInvalid")
    assert results[0].cost_usd > 0


async def test_smoke_tool_check_requires_the_echoed_goal():
    from quartet.llm.fake import FakeTurn

    digest = ContextDigest(
        schema_version=1, goal="other", constraints=[], open_questions=[], artifact_ids=[]
    )
    good = scripted_runner(Provider.ANTHROPIC)
    made: list[int] = []

    def factory():
        made.append(1)
        if len(made) <= len(ARTIFACT_MODELS):
            return good._adapter_factory()
        return FakeProvider(
            [
                FakeTurn(tool_calls=(("c1", "echo", {"text": "ping"}),)),
                FakeTurn(final=digest.model_dump_json()),
            ]
        )

    wrong = runner(factory)
    results = await run_smoke({Provider.ANTHROPIC: wrong}, max_usd=Decimal("1"))
    assert all(r.ok for r in results[:-1])
    assert results[-1].check == "tool_round_trip" and not results[-1].ok


async def test_echo_toolbox_records_calls():
    box = EchoToolbox()
    assert [s.name for s in box.specs] == ["echo"] and isinstance(box.specs[0], ToolSpec)
    outcome = await box.call("echo", {"text": "ping"})
    assert outcome.content == "ping" and not outcome.is_error and box.calls == ["ping"]


def test_smoke_constants():
    assert SMOKE_LIMITS.max_usd == Decimal("0.25") and SMOKE_LIMITS.max_turns == 4
    assert SMOKE_MODELS == {Provider.ANTHROPIC: "claude-opus-5-5", Provider.OPENAI: "gpt-6.1-sol"}
