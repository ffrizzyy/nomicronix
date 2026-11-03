import pytest

from nomicronix.market_data.models import Candle
from nomicronix.strategy_engine.proposers import DonchianBreakout, SmaCross
from nomicronix.strategy_engine.views import CandleView
from tests.strategy_engine.fakes import make_candles, series_from_closes


def view(candles: list[Candle], end: int | None = None) -> CandleView:
    return CandleView(candles, len(candles) if end is None else end)


def test_sma_cross_fires_on_the_crossing_candle_only() -> None:
    candles = series_from_closes(["10", "9", "8", "7", "10"])
    proposer = SmaCross(fast=2, slow=3, atr_period=2)

    assert proposer.propose(view(candles, 4)) is None
    idea = proposer.propose(view(candles))
    assert idea is not None
    assert idea.entry_price == 10
    assert idea.stop_price == 4
    assert idea.target_price == 28
    assert idea.features["fast_sma"] == "8.5"
    assert idea.features["atr"] == "3"


def test_sma_cross_does_not_repeat_while_the_spread_persists() -> None:
    candles = series_from_closes(["10", "9", "8", "7", "10", "11"])
    proposer = SmaCross(fast=2, slow=3, atr_period=2)

    assert proposer.propose(view(candles)) is None


def test_sma_cross_returns_none_without_enough_history() -> None:
    proposer = SmaCross(fast=2, slow=3, atr_period=2)
    assert proposer.propose(view(make_candles(3))) is None


def test_sma_cross_returns_none_when_the_stop_would_not_be_positive() -> None:
    candles = series_from_closes(["10", "9", "8", "7", "10"])
    proposer = SmaCross(fast=2, slow=3, atr_period=2, stop_atr=100.0)
    assert proposer.propose(view(candles)) is None


def test_sma_cross_min_history_covers_slow_and_atr() -> None:
    assert SmaCross(fast=2, slow=3, atr_period=9).min_history == 10


@pytest.mark.parametrize(
    "params",
    [
        {"fast": 0},
        {"fast": 5, "slow": 5},
        {"fast": 6, "slow": 5},
        {"atr_period": 0},
        {"stop_atr": 0.0},
        {"target_rr": 0.0},
        {"confidence": -0.1},
        {"confidence": 1.1},
    ],
)
def test_sma_cross_rejects_bad_params(params: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SmaCross(**params)  # type: ignore[arg-type]


def test_donchian_fires_on_a_fresh_breakout_only() -> None:
    candles = series_from_closes(
        ["10", "10", "10", "10", "15", "17"],
    )
    proposer = DonchianBreakout(entry_period=3, atr_period=2)

    assert proposer.propose(view(candles, 3)) is None
    idea = proposer.propose(view(candles, 5))
    assert idea is not None
    assert idea.entry_price == 15
    assert idea.features["channel_high"] == "11"
    assert idea.stop_price == 7
    assert idea.target_price == 31

    assert proposer.propose(view(candles)) is None


def test_donchian_returns_none_without_enough_history() -> None:
    proposer = DonchianBreakout(entry_period=3, atr_period=2)
    assert proposer.propose(view(make_candles(3))) is None


def test_donchian_returns_none_when_the_stop_would_not_be_positive() -> None:
    candles = series_from_closes(["10", "10", "10", "10", "15"])
    proposer = DonchianBreakout(entry_period=3, atr_period=2, stop_atr=100.0)
    assert proposer.propose(view(candles)) is None


def test_donchian_min_history_covers_channel_and_atr() -> None:
    assert DonchianBreakout(entry_period=5, atr_period=14).min_history == 15


@pytest.mark.parametrize(
    "params",
    [
        {"entry_period": 0},
        {"atr_period": 0},
        {"stop_atr": 0.0},
        {"target_rr": 0.0},
        {"confidence": -0.1},
        {"confidence": 1.1},
    ],
)
def test_donchian_rejects_bad_params(params: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        DonchianBreakout(**params)  # type: ignore[arg-type]
