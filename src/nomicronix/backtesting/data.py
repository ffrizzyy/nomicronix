"""Load and validate candles from the market-data store."""

from __future__ import annotations

from nomicronix.backtesting.models import BacktestConfig
from nomicronix.market_data.models import Candle, Timeframe
from nomicronix.market_data.store import MarketDataStore


class DataError(ValueError):
    """Base class for a requested backtest range that cannot be trusted."""


class DataGapError(DataError):
    def __init__(self, gaps: list[tuple[int, int]]) -> None:
        self.gaps = tuple(gaps)
        super().__init__(f"candle gaps in requested range: {self.gaps}")


class InsufficientDataError(DataError):
    pass


def load_candles(store: MarketDataStore, config: BacktestConfig) -> list[Candle]:
    """Load the requested range plus warm-up, rejecting gaps and short series."""
    step_ms = Timeframe(config.timeframe).ms
    warmup_start = config.start_ms - config.warmup_candles * step_ms
    candles = store.get_candles(
        config.symbol,
        config.timeframe,
        warmup_start,
        config.end_ms,
    )
    gaps = store.find_gaps(
        config.symbol,
        config.timeframe,
        warmup_start,
        config.end_ms,
    )
    if gaps:
        raise DataGapError(list(gaps))
    if len(candles) < config.min_candles:
        raise InsufficientDataError(
            f"need at least {config.min_candles} candles, got {len(candles)}"
        )
    for previous, current in zip(candles, candles[1:], strict=False):
        if current.open_time_ms <= previous.open_time_ms:
            raise DataError("candles must be strictly ascending and de-duplicated")
    return candles
