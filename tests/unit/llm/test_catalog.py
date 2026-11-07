import datetime
from dataclasses import replace

import pytest

from quartet.llm.base import Provider, RoleConfig
from quartet.llm.catalog import ModelCatalog, validate_role_config
from quartet.llm.pricing import PricingTable


def test_catalog_loads_package_data() -> None:
    assert {m.model for m in ModelCatalog.load()} == {
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "gpt-6.1-sol",
        "gpt-6-astra",
    }


def test_get_unknown_model_raises() -> None:
    with pytest.raises(ValueError, match="unknown model"):
        ModelCatalog.load().get("claude-mystery")


def test_catalog_values() -> None:
    info = ModelCatalog.load().get("gpt-6.1-sol")
    assert (info.context_window, info.max_output_tokens) == (1_050_000, 128_000)
    assert info.fallback_models == ()
    assert info.verified_via == "docs"


def test_validate_role_config(role_config: RoleConfig) -> None:
    validate_role_config(role_config, ModelCatalog.load(), PricingTable.load())
    for bad in (
        {"max_output_tokens": 200_000},
        {"model": "claude-mystery"},
        {"server_fallback": True},
    ):
        with pytest.raises(ValueError):
            validate_role_config(
                replace(role_config, **bad), ModelCatalog.load(), PricingTable.load()
            )


def test_provider_mismatch_rejected(role_config: RoleConfig) -> None:
    with pytest.raises(ValueError, match="provider"):
        validate_role_config(
            replace(role_config, provider=Provider.OPENAI), ModelCatalog.load(), PricingTable.load()
        )


def test_unpriced_model_rejected(role_config: RoleConfig) -> None:
    with pytest.raises(ValueError, match="priced"):
        validate_role_config(
            role_config, ModelCatalog.load(), PricingTable.from_mapping({"models": {}})
        )


def test_effort_not_offered_rejected(role_config: RoleConfig) -> None:
    catalog = ModelCatalog.from_mapping(
        {
            "models": {
                "claude-opus-5-5": {
                    "provider": "anthropic",
                    "context_window": 1,
                    "max_output_tokens": 128_000,
                    "efforts": ["low"],
                    "fallback_models": [],
                    "verified_via": "docs",
                    "verified_at": datetime.date(2026, 11, 6),
                }
            }
        }
    )
    with pytest.raises(ValueError, match="effort"):
        validate_role_config(role_config, catalog, PricingTable.load())


def _catalog_with_fallbacks(fallbacks: list[str]) -> ModelCatalog:
    return ModelCatalog.from_mapping(
        {
            "models": {
                "claude-opus-5-5": {
                    "provider": "anthropic",
                    "context_window": 1_000_000,
                    "max_output_tokens": 128_000,
                    "efforts": ["high"],
                    "fallback_models": fallbacks,
                    "verified_via": "docs",
                    "verified_at": datetime.date(2026, 11, 6),
                }
            }
        }
    )


def test_server_fallback_needs_priced_fallback_models(role_config: RoleConfig) -> None:
    fb = replace(role_config, server_fallback=True)
    validate_role_config(fb, _catalog_with_fallbacks(["claude-sonnet-5-5"]), PricingTable.load())
    with pytest.raises(ValueError, match="fallback"):
        validate_role_config(fb, _catalog_with_fallbacks(["claude-mystery"]), PricingTable.load())
