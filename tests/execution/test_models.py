from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from nomicronix.execution.models import (
    EventType,
    FillReport,
    OrderEvent,
    OrderStatus,
    Reconciliation,
    Ticker,
    intent_id_for,
)
from tests.execution.fakes import NOW_MS, make_intent, make_order


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signal_id", ""),
        ("intent_id", "nx-other"),
        ("symbol", ""),
        ("side", "sell"),
        ("quantity", Decimal("-1")),
        ("quantity", Decimal("0")),
        ("entry_price", Decimal("0")),
        ("stop_price", Decimal("0")),
        ("target_price", Decimal("0")),
        ("stop_price", Decimal("100")),
        ("target_price", Decimal("100")),
        ("approved_notional", Decimal("-1")),
        ("created_at_ms", 0),
        ("expires_at_ms", NOW_MS),
        ("filled_quantity", Decimal("2")),
        ("filled_quantity", Decimal("1")),
    ],
)
def test_invalid_intents_are_refused(field: str, value: object) -> None:
    intent = make_intent()
    with pytest.raises(ValueError):
        replace(intent, **{field: value})


def test_a_zero_quantity_intent_is_only_valid_when_rejected() -> None:
    from nomicronix.execution.models import IntentState

    rejected = make_intent(state=IntentState.REJECTED, quantity="0")
    assert rejected.quantity == 0
    assert rejected.terminal is True


def test_an_intent_requires_an_average_price_after_a_fill() -> None:
    with pytest.raises(ValueError, match="average fill price"):
        make_intent(filled_quantity="0.5")
    filled = make_intent(filled_quantity="0.5", average_fill_price="100")
    assert filled.remaining_quantity == Decimal("0.5")


def test_intent_id_is_a_valid_deterministic_client_order_id() -> None:
    first = intent_id_for("sig-1")
    assert first == intent_id_for("sig-1")
    assert first.startswith("nx-")
    assert len(first) <= 36
    assert first != intent_id_for("sig-2")
    assert set(first) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
    long_signal = "sma-cross:BTC/USDT:1h:1735776000000"
    assert len(intent_id_for(long_signal)) <= 36


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_order_id", ""),
        ("exchange_order_id", ""),
        ("symbol", ""),
        ("quantity", Decimal("0")),
        ("filled_quantity", Decimal("2")),
        ("average_price", None),
        ("fee", Decimal("-1")),
        ("updated_at_ms", 0),
    ],
)
def test_invalid_orders_are_refused(field: str, value: object) -> None:
    order = make_order(status=OrderStatus.CLOSED, filled_quantity="1", average_price="100")
    with pytest.raises(ValueError):
        replace(order, **{field: value})


def test_an_unfilled_order_must_not_carry_an_average_price() -> None:
    with pytest.raises(ValueError, match="must not carry"):
        make_order(status=OrderStatus.OPEN, average_price="100")


@pytest.mark.parametrize(
    ("bid", "ask", "last"),
    [
        ("0", "1", "1"),
        ("1", "0", "1"),
        ("1", "1", "0"),
        ("2", "1", "1"),
    ],
)
def test_invalid_tickers_are_refused(bid: str, ask: str, last: str) -> None:
    with pytest.raises(ValueError):
        Ticker(
            symbol="BTC/USDT",
            bid=Decimal(bid),
            ask=Decimal(ask),
            last=Decimal(last),
            at_ms=NOW_MS,
        )


def test_an_empty_ticker_symbol_is_refused() -> None:
    with pytest.raises(ValueError):
        Ticker(symbol="", bid=Decimal("1"), ask=Decimal("1"), last=Decimal("1"), at_ms=NOW_MS)


@pytest.mark.parametrize(
    ("filled", "average", "fee"),
    [
        ("-1", None, "0"),
        ("1", None, "0"),
        ("0", "100", "0"),
        ("1", "100", "-1"),
    ],
)
def test_invalid_fill_reports_are_refused(filled: str, average: str | None, fee: str) -> None:
    with pytest.raises(ValueError):
        FillReport(
            intent_id="nx-sig-1",
            filled_quantity=Decimal(filled),
            average_price=None if average is None else Decimal(average),
            fee=Decimal(fee),
            complete=False,
        )


def test_order_events_need_an_intent_and_a_time() -> None:
    with pytest.raises(ValueError):
        OrderEvent("", EventType.FILLED, NOW_MS)
    with pytest.raises(ValueError):
        OrderEvent("nx-sig-1", EventType.FILLED, 0)


def test_reconciliation_clean_means_no_mismatches() -> None:
    assert Reconciliation().clean is True
    assert Reconciliation(matched=("nx-a",)).clean is True
    assert Reconciliation(unknown_orders=("nx-a",)).clean is False
    assert Reconciliation(missing_orders=("nx-a",)).clean is False
    assert Reconciliation(unknown_positions=("BTC/USDT",)).clean is False


def test_exchange_error_codes_have_stable_exit_codes() -> None:
    from nomicronix.execution.models import (
        ConfigError,
        CredentialError,
        ExchangeError,
        IntentError,
        LiveDisabled,
        ReconciliationError,
        RiskRejected,
        StoreError,
    )

    assert ConfigError("x").exit_code == 1
    assert CredentialError("x").exit_code == 1
    assert StoreError("x").exit_code == 1
    assert IntentError("x").exit_code == 1
    assert RiskRejected("x").exit_code == 2
    assert ExchangeError("x").exit_code == 3
    assert ReconciliationError("x").exit_code == 3
    assert LiveDisabled("x").exit_code == 4
    assert ExchangeError("x", code="custom", exit_code=9).code == "custom"
