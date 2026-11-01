"""Test doubles and builders for market-data tests. Nothing here touches the network."""

from decimal import Decimal

from nomicronix.market_data.models import Candle, Timeframe

JAN_1_2025_MS = 1_735_689_600_000  # 2025-01-01T00:00:00Z, aligned to every supported timeframe


def make_candle(
    open_time_ms: int,
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    price: str = "100",
) -> Candle:
    base = Decimal(price)
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time_ms=open_time_ms,
        open=base,
        high=base + Decimal("2"),
        low=base - Decimal("2"),
        close=base + Decimal("1"),
        volume=Decimal("10.5"),
    )


def make_candles(
    start_ms: int,
    count: int,
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
) -> list[Candle]:
    """`count` contiguous candles starting at `start_ms`, prices rising by 1 per candle."""
    step_ms = Timeframe(timeframe).ms
    return [
        make_candle(
            start_ms + i * step_ms,
            symbol=symbol,
            timeframe=timeframe,
            price=str(100 + i),
        )
        for i in range(count)
    ]


class FakeExchangeAdapter:
    """Deterministic in-memory exchange. Script failures with `fail_next`."""

    name = "fake"

    def __init__(self, candles: list[Candle], *, duplicate_first_row: bool = False) -> None:
        self._candles = list(candles)
        self._duplicate_first_row = duplicate_first_row
        self._failures: list[Exception] = []
        self.calls: list[tuple[str, str, int, int]] = []

    def fail_next(self, error: Exception, times: int = 1) -> None:
        """The next `times` calls raise `error` before any later call succeeds."""
        self._failures.extend([error] * times)

    def fetch_candles(
        self, symbol: str, timeframe: str, since_ms: int, limit: int
    ) -> list[Candle]:
        self.calls.append((symbol, timeframe, since_ms, limit))
        if self._failures:
            raise self._failures.pop(0)
        rows = sorted(
            (
                c
                for c in self._candles
                if c.symbol == symbol and c.timeframe == timeframe and c.open_time_ms >= since_ms
            ),
            key=lambda c: c.open_time_ms,
        )[:limit]
        if self._duplicate_first_row and rows:
            rows = [rows[0], *rows]
        return rows
