from decimal import Decimal
from pathlib import Path

import pytest

from nomicronix.signal_delivery.models import (
    DeliveryRecord,
    DeliveryStatus,
    DeliveryTotals,
    PaperTrade,
    StoreError,
)
from nomicronix.signal_delivery.render import render_json
from nomicronix.signal_delivery.store import DeliveryStore
from tests.signal_delivery.fakes import make_candidate, make_notification


def make_record(
    status: DeliveryStatus = DeliveryStatus.DELIVERED, *, signal_id: str | None = None
) -> DeliveryRecord:
    notification = make_notification()
    return DeliveryRecord(
        signal_id=signal_id or notification.signal_id,
        status=status,
        payload_json=render_json(notification),
        recorded_at_ms=notification.created_at_ms,
    )


def make_trade(*, closed: bool = False) -> PaperTrade:
    signal = make_candidate()
    if not closed:
        return PaperTrade(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            quantity=Decimal("1"),
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            opened_at_ms=signal.created_at_ms,
        )
    return PaperTrade(
        signal_id=signal.signal_id,
        symbol=signal.symbol,
        quantity=Decimal("1"),
        entry_price=signal.entry_price,
        stop_price=signal.stop_price,
        target_price=signal.target_price,
        opened_at_ms=signal.created_at_ms,
        closed_at_ms=signal.created_at_ms + 60_000,
        exit_price=Decimal("115"),
        exit_reason="target",
        realized_pnl=Decimal("15"),
    )


def test_delivery_round_trip() -> None:
    store = DeliveryStore(":memory:")
    try:
        record = make_record()
        store.put_delivery(record)
        assert store.get_delivery(record.signal_id) == record
        assert store.all_deliveries() == [record]
    finally:
        store.close()


def test_delivery_upsert_does_not_duplicate() -> None:
    store = DeliveryStore(":memory:")
    try:
        record = make_record(DeliveryStatus.PENDING)
        store.put_delivery(record)
        store.mark_status(
            record.signal_id,
            DeliveryStatus.FAILED,
            recorded_at_ms=record.recorded_at_ms,
            error="sink down",
        )
        updated = store.get_delivery(record.signal_id)
        assert updated is not None
        assert updated.status is DeliveryStatus.FAILED
        assert updated.error == "sink down"
        assert len(store.all_deliveries()) == 1
    finally:
        store.close()


def test_missing_delivery_status_update_raises() -> None:
    store = DeliveryStore(":memory:")
    try:
        with pytest.raises(StoreError):
            store.mark_status("missing", DeliveryStatus.DELIVERED, recorded_at_ms=1)
    finally:
        store.close()


def test_delivery_totals_group_by_status() -> None:
    store = DeliveryStore(":memory:")
    try:
        store.put_delivery(make_record(DeliveryStatus.DELIVERED, signal_id="a"))
        store.put_delivery(make_record(DeliveryStatus.REJECTED, signal_id="b"))
        store.put_delivery(make_record(DeliveryStatus.FAILED, signal_id="c"))
        assert store.delivery_totals() == DeliveryTotals(
            total=3, delivered=1, rejected=1, pending=0, failed=1
        )
    finally:
        store.close()


def test_open_trade_round_trip_and_close() -> None:
    store = DeliveryStore(":memory:")
    try:
        trade = make_trade()
        store.put_trade(trade)
        assert store.get_trade(trade.signal_id) == trade
        assert store.open_trades() == [trade]

        store.close_trade(
            trade.signal_id,
            exit_price=Decimal("115"),
            exit_reason="target",
            closed_at_ms=trade.opened_at_ms + 60_000,
            realized_pnl=Decimal("15"),
        )
        closed = store.get_trade(trade.signal_id)
        assert closed is not None
        assert not closed.is_open
        assert closed.exit_reason == "target"
        assert closed.realized_pnl == Decimal("15")
        assert store.open_trades() == []
        assert store.all_trades() == [closed]
    finally:
        store.close()


def test_closing_an_already_closed_trade_fails() -> None:
    store = DeliveryStore(":memory:")
    try:
        trade = make_trade(closed=True)
        store.put_trade(trade)
        with pytest.raises(StoreError):
            store.close_trade(
                trade.signal_id,
                exit_price=Decimal("115"),
                exit_reason="target",
                closed_at_ms=trade.opened_at_ms + 120_000,
                realized_pnl=Decimal("15"),
            )
    finally:
        store.close()


def test_data_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "delivery.sqlite"
    store = DeliveryStore(path)
    record = make_record()
    trade = make_trade()
    store.put_delivery(record)
    store.put_trade(trade)
    store.close()

    reopened = DeliveryStore(path)
    try:
        assert reopened.get_delivery(record.signal_id) == record
        assert reopened.get_trade(trade.signal_id) == trade
    finally:
        reopened.close()
