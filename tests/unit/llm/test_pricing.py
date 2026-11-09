import importlib.resources
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from quartet.llm.base import PricingUnavailable, Provider, Usage, UsagePart
from quartet.llm.pricing import PricingTable, cost_of

T = PricingTable.load()


def test_package_data_present() -> None:
    files = importlib.resources.files("quartet.llm")
    assert files.joinpath("pricing.toml").is_file()
    assert files.joinpath("models.toml").is_file()


def test_opus_cost_to_the_cent() -> None:
    u = Usage(
        uncached_input_tokens=12_000,
        cache_read_tokens=100_000,
        cache_write_short_tokens=8_000,
        cache_write_long_tokens=0,
        output_tokens=3_000,
        reasoning_tokens=1_000,
    )
    # 12k*4 + 100k*0.20 + 8k*5 + 3k*20 = 168,000 / 1e6
    assert T.cost((UsagePart("claude-opus-5-5", u),)) == Decimal("0.168000")


def test_sol_cost_bills_cache_writes() -> None:
    u = Usage(50_000, 200_000, 10_000, 2_000, 4_000, 3_000)
    # 50k*2 + 200k*0.10 + 10k*2.50 + 2k*5 + 4k*10 = 195,000 / 1e6
    assert T.cost((UsagePart("gpt-6.1-sol", u),)) == Decimal("0.195000")


def test_sol_long_context_prices_whole_request_and_boundary_is_short() -> None:
    assert T.cost((UsagePart("gpt-6.1-sol", Usage(272_001, 0, 0, 0, 1_000, 0)),)) == Decimal(
        "1.103004"
    )
    assert T.cost((UsagePart("gpt-6.1-sol", Usage(272_000, 0, 0, 0, 1_000, 0)),)) == Decimal(
        "0.554000"
    )


def test_parts_priced_per_model() -> None:
    u = Usage(1_000, 0, 0, 0, 0, 0)
    parts = (UsagePart("claude-opus-5-5", u), UsagePart("claude-sonnet-5-5", u))
    assert T.cost(parts) == Decimal("0.006000")


def test_tiny_cost_quantized() -> None:
    assert T.cost((UsagePart("claude-opus-5-5", Usage(0, 0, 0, 0, 1, 0)),)) == Decimal("0.000020")


def test_rounds_half_up_to_six_places() -> None:
    price = T.price("claude-sonnet-5-5")
    cheap = replace(price, standard=replace(price.standard, input=Decimal("0.5")))
    assert cost_of(cheap, Usage(1, 0, 0, 0, 0, 0)) == Decimal("0.000001")


def test_sources_dates_and_string_prices() -> None:
    assert all(p.source_url.startswith("https://") and p.retrieved for p in T)
    with pytest.raises(ValueError):
        PricingTable.from_mapping({"models": {"m": {"provider": "openai", "input": 4.0}}})


def _model(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "provider": "openai",
        "input": "1",
        "cache_read": "1",
        "cache_write_short": "1",
        "cache_write_long": "1",
        "output": "1",
        "source_url": "https://example.com/p",
        "retrieved": T.price("gpt-6-astra").retrieved,
    }
    return {**base, **overrides}


def test_from_mapping_accepts_a_complete_model() -> None:
    table = PricingTable.from_mapping({"models": {"m": _model()}})
    assert table.has("m")


@pytest.mark.parametrize(
    "bad",
    [
        {"source_url": "http://example.com"},
        {"source_url": ""},
        {"retrieved": "2026-11-06"},
        {"provider": "mystery"},
        {"output": "-1"},
        {"output": "abc"},
        {"output": 20},
    ],
)
def test_from_mapping_rejects_bad_models(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        PricingTable.from_mapping({"models": {"m": _model(**bad)}})


def test_from_mapping_rejects_non_string_long_context_price() -> None:
    lc = {
        "threshold_tokens": 10,
        "input": "1",
        "cache_read": "1",
        "cache_write_short": "1",
        "cache_write_long": "1",
        "output": 2.0,
    }
    with pytest.raises(ValueError):
        PricingTable.from_mapping({"models": {"m": _model(long_context=lc)}})


def test_unknown_model_and_ceiling() -> None:
    with pytest.raises(PricingUnavailable):
        T.price("claude-mystery")
    assert not T.has("claude-mystery")
    assert T.ceiling(Provider.OPENAI).output == Decimal("75.00")
    cost, estimated = T.cost_or_ceiling(
        (UsagePart("gpt-mystery", Usage(0, 0, 0, 0, 1_000, 0)),), Provider.OPENAI
    )
    assert (cost, estimated) == (Decimal("0.075000"), True)


def test_ceiling_is_fieldwise_max() -> None:
    r = T.ceiling(Provider.OPENAI)
    assert (r.input, r.cache_read, r.cache_write_short, r.cache_write_long) == (
        Decimal("20.00"),
        Decimal("2.00"),
        Decimal("25.00"),
        Decimal("50.00"),
    )
    assert T.ceiling(Provider.ANTHROPIC).output == Decimal("20.00")


def test_ceiling_without_models_raises() -> None:
    with pytest.raises(PricingUnavailable):
        PricingTable.from_mapping({"models": {}}).ceiling(Provider.OPENAI)


def test_cost_or_ceiling_known_model_is_not_estimated() -> None:
    u = Usage(1_000, 0, 0, 0, 0, 0)
    assert T.cost_or_ceiling((UsagePart("claude-opus-5-5", u),), Provider.ANTHROPIC) == (
        Decimal("0.004000"),
        False,
    )


def test_cache_read_rate() -> None:
    assert T.cache_read_rate("claude-opus-5-5") == Decimal("0.20")
    with pytest.raises(PricingUnavailable):
        T.cache_read_rate("claude-mystery")
