"""Load and validate a gap-free scan range from the market-data store."""

from __future__ import annotations

from nomicronix.market_data.models import Candle, Timeframe
from nomicronix.market_data.store import MarketDataStore
from nomicronix.strategy_engine.models import ScanConfig


class DataError(ValueError):
    """Base class for a requested scan range that cannot be trusted."""


class DataGapError(DataError):
    def __init__(self, gaps: list[tuple[int, int]]) -> None:
        self.gaps = tuple(gaps)
        super().__init__(f"candle gaps in requested range: {self.gaps}")


class InsufficientDataError(DataError):
    pass


def load_candles(
    store: MarketDataStore,
    config: ScanConfig,
    *,
    warmup_candles: int | None = None,
) -> list[Candle]:
    """Load the requested range plus warm-up, rejecting gaps and short series.

    Gaps are checked only inside `[start_ms, end_ms)`: the warm-up prefix is
    best-effort because an exchange may not have listed the symbol early enough.
    The scan engine itself waits until enough history is visible.
    """
    step_ms = Timeframe(config.timeframe).ms
    warmup = config.min_history if warmup_candles is None else warmup_candles
    if warmup < 0:
        raise ValueError("warmup_candles must not be negative")
    warmup_start = config.start_ms - warmup * step_ms

    candles = store.get_candles(config.symbol, config.timeframe, warmup_start, config.end_ms)
    gaps = store.find_gaps(config.symbol, config.timeframe, config.start_ms, config.end_ms)
    if gaps:
        raise DataGapError(list(gaps))

    if len(candles) < config.min_history:
        raise InsufficientDataError(
            f"need at least {config.min_history} candles, got {len(candles)}"
        )

    for previous, current in zip(candles, candles[1:], strict=False):
        if current.open_time_ms <= previous.open_time_ms:
            raise DataError("candles must be strictly ascending and de-duplicated")
    return candles
