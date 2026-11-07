"""Dollars from the token counts a response reports (C-31.10).

The interface's prices, per million tokens: Opus 5.5 $4 input and $20 output, Sonnet
5.5 $2 and $10, Haiku 5.5 $0.10 and $0.50. From the claude-api skill's pricing notes:
cache reads cost $0.20 on Opus 5.5 and Sonnet 5.5 and a tenth of input on Haiku 5.5;
a cache write costs 1.25 times input for the 5-minute lifetime and twice input for
the hour; Haiku 5.5 charges $0.50 and $2.50 when the prompt passes 100,000 tokens.
A compaction iteration's tokens are not in the top-level usage (the SDK's
`BetaUsage.iterations`), so `usd` adds each one.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

MILLION = 1_000_000
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    #: Above this many prompt tokens (input, cache reads and writes) the long rates apply.
    long_above: int | None = None
    long_input: float = 0.0
    long_output: float = 0.0
    long_cache_read: float = 0.0


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.20),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01, long_above=100_000,
                              long_input=0.50, long_output=2.50, long_cache_read=0.05),
}


def known(model: str) -> bool:
    return model in PRICES


def _count(usage: Mapping, name: str) -> int:
    value = usage.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def four(usage: Mapping | None) -> dict[str, int]:
    """The `response` event's `usage`: the interface's four counts, absent ones 0."""
    usage = usage or {}
    return {name: _count(usage, name) for name in
            ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}


def _one(price: Price, usage: Mapping) -> float:
    input_tokens = _count(usage, "input_tokens")
    output_tokens = _count(usage, "output_tokens")
    read = _count(usage, "cache_read_input_tokens")
    written = _count(usage, "cache_creation_input_tokens")
    hour = 0
    breakdown = usage.get("cache_creation")
    if isinstance(breakdown, Mapping):
        hour = min(_count(breakdown, "ephemeral_1h_input_tokens"), written)
    minutes = written - hour
    rate_in, rate_out, rate_read = price.input, price.output, price.cache_read
    if price.long_above is not None and input_tokens + read + written > price.long_above:
        rate_in, rate_out, rate_read = price.long_input, price.long_output, price.long_cache_read
    return (input_tokens * rate_in + output_tokens * rate_out + read * rate_read
            + minutes * rate_in * CACHE_WRITE_5M + hour * rate_in * CACHE_WRITE_1H) / MILLION


def usd(model: str, usage: Mapping | None) -> float:
    """What one response cost: its top-level usage plus each compaction iteration."""
    price = PRICES[model]
    usage = usage or {}
    total = _one(price, usage)
    iterations = usage.get("iterations")
    if isinstance(iterations, list):
        for item in iterations:
            if isinstance(item, Mapping) and item.get("type") == "compaction":
                total += _one(price, item)
    return total
