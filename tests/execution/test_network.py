"""Opt-in read-only contract test. No credentials, no order, never in CI."""

from __future__ import annotations

import pytest

from nomicronix.execution.adapter import build_adapter
from nomicronix.execution.config import ExecutionConfig

pytestmark = pytest.mark.network


def test_binance_public_ticker_is_reachable() -> None:
    exchange = build_adapter(ExecutionConfig(), require_private=False)
    ticker = exchange.fetch_ticker("BTC/USDT")
    assert ticker.symbol == "BTC/USDT"
    assert ticker.bid > 0
    assert ticker.ask >= ticker.bid
    assert ticker.last > 0
    assert exchange.market_type() == "spot"
