from dataclasses import replace
from decimal import Decimal

import pytest

from nomicronix.signal_delivery.models import (
    InstrumentRules,
    Notification,
    PaperTrade,
)
from tests.signal_delivery.fakes import make_notification


def test_instrument_rules_build_a_risk_instrument() -> None:
    rules = InstrumentRules(
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.01"),
        min_notional=Decimal("20"),
    )
    instrument = rules.for_symbol("BTC/USDT")
    assert instrument.symbol == "BTC/USDT"
    assert instrument.quantity_step == Decimal("0.001")
    assert instrument.min_quantity == Decimal("0.01")
    assert instrument.min_notional == Decimal("20")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"quantity_step": Decimal("0")}, "quantity_step"),
        ({"quantity_step": Decimal("-1")}, "quantity_step"),
        ({"min_quantity": Decimal("-1")}, "min_quantity"),
        ({"min_notional": Decimal("-1")}, "min_notional"),
    ],
)
def test_instrument_rules_reject_invalid_values(kwargs: dict[str, Decimal], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        InstrumentRules(**kwargs)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("strategy_id", " ", "strategy_id"),
        ("symbol", "", "symbol"),
        ("created_at_ms", 0, "created_at_ms"),
        ("risk_amount", Decimal("-1"), "risk figures"),
    ],
)
def test_notification_field_validation(field: str, value: object, message: str) -> None:
    notification = make_notification()
    with pytest.raises(ValueError, match=message):
        Notification(**{**_fields(notification), field: value})


def test_approved_notification_cannot_carry_reasons() -> None:
    with pytest.raises(ValueError, match="rejection reasons"):
        replace(make_notification(), reasons=("stale_signal",))


def test_notification_rejects_unknown_timeframe_and_side() -> None:
    notification = make_notification()
    with pytest.raises(ValueError):
        Notification(**{**_fields(notification), "timeframe": "7m"})
    with pytest.raises(ValueError):
        Notification(**{**_fields(notification), "side": "sell"})


def test_notification_status_text_follows_approval() -> None:
    assert make_notification().status_text == "approved"
    assert make_notification(approved=False).status_text == "rejected"


def test_paper_trade_validates_identity_and_levels() -> None:
    trade = _trade()
    with pytest.raises(ValueError, match="signal id and symbol"):
        PaperTrade(**{**_trade_fields(trade), "symbol": " "})
    with pytest.raises(ValueError, match="stop < entry < target"):
        PaperTrade(**{**_trade_fields(trade), "stop_price": trade.entry_price})
    with pytest.raises(ValueError, match="opened_at_ms"):
        PaperTrade(**{**_trade_fields(trade), "opened_at_ms": 0})
    with pytest.raises(ValueError, match="quantity"):
        PaperTrade(**{**_trade_fields(trade), "quantity": Decimal("0")})


def test_paper_trade_is_either_open_or_closed() -> None:
    trade = _trade()
    with pytest.raises(ValueError, match="fully open or fully closed"):
        replace(trade, exit_price=Decimal("115"))
    with pytest.raises(ValueError, match="exit_reason"):
        replace(
            trade,
            closed_at_ms=trade.opened_at_ms + 60_000,
            exit_price=Decimal("115"),
            exit_reason="trailing",
            realized_pnl=Decimal("15"),
        )
    assert trade.is_open
    assert trade.pnl_at(Decimal("110")) == Decimal("10")


def _fields(notification: Notification) -> dict[str, object]:
    return {field: getattr(notification, field) for field in notification.__dataclass_fields__}


def _trade() -> PaperTrade:
    return PaperTrade(
        signal_id="sma-cross:BTC/USDT:1h:1",
        symbol="BTC/USDT",
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        target_price=Decimal("115"),
        opened_at_ms=1,
    )


def _trade_fields(trade: PaperTrade) -> dict[str, object]:
    return {field: getattr(trade, field) for field in trade.__dataclass_fields__}
