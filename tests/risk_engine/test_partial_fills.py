"""Risk-engine v1.1: partial fills.

An OPEN fill may cover only part of the approved quantity. The approval is
consumed only when the pieces add up to the approved quantity, and the pieces
accumulate into one position. A CLOSE fill may likewise be partial.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from decimal import Decimal

import pytest

from nomicronix.risk_engine.models import Fill, FillAction, RiskLimits
from nomicronix.risk_engine.service import RiskService
from nomicronix.risk_engine.store import ApprovalError, RiskStateStore
from tests.risk_engine.fakes import NOW_MS, make_instrument, make_proposal


@contextmanager
def _sqlite(path):
    conn = sqlite3.connect(str(path))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def make_service(path: str) -> tuple[RiskStateStore, RiskService]:
    store = RiskStateStore(path)
    service = RiskService(
        store,
        RiskLimits(),
        initial_cash=Decimal("1000"),
        clock=lambda: NOW_MS,
    )
    return store, service


def open_fill(
    *,
    price: str = "100",
    quantity: str = "1",
    fee: str = "0",
    symbol: str = "BTC/USDT",
) -> Fill:
    return Fill(
        FillAction.OPEN,
        symbol,
        Decimal(quantity),
        Decimal(price),
        Decimal(fee),
        NOW_MS,
    )


def close_fill(*, price: str = "110", quantity: str = "1", fee: str = "0") -> Fill:
    return Fill(
        FillAction.CLOSE,
        "BTC/USDT",
        Decimal(quantity),
        Decimal(price),
        Decimal(fee),
        NOW_MS + 1,
    )


def approve(service: RiskService) -> None:
    decision = service.check(make_proposal(), make_instrument())
    assert decision.approved
    assert decision.approved_quantity == Decimal("1")


def test_a_partial_open_creates_one_position_and_keeps_the_approval_open(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        approve(service)
        state = service.record_fill("sig-1", open_fill(quantity="0.4", fee="0.1"))

        approval = store.load_approval("sig-1")
        assert approval is not None
        assert approval.filled_quantity == Decimal("0.4")
        assert approval.consumed_at_ms is None

        position = state.positions["BTC/USDT"]
        assert position.quantity == Decimal("0.4")
        assert position.entry_price == Decimal("100")
        assert position.entry_fee == Decimal("0.1")
        assert state.cash == Decimal("959.9")
    finally:
        store.close()


def test_a_second_partial_accumulates_one_position_at_a_weighted_average(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        approve(service)
        service.record_fill("sig-1", open_fill(quantity="0.4", fee="0.1"))
        state = service.record_fill("sig-1", open_fill(price="110", quantity="0.3", fee="0.2"))

        approval = store.load_approval("sig-1")
        assert approval is not None
        assert approval.filled_quantity == Decimal("0.7")
        assert approval.consumed_at_ms is None

        position = state.positions["BTC/USDT"]
        assert position.quantity == Decimal("0.7")
        assert position.entry_price == Decimal("73") / Decimal("0.7")
        assert position.entry_fee == Decimal("0.3")
        assert state.cash == Decimal("926.7")
    finally:
        store.close()


def test_the_final_piece_consumes_the_approval_exactly_once(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        approve(service)
        service.record_fill("sig-1", open_fill(quantity="0.4", fee="0.1"))
        state = service.record_fill("sig-1", open_fill(price="105", quantity="0.6", fee="0.05"))

        approval = store.load_approval("sig-1")
        assert approval is not None
        assert approval.filled_quantity == Decimal("1")
        assert approval.consumed_at_ms is not None
        assert state.positions["BTC/USDT"].quantity == Decimal("1")

        with pytest.raises(ApprovalError):
            service.record_fill("sig-1", open_fill(quantity="0.1"))
    finally:
        store.close()


def test_an_overfill_is_rejected_without_changing_state(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        approve(service)
        service.record_fill("sig-1", open_fill(quantity="0.4", fee="0.1"))

        with pytest.raises(ApprovalError):
            service.record_fill("sig-1", open_fill(quantity="0.7"))

        approval = store.load_approval("sig-1")
        assert approval is not None
        assert approval.filled_quantity == Decimal("0.4")
        status = service.status()
        assert status.exposure == Decimal("40")
        assert status.open_positions == 1
    finally:
        store.close()


@pytest.mark.parametrize("quantity", ["0", "-0.1"])
def test_a_non_positive_fill_quantity_is_rejected(tmp_path, quantity: str) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        approve(service)
        with pytest.raises(ApprovalError):
            service.record_fill("sig-1", open_fill(quantity=quantity))
        assert service.status().open_positions == 0
    finally:
        store.close()


@pytest.mark.parametrize(("price", "fee"), [("0", "0"), ("-1", "0"), ("100", "-0.1")])
def test_a_fill_with_an_invalid_price_or_fee_is_rejected(tmp_path, price: str, fee: str) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        approve(service)
        with pytest.raises(ApprovalError):
            service.record_fill("sig-1", open_fill(price=price, fee=fee))
        assert service.status().open_positions == 0
    finally:
        store.close()


def test_persisting_an_open_fill_without_an_approval_is_rejected(tmp_path) -> None:
    store, _ = make_service(str(tmp_path / "risk.sqlite"))
    try:
        state = store.load_state(NOW_MS, Decimal("1000"))
        with pytest.raises(ApprovalError):
            store.persist_fill(state, "sig-1", open_fill(quantity="0.4"), NOW_MS)
    finally:
        store.close()


def test_a_stale_approval_snapshot_cannot_be_applied_twice(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        approve(service)
        approval = store.load_approval("sig-1")
        assert approval is not None
        state = store.load_state(NOW_MS, Decimal("1000"))

        store.persist_fill(state, "sig-1", open_fill(quantity="0.4"), NOW_MS, approval=approval)
        with pytest.raises(ApprovalError):
            store.persist_fill(state, "sig-1", open_fill(quantity="0.4"), NOW_MS, approval=approval)
    finally:
        store.close()


def test_a_partial_fill_survives_a_restart(tmp_path) -> None:
    path = str(tmp_path / "risk.sqlite")
    store, service = make_service(path)
    try:
        approve(service)
        service.record_fill("sig-1", open_fill(quantity="0.4", fee="0.1"))
    finally:
        store.close()

    store2, service2 = make_service(path)
    try:
        approval = store2.load_approval("sig-1")
        assert approval is not None
        assert approval.filled_quantity == Decimal("0.4")
        assert approval.consumed_at_ms is None

        state = service2.record_fill("sig-1", open_fill(quantity="0.6"))
        assert state.positions["BTC/USDT"].quantity == Decimal("1")
        consumed = store2.load_approval("sig-1")
        assert consumed is not None
        assert consumed.consumed_at_ms is not None
    finally:
        store2.close()


def test_a_fill_for_another_symbol_cannot_consume_the_approval(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        approve(service)
        with pytest.raises(ApprovalError):
            service.record_fill(
                "sig-1",
                open_fill(quantity="0.4", symbol="ETH/USDT"),
            )
        approval = store.load_approval("sig-1")
        assert approval is not None
        assert approval.filled_quantity == Decimal("0")
    finally:
        store.close()


def test_a_partial_close_reduces_the_position_and_realizes_proportionally(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        approve(service)
        service.record_fill("sig-1", open_fill(quantity="1", fee="0.2"))

        state = service.record_fill("sig-1", close_fill(quantity="0.4", fee="0.04"))

        position = state.positions["BTC/USDT"]
        assert position.quantity == Decimal("0.6")
        assert position.entry_fee == Decimal("0.12")
        assert state.realized_pnl_today == Decimal("3.88")
        assert state.cash == Decimal("943.76")

        state = service.record_fill("sig-1", close_fill(price="105", quantity="0.6", fee="0.06"))
        assert state.positions == {}
        assert state.realized_pnl_today == Decimal("6.70")
        assert state.cash == Decimal("1006.70")
    finally:
        store.close()


def test_a_close_larger_than_the_position_is_rejected(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        approve(service)
        service.record_fill("sig-1", open_fill(quantity="1"))
        with pytest.raises(ValueError):
            service.record_fill("sig-1", close_fill(quantity="1.1"))
        assert service.status().open_positions == 1
    finally:
        store.close()


def test_expiring_a_canceled_remainder_blocks_later_fills(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        approve(service)
        service.record_fill("sig-1", open_fill(quantity="0.4"))

        service.expire_approval("sig-1", NOW_MS + 1)

        approval = store.load_approval("sig-1")
        assert approval is not None
        assert approval.filled_quantity == Decimal("0.4")
        assert approval.consumed_at_ms == NOW_MS + 1

        with pytest.raises(ApprovalError):
            service.record_fill("sig-1", open_fill(quantity="0.6"))
        with pytest.raises(ApprovalError):
            service.expire_approval("sig-1", NOW_MS + 2)
    finally:
        store.close()


def test_expiring_an_unknown_approval_is_rejected(tmp_path) -> None:
    store, service = make_service(str(tmp_path / "risk.sqlite"))
    try:
        with pytest.raises(ApprovalError):
            service.expire_approval("missing", NOW_MS)
    finally:
        store.close()


def test_an_existing_database_gains_the_filled_quantity_column(tmp_path) -> None:
    path = tmp_path / "old.sqlite"
    with _sqlite(path) as conn:
        conn.execute(
            """
            CREATE TABLE approvals (
                signal_id      TEXT PRIMARY KEY,
                symbol         TEXT NOT NULL,
                quantity       TEXT NOT NULL,
                notional       TEXT NOT NULL,
                stop_price     TEXT NOT NULL,
                target_price   TEXT NOT NULL,
                created_at_ms  INTEGER NOT NULL,
                consumed_at_ms INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            ("sig-old", "BTC/USDT", "1", "100", "95", "115", NOW_MS),
        )

    store = RiskStateStore(str(path))
    try:
        approval = store.load_approval("sig-old")
        assert approval is not None
        assert approval.filled_quantity == Decimal("0")
        assert approval.consumed_at_ms is None
    finally:
        store.close()
