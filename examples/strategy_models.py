"""Example proposers for the strategy-engine CLI.

The first two are the built-ins re-exported so `module:Class` can point here.
`ThreeBarMomentum` shows how to add a custom, causal proposer.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nomicronix.strategy_engine import (
    CandleView,
    DonchianBreakout,
    SignalIdea,
    SmaCross,
)
from nomicronix.strategy_engine.indicators import atr, sma
from nomicronix.strategy_engine.models import decimal_text

__all__ = ["DonchianBreakout", "SmaCross", "ThreeBarMomentum"]


@dataclass(frozen=True, slots=True)
class ThreeBarMomentum:
    """Long when three rising closes are also above a longer moving average.

    Fires only on the first candle of the run. The stop is `stop_atr` ATRs below
    entry and the target is `target_rr` times the stop distance above entry.
    """

    period: int = 20
    atr_period: int = 14
    stop_atr: float = 2.0
    target_rr: float = 2.0
    confidence: float = 0.5

    strategy_id = "three-bar-momentum"

    @property
    def min_history(self) -> int:
        return max(self.period + 1, self.atr_period + 1)

    def propose(self, view: CandleView) -> SignalIdea | None:
        if len(view) < self.min_history:
            return None

        closes = [candle.close for candle in view]
        if not (closes[-3] < closes[-2] < closes[-1]):
            return None

        previous = closes[:-1]
        if previous[-3] < previous[-2] < previous[-1]:
            return None

        average = sma(closes, self.period)
        atr_value = atr(view, self.atr_period)
        if average is None or atr_value is None or atr_value <= 0:
            return None
        if closes[-1] <= average:
            return None

        entry = closes[-1]
        risk = Decimal(str(self.stop_atr)) * atr_value
        stop = entry - risk
        target = entry + Decimal(str(self.target_rr)) * risk
        if stop <= 0:
            return None

        return SignalIdea(
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            confidence=self.confidence,
            rationale="three rising closes above the moving average",
            features={
                "atr": decimal_text(atr_value),
                "sma": decimal_text(average),
                "close": decimal_text(entry),
            },
        )
