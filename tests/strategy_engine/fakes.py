"""Shared fixtures for strategy-engine tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from nomicronix.market_data.models import Candle
from nomicronix.market_data.store import SqliteStore
from nomicronix.strategy_engine.models import ScanConfig, SignalIdea
from nomicronix.strategy_engine.views import CandleView

HOUR_MS = 3_600_000
JAN_1_2025_MS = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)

_OPEN_STORES: list[SqliteStore] = []


def candle(
    open_time_ms: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
    open_price: str | None = None,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
) -> Candle:
    close_value = Decimal(close)
    high_value = Decimal(high) if high is not None else close_value + Decimal("2")
    low_value = Decimal(low) if low is not None else close_value - Decimal("2")
    open_value = Decimal(open_price) if open_price is not None else close_value
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time_ms=open_time_ms,
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=Decimal("10"),
    )


def make_candles(
    count: int,
    *,
    start_ms: int = JAN_1_2025_MS,
    base: str = "100",
) -> list[Candle]:
    """A gently rising series with no SMA cross and no fresh breakout."""
    start = Decimal(base)
    return [
        candle(
            start_ms + index * HOUR_MS,
            str(start + index),
            high=str(start + index + 2),
            low=str(start + index - 2),
        )
        for index in range(count)
    ]


def series_from_closes(
    closes: list[str],
    *,
    start_ms: int = JAN_1_2025_MS,
    spread: str = "1",
    highs: list[str] | None = None,
    lows: list[str] | None = None,
) -> list[Candle]:
    spread_value = Decimal(spread)
    candles: list[Candle] = []
    for index, close in enumerate(closes):
        close_value = Decimal(close)
        high = highs[index] if highs is not None else str(close_value + spread_value)
        low = lows[index] if lows is not None else str(close_value - spread_value)
        candles.append(
            candle(
                start_ms + index * HOUR_MS,
                close,
                high=high,
                low=low,
            )
        )
    return candles


def make_config(
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    start_ms: int = JAN_1_2025_MS,
    end_ms: int | None = None,
    **overrides: object,
) -> ScanConfig:
    values: dict[str, object] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "start_ms": start_ms,
        "end_ms": end_ms if end_ms is not None else start_ms + 20 * HOUR_MS,
        "min_history": 1,
        "min_confidence": 0.0,
        "ttl_ms": 300_000,
        "max_signals": 100,
    }
    values.update(overrides)
    return ScanConfig(**values)  # type: ignore[arg-type]


def valid_idea(confidence: float = 0.6) -> SignalIdea:
    return SignalIdea(
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        target_price=Decimal("115"),
        confidence=confidence,
        rationale="test entry",
        features={"atr": "2.5"},
    )


@dataclass
class FixedProposer:
    """Return the same valid idea on every candle after `min_history`."""

    strategy_id: str = "fixed"
    min_history: int = 1
    confidence: float = 0.6

    def propose(self, view: CandleView) -> SignalIdea | None:
        return valid_idea(self.confidence)


@dataclass
class AlwaysFlat:
    strategy_id: str = "always-flat"
    min_history: int = 1

    def propose(self, view: CandleView) -> SignalIdea | None:
        return None


class RejectingProposer:
    strategy_id = "rejecting"
    min_history = 1

    def propose(self, view: CandleView) -> SignalIdea | None:
        from nomicronix.strategy_engine.models import ProposalError

        raise ProposalError("bad idea from test")


class ExplodingProposer:
    strategy_id = "exploding"
    min_history = 1

    def propose(self, view: CandleView) -> SignalIdea | None:
        raise RuntimeError("boom")


@dataclass
class SpyProposer:
    """Record every view and prove it never exceeds the decision candle."""

    strategy_id: str = "spy"
    min_history: int = 1
    seen_lengths: list[int] = field(default_factory=list)
    seen_times: list[int] = field(default_factory=list)

    def propose(self, view: CandleView) -> SignalIdea | None:
        self.seen_lengths.append(len(view))
        self.seen_times.append(view.candle.open_time_ms)
        assert view[-1] is view.candle
        return None


@dataclass
class ParamProposer:
    label: str = "default"
    strategy_id: str = "param"
    min_history: int = 1

    def propose(self, view: CandleView) -> SignalIdea | None:
        return None


NOT_A_CLASS = 1


@dataclass
class NoStrategyId:
    min_history: int = 1

    def propose(self, view: CandleView) -> SignalIdea | None:
        return None


@dataclass
class BadMinHistory:
    strategy_id: str = "bad-history"
    min_history: int = 0

    def propose(self, view: CandleView) -> SignalIdea | None:
        return None


@dataclass
class NoPropose:
    strategy_id: str = "no-propose"
    min_history: int = 1


def make_store(candles: list[Candle], exchange: str = "test") -> SqliteStore:
    store = SqliteStore(":memory:", exchange)
    store.upsert_candles(candles)
    _OPEN_STORES.append(store)
    return store


def close_stores() -> None:
    while _OPEN_STORES:
        _OPEN_STORES.pop().close()
