"""Shared market-data types: Candle and Timeframe.

Prices and volumes are Decimal, never float. Times are UTC epoch milliseconds.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Timeframe(StrEnum):
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def ms(self) -> int:
        """Length of one candle of this timeframe, in milliseconds."""
        return _TIMEFRAME_MS[self]


_TIMEFRAME_MS: dict[Timeframe, int] = {
    Timeframe.M15: 15 * 60_000,
    Timeframe.H1: 60 * 60_000,
    Timeframe.H4: 4 * 60 * 60_000,
    Timeframe.D1: 24 * 60 * 60_000,
}


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str  # "BTC/USDT"
    timeframe: str  # "1h"
    open_time_ms: int  # UTC epoch milliseconds, start of the candle
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def close_time_ms(self) -> int:
        """When this candle closes. Raises ValueError for an unsupported timeframe."""
        return self.open_time_ms + Timeframe(self.timeframe).ms


STALE_AFTER_TIMEFRAMES = 2


def is_stale(latest: Candle | None, now_ms: int) -> bool:
    """True if no new candle has arrived for more than 2x the timeframe.

    The age is measured from the close of the latest stored candle. An empty series is
    stale. Consumers should apply this to `store.latest_candle(...)` before trusting prices.
    """
    if latest is None:
        return True
    age_ms = now_ms - latest.close_time_ms
    return age_ms > STALE_AFTER_TIMEFRAMES * Timeframe(latest.timeframe).ms
