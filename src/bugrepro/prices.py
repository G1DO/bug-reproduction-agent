"""Model prices and the bounds that keep a run under its cost cap.

Source: https://api-docs.deepseek.com/quick_start/pricing
Checked: 2026-11-18.

DeepSeek lists peak and off-peak rates. Peak is 01:00-04:00 and 06:00-10:00
UTC, Monday through Friday, excluding Chinese public holidays. The tool
reserves and charges the peak tier: a call that starts off-peak can finish in a
peak window, and a holiday weekday priced as peak is still safe. Off-peak
rates are recorded so a later change can use the provider's calendar.

DeepSeek enables prompt caching automatically and documents no way to turn it
off. The worst case prices every input token as a cache miss. The actual cost
uses the returned cache-hit split, which is never higher.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

PER_MILLION: Final = Decimal(1_000_000)
PRICES_SOURCE: Final = "https://api-docs.deepseek.com/quick_start/pricing"
PRICES_CHECKED: Final = "2026-11-18"

# DeepSeek's Chat Completions reference documents no fixed per-request token
# overhead. This allowance covers a provider-side system or tool prompt that is
# not in the request body. The margin covers token/byte rounding and framing.
REQUEST_OVERHEAD_TOKENS: Final = 1024
INPUT_MARGIN: Final = Decimal("1.2")

# Only the highest tier is safe to reserve. A request is refused if the table
# has no entry for this mode.
ENFORCED_PRICING_MODE: Final = "peak"


@dataclass(frozen=True)
class PriceTier:
    """One pricing mode, in US dollars per million tokens."""

    multiplier: Decimal
    input_cache_miss_usd_per_million: Decimal
    input_cache_hit_usd_per_million: Decimal
    output_usd_per_million: Decimal


@dataclass(frozen=True)
class ModelPrices:
    """The price modes published for one model ID."""

    tiers: dict[str, PriceTier]


PEAK: Final = PriceTier(
    multiplier=Decimal(1),
    input_cache_miss_usd_per_million=Decimal("0.30"),
    input_cache_hit_usd_per_million=Decimal("0.006"),
    output_usd_per_million=Decimal("1.20"),
)
OFF_PEAK: Final = PriceTier(
    multiplier=Decimal("0.5"),
    input_cache_miss_usd_per_million=Decimal("0.15"),
    input_cache_hit_usd_per_million=Decimal("0.003"),
    output_usd_per_million=Decimal("0.60"),
)

PRICES: Final[dict[str, ModelPrices]] = {
    "deepseek-flash": ModelPrices(tiers={"peak": PEAK, "off-peak": OFF_PEAK}),
}


def _tokens_cost(tokens: int, usd_per_million: Decimal) -> Decimal:
    return Decimal(tokens) * usd_per_million / PER_MILLION


def input_bound_tokens(request_bytes: int, provider_tokens: int | None = None) -> int:
    """Return the most input tokens a request can be billed for."""
    count = max(request_bytes, provider_tokens or 0)
    return math.ceil((Decimal(count) + Decimal(REQUEST_OVERHEAD_TOKENS)) * INPUT_MARGIN)


def worst_case_usd(tier: PriceTier, request_bytes: int, max_tokens: int) -> Decimal:
    """Return the most a call can cost before it is made."""
    return _tokens_cost(
        input_bound_tokens(request_bytes),
        tier.input_cache_miss_usd_per_million,
    ) + _tokens_cost(max_tokens, tier.output_usd_per_million)


def affordable_max_tokens(tier: PriceTier, request_bytes: int, usd: Decimal) -> int:
    """Return how many output tokens fit in ``usd`` after the input cost."""
    remaining = usd - _tokens_cost(
        input_bound_tokens(request_bytes),
        tier.input_cache_miss_usd_per_million,
    )
    if remaining <= 0:
        return 0
    return int(remaining * PER_MILLION / tier.output_usd_per_million)


def actual_cost_usd(
    tier: PriceTier,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int = 0,
) -> Decimal:
    """Return the cost of the usage the provider reported."""
    cache_hit = min(max(cache_hit_tokens, 0), prompt_tokens)
    cache_miss = prompt_tokens - cache_hit
    return (
        _tokens_cost(cache_miss, tier.input_cache_miss_usd_per_million)
        + _tokens_cost(cache_hit, tier.input_cache_hit_usd_per_million)
        + _tokens_cost(completion_tokens, tier.output_usd_per_million)
    )
