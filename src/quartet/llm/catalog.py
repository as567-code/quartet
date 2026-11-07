"""Model catalog from ``models.toml`` and the startup check for a role's model choice."""

import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Literal, cast

from quartet.llm.base import EFFORTS, Provider, RoleConfig
from quartet.llm.pricing import PricingTable

_VERIFIED_VIA = ("docs", "models-api")


@dataclass(frozen=True, slots=True)
class ModelInfo:
    model: str
    provider: Provider
    context_window: int
    max_output_tokens: int
    efforts: frozenset[str]
    fallback_models: tuple[str, ...]
    verified_via: Literal["docs", "models-api"]
    verified_at: date


def _positive_int(model: str, raw: Mapping[str, Any], name: str) -> int:
    value = raw.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{model}: {name} must be a positive integer")
    return value


def _str_list(model: str, raw: Mapping[str, Any], name: str) -> list[str]:
    value = raw.get(name)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{model}: {name} must be a list of strings")
    return cast("list[str]", value)


def _parse(model: str, raw: Mapping[str, Any]) -> ModelInfo:
    try:
        provider = Provider(str(raw.get("provider")))
    except ValueError:
        raise ValueError(f"{model}: unknown provider") from None
    efforts = frozenset(_str_list(model, raw, "efforts"))
    if not efforts or not efforts <= EFFORTS:
        raise ValueError(f"{model}: efforts must be a non-empty subset of {sorted(EFFORTS)}")
    verified_via = raw.get("verified_via")
    if verified_via not in _VERIFIED_VIA:
        raise ValueError(f"{model}: verified_via must be one of {list(_VERIFIED_VIA)}")
    verified_at = raw.get("verified_at")
    if not isinstance(verified_at, date):
        raise ValueError(f"{model}: verified_at must be a date")
    return ModelInfo(
        model=model,
        provider=provider,
        context_window=_positive_int(model, raw, "context_window"),
        max_output_tokens=_positive_int(model, raw, "max_output_tokens"),
        efforts=efforts,
        fallback_models=tuple(_str_list(model, raw, "fallback_models")),
        verified_via=verified_via,
        verified_at=verified_at,
    )


class ModelCatalog:
    def __init__(self, models: Mapping[str, ModelInfo]) -> None:
        self._models = dict(models)

    @classmethod
    def load(cls, source: Traversable | Path | None = None) -> "ModelCatalog":
        src = source if source is not None else files("quartet.llm").joinpath("models.toml")
        return cls.from_mapping(tomllib.loads(src.read_text(encoding="utf-8")))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ModelCatalog":
        models = data.get("models", {})
        return cls({name: _parse(name, raw) for name, raw in models.items()})

    def get(self, model: str) -> ModelInfo:
        try:
            return self._models[model]
        except KeyError:
            raise ValueError(f"unknown model {model!r}") from None

    def __iter__(self) -> Iterator[ModelInfo]:
        return iter(self._models.values())


def validate_role_config(config: RoleConfig, catalog: ModelCatalog, pricing: PricingTable) -> None:
    info = catalog.get(config.model)
    if info.provider is not config.provider:
        raise ValueError(f"model {config.model!r} belongs to provider {info.provider.value}")
    if config.effort not in info.efforts:
        raise ValueError(f"effort {config.effort!r} is not offered by {config.model!r}")
    if config.max_output_tokens > info.max_output_tokens:
        raise ValueError(
            f"max_output_tokens {config.max_output_tokens} exceeds {info.max_output_tokens}"
        )
    if not pricing.has(config.model):
        raise ValueError(f"model {config.model!r} is not priced")
    if config.server_fallback:
        if not info.fallback_models:
            raise ValueError(f"server_fallback set but {config.model!r} has no fallback models")
        for fb in info.fallback_models:
            if not pricing.has(fb):
                raise ValueError(f"fallback model {fb!r} is not priced")
