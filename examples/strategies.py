"""Small, dependency-free strategies used in examples and tests."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from nomicronix.backtesting.models import Action, Signal
from nomicronix.backtesting.strategy import Strategy
from nomicronix.market_data.models import Candle


class SmaCross(Strategy):
    """Long when the fast SMA crosses above the slow SMA; flat on the reverse."""

    def __init__(self, fast: int = 20, slow: int = 50) -> None:
        if fast < 1 or slow <= fast:
            raise ValueError("require 1 <= fast < slow")
        self.fast = fast
        self.slow = slow

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> Signal | None:
        if len(history) < self.slow + 1:
            return None
        closes = [item.close for item in history]
        fast_now = sum(closes[-self.fast :], Decimal(0)) / self.fast
        slow_now = sum(closes[-self.slow :], Decimal(0)) / self.slow
        fast_prev = sum(closes[-self.fast - 1 : -1], Decimal(0)) / self.fast
        slow_prev = sum(closes[-self.slow - 1 : -1], Decimal(0)) / self.slow

        if fast_prev <= slow_prev and fast_now > slow_now:
            return Signal(
                Action.ENTER_LONG,
                stop_price=candle.close * Decimal("0.95"),
                target_price=candle.close * Decimal("1.10"),
                reason="sma-cross-up",
            )
        if fast_prev >= slow_prev and fast_now < slow_now:
            return Signal(Action.EXIT_LONG, reason="sma-cross-down")
        return None


class BuyAndHold(Strategy):
    """Enter on the first candle and hold until the stop, target or data end."""

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> Signal | None:
        if len(history) != 1:
            return None
        return Signal(
            Action.ENTER_LONG,
            stop_price=candle.close * Decimal("0.90"),
            target_price=candle.close * Decimal("1.50"),
            reason="buy-and-hold",
        )


class AlwaysFlat(Strategy):
    """A useful control: never trades."""

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> Signal | None:
        return None
