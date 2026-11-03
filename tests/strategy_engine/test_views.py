import pytest

from nomicronix.strategy_engine.views import CandleView
from tests.strategy_engine.fakes import make_candles


def test_empty_view_has_no_decision_candle() -> None:
    with pytest.raises(ValueError, match="empty"):
        _ = CandleView(make_candles(3), 0).candle


def test_view_rejects_an_end_outside_the_list() -> None:
    with pytest.raises(ValueError, match="inside"):
        CandleView(make_candles(3), 4)
    with pytest.raises(ValueError, match="inside"):
        CandleView(make_candles(3), -1)


def test_view_indexing_is_bounded_and_negative_indices_work() -> None:
    candles = make_candles(3)
    view = CandleView(candles, 3)

    assert view[-1] is candles[2]
    assert view.candle is candles[2]
    assert view[0] is candles[0]
    assert view[:2] == tuple(candles[:2])
    with pytest.raises(IndexError):
        _ = view[3]
    with pytest.raises(IndexError):
        _ = view[-4]
