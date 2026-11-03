from decimal import Decimal

import pytest

from nomicronix.strategy_engine.indicators import atr, highest_high, lowest_low, sma, true_range
from tests.strategy_engine.fakes import candle


def test_sma_uses_only_the_requested_window() -> None:
    values = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("10")]
    assert sma(values, 3) == Decimal("5")
    assert sma(values, 1) == Decimal("10")


def test_sma_returns_none_when_history_is_short() -> None:
    assert sma([Decimal("1")], 2) is None


def test_sma_rejects_bad_period() -> None:
    with pytest.raises(ValueError):
        sma([Decimal("1")], 0)


def test_true_range_uses_the_gap_when_it_is_largest() -> None:
    previous_close = Decimal("100")
    current = candle(0, "108", high="110", low="106")
    assert true_range(previous_close, current) == Decimal("10")


def test_true_range_falls_back_to_high_minus_low() -> None:
    previous_close = Decimal("100")
    current = candle(0, "100", high="102", low="98")
    assert true_range(previous_close, current) == Decimal("4")


def test_atr_wilder_smoothing_fixture() -> None:
    candles = [
        candle(0, "100", high="102", low="98"),
        candle(1, "100", high="102", low="98"),
        candle(2, "100", high="102", low="98"),
        candle(3, "100", high="102", low="98"),
        candle(4, "100", high="106", low="98"),
    ]

    assert atr(candles, 3) == (Decimal("4") * 2 + Decimal("8")) / Decimal("3")


def test_atr_returns_none_when_history_is_short() -> None:
    assert atr([candle(0, "100")], 3) is None
    assert atr([], 3) is None


def test_atr_rejects_bad_period() -> None:
    with pytest.raises(ValueError):
        atr([candle(0, "100")], 0)


def test_channels_use_only_the_last_period() -> None:
    candles = [
        candle(0, "10", high="11", low="9"),
        candle(1, "20", high="21", low="19"),
        candle(2, "5", high="6", low="4"),
    ]
    assert highest_high(candles, 2) == Decimal("21")
    assert lowest_low(candles, 2) == Decimal("4")


def test_channels_return_none_when_history_is_short() -> None:
    assert highest_high([candle(0, "10")], 2) is None
    assert lowest_low([candle(0, "10")], 2) is None


def test_channels_reject_bad_period() -> None:
    with pytest.raises(ValueError):
        highest_high([candle(0, "10")], 0)
    with pytest.raises(ValueError):
        lowest_low([candle(0, "10")], 0)
