"""Sourced pricing table and cost arithmetic (USD per 1M tokens, Standard tier).

Prices live in ``pricing.toml`` as strings and become ``Decimal`` here; a float price is
rejected so binary rounding never reaches a cost. Every model carries the URL and date it
was read from.
"""

import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from quartet.llm.base import PricingUnavailable, Provider, Usage, UsagePart

_PER = Decimal(1_000_000)
_SIX_DP = Decimal("0.000001")
_RATE_FIELDS = ("input", "cache_read", "cache_write_short", "cache_write_long", "output")


@dataclass(frozen=True, slots=True)
class Rates:
    input: Decimal
    cache_read: Decimal
    cache_write_short: Decimal
    cache_write_long: Decimal
    output: Decimal


@dataclass(frozen=True, slots=True)
class ModelPrice:
    model: str
    provider: Provider
    standard: Rates
    long_context_threshold: int | None
    long_context: Rates | None
    source_url: str
    retrieved: date


def _cost_at(rates: Rates, usage: Usage) -> Decimal:
    # reasoning_tokens are already inside output_tokens: never added.
    total = (
        usage.uncached_input_tokens * rates.input
        + usage.cache_read_tokens * rates.cache_read
        + usage.cache_write_short_tokens * rates.cache_write_short
        + usage.cache_write_long_tokens * rates.cache_write_long
        + usage.output_tokens * rates.output
    )
    return (total / _PER).quantize(_SIX_DP, rounding=ROUND_HALF_UP)


def cost_of(price: ModelPrice, usage: Usage) -> Decimal:
    rates = price.standard
    if (
        price.long_context_threshold is not None
        and price.long_context is not None
        and usage.prompt_tokens > price.long_context_threshold
    ):
        rates = price.long_context
    return _cost_at(rates, usage)


def _parse_rates(model: str, raw: Mapping[str, Any]) -> Rates:
    values: dict[str, Decimal] = {}
    for name in _RATE_FIELDS:
        value = raw.get(name)
        if not isinstance(value, str):
            raise ValueError(f"{model}: {name} must be a string price")
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise ValueError(f"{model}: {name} is not a decimal") from None
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(f"{model}: {name} must be a finite price >= 0")
        values[name] = parsed
    return Rates(**values)


def _parse_model(model: str, raw: Mapping[str, Any]) -> ModelPrice:
    try:
        provider = Provider(str(raw.get("provider")))
    except ValueError:
        raise ValueError(f"{model}: unknown provider") from None
    source_url = raw.get("source_url")
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        raise ValueError(f"{model}: source_url must be an https:// URL")
    retrieved = raw.get("retrieved")
    if not isinstance(retrieved, date):
        raise ValueError(f"{model}: retrieved must be a date")
    threshold: int | None = None
    long_context: Rates | None = None
    lc = raw.get("long_context")
    if lc is not None:
        threshold = lc.get("threshold_tokens")
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
            raise ValueError(f"{model}: threshold_tokens must be a positive integer")
        long_context = _parse_rates(model, lc)
    return ModelPrice(
        model=model,
        provider=provider,
        standard=_parse_rates(model, raw),
        long_context_threshold=threshold,
        long_context=long_context,
        source_url=source_url,
        retrieved=retrieved,
    )


class PricingTable:
    def __init__(self, prices: Mapping[str, ModelPrice]) -> None:
        self._prices = dict(prices)

    @classmethod
    def load(cls, source: Traversable | Path | None = None) -> "PricingTable":
        src = source if source is not None else files("quartet.llm").joinpath("pricing.toml")
        return cls.from_mapping(tomllib.loads(src.read_text(encoding="utf-8")))

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PricingTable":
        models = data.get("models", {})
        return cls({name: _parse_model(name, raw) for name, raw in models.items()})

    def __iter__(self) -> Iterator[ModelPrice]:
        return iter(self._prices.values())

    def has(self, model: str) -> bool:
        return model in self._prices

    def price(self, model: str) -> ModelPrice:
        try:
            return self._prices[model]
        except KeyError:
            raise PricingUnavailable(model=model) from None

    def cost(self, parts: Sequence[UsagePart]) -> Decimal:
        return sum((cost_of(self.price(p.model), p.usage) for p in parts), Decimal(0)).quantize(
            _SIX_DP
        )

    def ceiling(self, provider: Provider) -> Rates:
        """Field-wise maximum over the provider's standard and long-context rates."""
        candidates: list[Rates] = []
        for p in self._prices.values():
            if p.provider is provider:
                candidates.append(p.standard)
                if p.long_context is not None:
                    candidates.append(p.long_context)
        if not candidates:
            raise PricingUnavailable(model=f"<any {provider.value} model>")
        return Rates(**{f: max(getattr(r, f) for r in candidates) for f in _RATE_FIELDS})

    def cost_or_ceiling(
        self, parts: Sequence[UsagePart], provider: Provider
    ) -> tuple[Decimal, bool]:
        """Price unknown models at the provider's ceiling; the flag says that happened."""
        total = Decimal(0)
        estimated = False
        for part in parts:
            if self.has(part.model):
                total += cost_of(self._prices[part.model], part.usage)
            else:
                total += _cost_at(self.ceiling(provider), part.usage)
                estimated = True
        return total.quantize(_SIX_DP), estimated

    def cache_read_rate(self, model: str) -> Decimal:
        return self.price(model).standard.cache_read
