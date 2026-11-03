"""Deterministic candles and strategies for backtesting tests."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from nomicronix.backtesting.models import Action, BacktestConfig, Signal
from nomicronix.backtesting.strategy import Strategy
from nomicronix.market_data.models import Candle
from nomicronix.market_data.store import SqliteStore

JAN_1_2025_MS = 1_735_689_600_000
HOUR_MS = 3_600_000


def make_candle(
    open_time_ms: int,
    *,
    open_price: str = "100",
    high: str | None = None,
    low: str | None = None,
    close: str | None = None,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
) -> Candle:
    open_value = Decimal(open_price)
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time_ms=open_time_ms,
        open=open_value,
        high=Decimal(high) if high is not None else open_value + Decimal("2"),
        low=Decimal(low) if low is not None else open_value - Decimal("2"),
        close=Decimal(close) if close is not None else open_value + Decimal("1"),
        volume=Decimal("10"),
    )


def make_candles(
    count: int,
    *,
    start_ms: int = JAN_1_2025_MS,
    step_ms: int = HOUR_MS,
    start_price: str = "100",
    price_step: str = "1",
) -> list[Candle]:
    step = Decimal(price_step)
    base = Decimal(start_price)
    return [
        make_candle(
            start_ms + index * step_ms,
            open_price=str(base + index * step),
            close=str(base + index * step + step / 2),
        )
        for index in range(count)
    ]


def make_store(candles: list[Candle], exchange: str = "test") -> SqliteStore:
    store = SqliteStore(":memory:", exchange)
    store.upsert_candles(candles)
    _OPEN_STORES.append(store)
    return store


def close_stores() -> None:
    """Close any stores opened by `make_store` at the end of a test."""
    while _OPEN_STORES:
        _OPEN_STORES.pop().close()


_OPEN_STORES: list[SqliteStore] = []


def make_config(**overrides: object) -> BacktestConfig:
    defaults: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "start_ms": JAN_1_2025_MS,
        "end_ms": JAN_1_2025_MS + 1_000 * HOUR_MS,
        "initial_cash": Decimal("1000"),
        "fee_bps": Decimal("0"),
        "slippage_bps": Decimal("0"),
        "position_fraction": Decimal("0.25"),
        "min_notional": Decimal("10"),
        "quantity_step": Decimal("1"),
        "min_candles": 1,
        "warmup_candles": 0,
    }
    defaults.update(overrides)
    return BacktestConfig(**defaults)  # type: ignore[arg-type]


class ScriptedStrategy:
    """Return a signal on selected history lengths; use the index as the key."""

    def __init__(self, signals: dict[int, Signal]) -> None:
        self.signals = signals
        self.history_lengths: list[int] = []
        self.seen_times: list[int] = []

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> Signal | None:
        index = len(history) - 1
        self.history_lengths.append(len(history))
        self.seen_times.append(history[-1].open_time_ms)
        return self.signals.get(index)


class SpyStrategy:
    """Assert the history window is causal."""

    def __init__(self) -> None:
        self.violations: list[str] = []

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> Signal | None:
        if history[-1] != candle:
            self.violations.append("history does not end at the current candle")
        if any(item.open_time_ms > candle.open_time_ms for item in history):
            self.violations.append("history contains a future candle")
        return None


class EntryOnce(Strategy):
    def __init__(self, stop_pct: float = 0.10, target_pct: float = 0.20) -> None:
        self.stop_pct = Decimal(str(stop_pct))
        self.target_pct = Decimal(str(target_pct))
        self.called = 0

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> Signal | None:
        self.called += 1
        if self.called != 1:
            return None
        return Signal(
            Action.ENTER_LONG,
            stop_price=candle.close * (1 - self.stop_pct),
            target_price=candle.close * (1 + self.target_pct),
            reason="entry-once",
        )


class AlwaysEntry(Strategy):
    def __init__(self, stop_pct: float = 0.05, target_pct: float = 0.05) -> None:
        self.stop_pct = Decimal(str(stop_pct))
        self.target_pct = Decimal(str(target_pct))

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> Signal | None:
        return Signal(
            Action.ENTER_LONG,
            stop_price=candle.close * (1 - self.stop_pct),
            target_price=candle.close * (1 + self.target_pct),
            reason="always-entry",
        )


class BiasEntry(Strategy):
    """Enter once when `bias` is truthy; stay flat when it is falsy.

    Used to prove walk-forward parameter selection only keeps params that
    actually produced trades in the training window.
    """

    def __init__(self, bias: int = 1, stop_pct: float = 0.10) -> None:
        self.bias = int(bias)
        self.stop_pct = Decimal(str(stop_pct))

    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> Signal | None:
        if not self.bias or len(history) != 1:
            return None
        return Signal(
            Action.ENTER_LONG,
            stop_price=candle.close * (1 - self.stop_pct),
            target_price=candle.close * Decimal("1.20"),
            reason="bias-entry",
        )
