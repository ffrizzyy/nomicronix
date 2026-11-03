"""Deterministic, causal technical indicators used by the built-in proposers."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from nomicronix.market_data.models import Candle


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError("period must be at least 1")


def sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    """Simple moving average of the last `period` values, or None if too short."""
    _check_period(period)
    if len(values) < period:
        return None
    return sum(values[-period:], Decimal(0)) / period


def true_range(previous_close: Decimal, candle: Candle) -> Decimal:
    """Wilder's true range for one candle given the previous close."""
    return max(
        candle.high - candle.low,
        abs(candle.high - previous_close),
        abs(candle.low - previous_close),
    )


def atr(candles: Sequence[Candle], period: int = 14) -> Decimal | None:
    """Wilder's average true range over the full visible history.

    Needs `period + 1` candles: one previous close plus `period` true ranges.
    Returns None when there is not enough history.
    """
    _check_period(period)
    if len(candles) < period + 1:
        return None
    ranges = [
        true_range(candles[index - 1].close, candles[index]) for index in range(1, len(candles))
    ]
    if len(ranges) < period:
        return None
    value = sum(ranges[:period], Decimal(0)) / period
    for current in ranges[period:]:
        value = (value * (period - 1) + current) / period
    return value


def highest_high(candles: Sequence[Candle], period: int) -> Decimal | None:
    """Highest high of the last `period` candles, or None if too short."""
    _check_period(period)
    if len(candles) < period:
        return None
    return max(candle.high for candle in candles[-period:])


def lowest_low(candles: Sequence[Candle], period: int) -> Decimal | None:
    """Lowest low of the last `period` candles, or None if too short."""
    _check_period(period)
    if len(candles) < period:
        return None
    return min(candle.low for candle in candles[-period:])
