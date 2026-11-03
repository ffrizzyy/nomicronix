"""The proposer protocol and the built-in deterministic strategies."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from nomicronix.strategy_engine.indicators import atr, highest_high, sma
from nomicronix.strategy_engine.models import (
    SignalIdea,
    decimal_text,
    validate_confidence,
)
from nomicronix.strategy_engine.views import CandleView


class SignalProposer(Protocol):
    """A causal strategy that turns one closed-candle view into at most one idea."""

    strategy_id: str
    min_history: int

    def propose(self, view: CandleView) -> SignalIdea | None:
        """Return a long-only trade idea for the decision candle, or None."""
        ...


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _check_positive(name: str, value: float) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class SmaCross:
    """Long when the fast simple moving average crosses above the slow one.

    The stop is `stop_atr` ATRs below entry and the target is `target_rr` times
    the stop distance above entry. Fires only on the crossing candle, not while
    the fast average stays above the slow one.
    """

    fast: int = 20
    slow: int = 50
    atr_period: int = 14
    stop_atr: float = 2.0
    target_rr: float = 3.0
    confidence: float = 0.6

    strategy_id = "sma-cross"

    def __post_init__(self) -> None:
        if self.fast < 1:
            raise ValueError("fast must be at least 1")
        if self.slow <= self.fast:
            raise ValueError("slow must be greater than fast")
        if self.atr_period < 1:
            raise ValueError("atr_period must be at least 1")
        _check_positive("stop_atr", self.stop_atr)
        _check_positive("target_rr", self.target_rr)
        validate_confidence(self.confidence)

    @property
    def min_history(self) -> int:
        return max(self.slow + 1, self.atr_period + 1)

    def propose(self, view: CandleView) -> SignalIdea | None:
        if len(view) < self.min_history:
            return None

        closes = [candle.close for candle in view]
        fast_now = sma(closes, self.fast)
        slow_now = sma(closes, self.slow)
        if fast_now is None or slow_now is None:
            return None

        previous = closes[:-1]
        fast_previous = sma(previous, self.fast)
        slow_previous = sma(previous, self.slow)
        if fast_previous is None or slow_previous is None:
            return None
        if not (fast_previous <= slow_previous and fast_now > slow_now):
            return None

        atr_value = atr(view, self.atr_period)
        if atr_value is None or atr_value <= 0:
            return None

        entry = view.candle.close
        risk = _decimal(self.stop_atr) * atr_value
        stop = entry - risk
        target = entry + _decimal(self.target_rr) * risk
        if stop <= 0:
            return None

        return SignalIdea(
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            confidence=self.confidence,
            rationale="fast SMA crossed above slow SMA",
            features={
                "atr": decimal_text(atr_value),
                "fast": str(self.fast),
                "fast_sma": decimal_text(fast_now),
                "slow": str(self.slow),
                "slow_sma": decimal_text(slow_now),
            },
        )


@dataclass(frozen=True, slots=True)
class DonchianBreakout:
    """Long on a fresh close above the prior Donchian channel.

    A signal only fires on the first candle of a breakout. If the previous close
    was already above its own prior channel, the move is considered in progress
    and no new idea is returned.
    """

    entry_period: int = 20
    atr_period: int = 14
    stop_atr: float = 2.0
    target_rr: float = 2.0
    confidence: float = 0.55

    strategy_id = "donchian-breakout"

    def __post_init__(self) -> None:
        if self.entry_period < 1:
            raise ValueError("entry_period must be at least 1")
        if self.atr_period < 1:
            raise ValueError("atr_period must be at least 1")
        _check_positive("stop_atr", self.stop_atr)
        _check_positive("target_rr", self.target_rr)
        validate_confidence(self.confidence)

    @property
    def min_history(self) -> int:
        return max(self.entry_period + 1, self.atr_period + 1)

    def propose(self, view: CandleView) -> SignalIdea | None:
        if len(view) < self.min_history:
            return None

        candles = list(view)
        channel_high = highest_high(candles[:-1], self.entry_period)
        if channel_high is None or view.candle.close <= channel_high:
            return None

        previous_candles = candles[:-1]
        previous_channel = highest_high(previous_candles[:-1], self.entry_period)
        if previous_channel is not None and previous_candles[-1].close > previous_channel:
            return None

        atr_value = atr(candles, self.atr_period)
        if atr_value is None or atr_value <= 0:
            return None

        entry = view.candle.close
        risk = _decimal(self.stop_atr) * atr_value
        stop = entry - risk
        target = entry + _decimal(self.target_rr) * risk
        if stop <= 0:
            return None

        return SignalIdea(
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            confidence=self.confidence,
            rationale=f"close broke above the prior {self.entry_period}-candle high",
            features={
                "atr": decimal_text(atr_value),
                "channel_high": decimal_text(channel_high),
                "close": decimal_text(entry),
                "entry_period": str(self.entry_period),
            },
        )
