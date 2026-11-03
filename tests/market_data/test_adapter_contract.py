"""Contract test against the REAL exchange (public endpoints, no API keys).

Never runs by default. Run manually:  uv run pytest tests/market_data -m network
"""

from decimal import Decimal

import pytest

from nomicronix.market_data.adapter import CcxtAdapter
from tests.market_data.fakes import JAN_1_2025_MS

HOUR_MS = 3_600_000


@pytest.mark.network
def test_binance_returns_well_formed_hourly_candles() -> None:
    adapter = CcxtAdapter("binance")

    candles = adapter.fetch_candles("BTC/USDT", "1h", since_ms=JAN_1_2025_MS, limit=24)

    assert len(candles) == 24
    assert candles[0].open_time_ms == JAN_1_2025_MS
    deltas = [b.open_time_ms - a.open_time_ms for a, b in zip(candles, candles[1:], strict=False)]
    assert deltas == [HOUR_MS] * 23
    for c in candles:
        assert (c.symbol, c.timeframe) == ("BTC/USDT", "1h")
        assert all(isinstance(v, Decimal) for v in (c.open, c.high, c.low, c.close, c.volume))
        assert c.high >= max(c.open, c.close) and c.low <= min(c.open, c.close)
        assert c.volume >= 0
