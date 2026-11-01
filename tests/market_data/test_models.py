from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from nomicronix.market_data.models import Candle, Timeframe


def make_candle() -> Candle:
    return Candle(
        symbol="BTC/USDT",
        timeframe="1h",
        open_time_ms=1_735_689_600_000,
        open=Decimal("93500.10"),
        high=Decimal("93800.00"),
        low=Decimal("93400.55"),
        close=Decimal("93750.25"),
        volume=Decimal("812.345"),
    )


def test_candle_keeps_decimal_values_exactly() -> None:
    candle = make_candle()

    assert candle.open == Decimal("93500.10")
    assert candle.volume == Decimal("812.345")


def test_candle_is_immutable() -> None:
    candle = make_candle()

    with pytest.raises(FrozenInstanceError):
        candle.close = Decimal("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    "value, expected_ms",
    [
        ("15m", 900_000),
        ("1h", 3_600_000),
        ("4h", 14_400_000),
        ("1d", 86_400_000),
    ],
)
def test_timeframe_converts_to_milliseconds(value: str, expected_ms: int) -> None:
    assert Timeframe(value).ms == expected_ms


def test_timeframe_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        Timeframe("2h")


def test_timeframe_equals_its_plain_string() -> None:
    assert Timeframe.H1 == "1h"
