from decimal import Decimal

import pytest

from nomicronix.backtesting.broker import Broker, apply_slippage
from nomicronix.backtesting.models import Action, ExitReason, Signal, SkipReason
from nomicronix.backtesting.sizing import FixedFractionSizer
from tests.backtesting.fakes import JAN_1_2025_MS, make_candle, make_config


def entry() -> Signal:
    return Signal(Action.ENTER_LONG, stop_price=Decimal("90"), target_price=Decimal("120"))


def make_broker(**config_overrides: object) -> Broker:
    return Broker(make_config(**config_overrides), FixedFractionSizer(Decimal("0.25")))


def test_open_and_close_accounting() -> None:
    broker = make_broker()
    first = make_candle(JAN_1_2025_MS, open_price="100")
    assert broker.try_open(first, entry())
    assert broker.cash == Decimal("800")
    assert broker.position is not None
    assert broker.position.quantity == Decimal("2")

    second = make_candle(JAN_1_2025_MS + 1, open_price="110", close="110")
    assert broker.try_close(second, second.open, ExitReason.SIGNAL)
    assert broker.cash == Decimal("1020")
    assert broker.trades[0].pnl == Decimal("20")
    assert broker.position is None


def test_fees_and_slippage_are_charged_on_both_sides() -> None:
    broker = make_broker(fee_bps=Decimal("10"), slippage_bps=Decimal("5"))
    first = make_candle(JAN_1_2025_MS, open_price="100")
    broker.try_open(first, entry())
    second = make_candle(JAN_1_2025_MS + 1, open_price="110", close="110")
    broker.try_close(second, second.open, ExitReason.SIGNAL)

    trade = broker.trades[0]
    assert trade.entry_price == Decimal("100.05")
    assert trade.exit_price == Decimal("109.945")
    assert trade.fees == Decimal("0.41999")
    assert trade.pnl == Decimal("19.37001")
    assert broker.cash == Decimal("1019.37001")


def test_gap_through_stop_fills_at_the_open() -> None:
    broker = make_broker()
    broker.try_open(make_candle(JAN_1_2025_MS, open_price="100"), entry())
    crash = make_candle(
        JAN_1_2025_MS + 1,
        open_price="90",
        high="95",
        low="85",
        close="88",
    )
    assert broker.check_stop_target(crash)
    assert broker.trades[0].exit_price == Decimal("90")
    assert broker.trades[0].exit_reason is ExitReason.STOP


def test_gap_through_target_fills_at_the_open() -> None:
    broker = make_broker()
    broker.try_open(make_candle(JAN_1_2025_MS, open_price="100"), entry())
    moon = make_candle(
        JAN_1_2025_MS + 1,
        open_price="130",
        high="135",
        low="125",
        close="131",
    )
    assert broker.check_stop_target(moon)
    assert broker.trades[0].exit_price == Decimal("130")
    assert broker.trades[0].exit_reason is ExitReason.TARGET


def test_stop_wins_when_both_levels_are_touched() -> None:
    broker = make_broker()
    broker.try_open(make_candle(JAN_1_2025_MS, open_price="100"), entry())
    both = make_candle(
        JAN_1_2025_MS + 1,
        open_price="100",
        high="130",
        low="90",
        close="110",
    )
    assert broker.check_stop_target(both)
    assert broker.trades[0].exit_reason is ExitReason.STOP
    assert broker.trades[0].exit_price == Decimal("90")


def test_ignored_entry_when_already_long() -> None:
    broker = make_broker()
    candle = make_candle(JAN_1_2025_MS, open_price="100")
    assert broker.try_open(candle, entry())
    assert not broker.try_open(candle, entry())
    assert any(event.reason == SkipReason.IGNORED_ENTRY.value for event in broker.events)


def test_ignored_exit_when_flat() -> None:
    broker = make_broker()
    candle = make_candle(JAN_1_2025_MS, open_price="100")
    assert not broker.try_close(candle, candle.open, ExitReason.SIGNAL)
    assert any(event.reason == SkipReason.IGNORED_EXIT.value for event in broker.events)


def test_insufficient_notional_is_skipped() -> None:
    broker = make_broker(min_notional=Decimal("1000"))
    candle = make_candle(JAN_1_2025_MS, open_price="100")
    assert not broker.try_open(candle, entry())
    assert any(event.reason == SkipReason.INSUFFICIENT_CASH.value for event in broker.events)


def test_invalid_entry_without_a_stop_is_skipped() -> None:
    broker = make_broker()
    candle = make_candle(JAN_1_2025_MS, open_price="100")
    assert not broker.try_open(candle, Signal(Action.ENTER_LONG))
    assert any(event.reason == SkipReason.INVALID_SIGNAL.value for event in broker.events)


def test_gap_through_stop_at_entry_is_skipped() -> None:
    broker = make_broker()
    candle = make_candle(JAN_1_2025_MS, open_price="80")
    assert not broker.try_open(candle, entry())
    assert any(event.reason == SkipReason.SKIPPED_GAP.value for event in broker.events)


def test_force_close_at_end_of_data() -> None:
    broker = make_broker()
    broker.try_open(make_candle(JAN_1_2025_MS, open_price="100"), entry())
    last = make_candle(JAN_1_2025_MS + 1, open_price="105", close="106")
    broker.force_close(last)
    assert broker.trades[0].exit_reason is ExitReason.END_OF_DATA
    assert broker.trades[0].exit_price == Decimal("106")


def test_equity_point_marks_the_position() -> None:
    broker = make_broker()
    broker.try_open(make_candle(JAN_1_2025_MS, open_price="100"), entry())
    point = broker.equity_point(make_candle(JAN_1_2025_MS + 1, open_price="100", close="110"))
    assert point.equity == Decimal("1020")
    assert point.exposed is True


def test_slippage_helper_direction() -> None:
    assert apply_slippage(Decimal("100"), Decimal("5"), buying=True) == Decimal("100.05")
    assert apply_slippage(Decimal("100"), Decimal("5"), buying=False) == Decimal("99.95")


def test_sizer_validates_fraction() -> None:
    with pytest.raises(ValueError):
        FixedFractionSizer(Decimal("0"))
    with pytest.raises(ValueError):
        FixedFractionSizer(Decimal("1.1"))


def test_target_miss_leaves_position_open() -> None:
    broker = make_broker()
    broker.try_open(make_candle(JAN_1_2025_MS, open_price="100"), entry())
    quiet = make_candle(
        JAN_1_2025_MS + 1,
        open_price="101",
        high="102",
        low="99",
        close="101",
    )
    assert not broker.check_stop_target(quiet)
    assert broker.position is not None
