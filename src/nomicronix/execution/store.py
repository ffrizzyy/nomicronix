"""SQLite journal for order intents, exchange orders and append-only events."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from nomicronix.execution.models import (
    EventType,
    ExchangeOrder,
    IntentState,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    StoreError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id        TEXT PRIMARY KEY,
    signal_id        TEXT NOT NULL UNIQUE,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    quantity         TEXT NOT NULL,
    entry_price      TEXT NOT NULL,
    stop_price       TEXT NOT NULL,
    target_price     TEXT NOT NULL,
    approved_notional TEXT NOT NULL,
    created_at_ms    INTEGER NOT NULL,
    expires_at_ms    INTEGER NOT NULL,
    state            TEXT NOT NULL,
    filled_quantity  TEXT NOT NULL DEFAULT '0',
    exchange_order_id TEXT
);

CREATE INDEX IF NOT EXISTS intents_state_idx ON intents (state);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id   TEXT PRIMARY KEY,
    exchange_order_id TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    status            TEXT NOT NULL,
    quantity          TEXT NOT NULL,
    filled_quantity   TEXT NOT NULL,
    average_price     TEXT,
    fee               TEXT NOT NULL,
    updated_at_ms     INTEGER NOT NULL,
    raw_status        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id    TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    at_ms        INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS events_intent_idx ON events (intent_id, id);
"""


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _json(payload: dict[str, object]) -> str:
    return json.dumps(payload, default=_json_default, sort_keys=True)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _intent_from_row(row: sqlite3.Row) -> OrderIntent:
    try:
        state = IntentState(str(row["state"]))
    except ValueError as exc:
        raise StoreError(f"intent {row['intent_id']} has an unknown state") from exc
    try:
        return OrderIntent(
            intent_id=str(row["intent_id"]),
            signal_id=str(row["signal_id"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            quantity=_decimal(row["quantity"]),
            entry_price=_decimal(row["entry_price"]),
            stop_price=_decimal(row["stop_price"]),
            target_price=_decimal(row["target_price"]),
            approved_notional=_decimal(row["approved_notional"]),
            created_at_ms=int(row["created_at_ms"]),
            expires_at_ms=int(row["expires_at_ms"]),
            state=state,
            filled_quantity=_decimal(row["filled_quantity"]),
            exchange_order_id=(
                None if row["exchange_order_id"] is None else str(row["exchange_order_id"])
            ),
        )
    except (ValueError, ArithmeticError) as exc:
        raise StoreError(f"intent {row['intent_id']} is corrupt: {exc}") from exc


def _order_from_row(row: sqlite3.Row) -> ExchangeOrder:
    try:
        status = OrderStatus(str(row["status"]))
    except ValueError as exc:
        raise StoreError(f"order {row['client_order_id']} has an unknown status") from exc
    try:
        return ExchangeOrder(
            client_order_id=str(row["client_order_id"]),
            exchange_order_id=str(row["exchange_order_id"]),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            status=status,
            quantity=_decimal(row["quantity"]),
            filled_quantity=_decimal(row["filled_quantity"]),
            average_price=(
                None if row["average_price"] is None else _decimal(row["average_price"])
            ),
            fee=_decimal(row["fee"]),
            updated_at_ms=int(row["updated_at_ms"]),
            raw_status=str(row["raw_status"]),
        )
    except (ValueError, ArithmeticError) as exc:
        raise StoreError(f"order {row['client_order_id']} is corrupt: {exc}") from exc


def _event_from_row(row: sqlite3.Row) -> OrderEvent:
    try:
        event_type = EventType(str(row["event_type"]))
    except ValueError as exc:
        raise StoreError(f"event {row['id']} has an unknown type") from exc
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError as exc:
        raise StoreError(f"event {row['id']} has an invalid payload") from exc
    if not isinstance(payload, dict):
        raise StoreError(f"event {row['id']} payload must be an object")
    return OrderEvent(
        intent_id=str(row["intent_id"]),
        event_type=event_type,
        at_ms=int(row["at_ms"]),
        payload={str(key): value for key, value in payload.items()},
    )


class ExecutionStore:
    """One SQLite database for the order journal. WAL, single writer."""

    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _append(self, conn: sqlite3.Connection, event: OrderEvent) -> None:
        conn.execute(
            "INSERT INTO events (intent_id, event_type, at_ms, payload_json) VALUES (?, ?, ?, ?)",
            (event.intent_id, str(event.event_type), event.at_ms, _json(event.payload)),
        )

    def create_intent(self, intent: OrderIntent, *, event: OrderEvent | None = None) -> bool:
        """Insert an intent once. A duplicate is a no-op, never a second order."""
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO intents
                    (intent_id, signal_id, symbol, side, quantity, entry_price,
                     stop_price, target_price, approved_notional, created_at_ms,
                     expires_at_ms, state, filled_quantity, exchange_order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.intent_id,
                    intent.signal_id,
                    intent.symbol,
                    intent.side,
                    str(intent.quantity),
                    str(intent.entry_price),
                    str(intent.stop_price),
                    str(intent.target_price),
                    str(intent.approved_notional),
                    intent.created_at_ms,
                    intent.expires_at_ms,
                    str(intent.state),
                    str(intent.filled_quantity),
                    intent.exchange_order_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            if event is not None:
                self._append(conn, event)
            return True

    def append_event(self, event: OrderEvent) -> None:
        with self._transaction() as conn:
            self._append(conn, event)

    def update_intent(
        self,
        intent_id: str,
        *,
        state: IntentState | None = None,
        filled_quantity: Decimal | None = None,
        exchange_order_id: str | None = None,
        event: OrderEvent | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[object] = []
        if state is not None:
            assignments.append("state = ?")
            values.append(str(state))
        if filled_quantity is not None:
            assignments.append("filled_quantity = ?")
            values.append(str(filled_quantity))
        if exchange_order_id is not None:
            assignments.append("exchange_order_id = ?")
            values.append(exchange_order_id)
        if not assignments:
            raise StoreError("update_intent needs at least one field")
        values.append(intent_id)
        with self._transaction() as conn:
            cursor = conn.execute(
                f"UPDATE intents SET {', '.join(assignments)} WHERE intent_id = ?",
                values,
            )
            if cursor.rowcount != 1:
                raise StoreError(f"no intent {intent_id}")
            if event is not None:
                self._append(conn, event)

    def get_intent(self, intent_id: str) -> OrderIntent | None:
        row = self._conn.execute(
            "SELECT * FROM intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        return None if row is None else _intent_from_row(row)

    def list_intents(self, states: Sequence[IntentState] | None = None) -> list[OrderIntent]:
        if states:
            placeholders = ", ".join("?" for _ in states)
            rows = self._conn.execute(
                f"SELECT * FROM intents WHERE state IN ({placeholders})"
                " ORDER BY created_at_ms, intent_id",
                [str(state) for state in states],
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM intents ORDER BY created_at_ms, intent_id"
            ).fetchall()
        return [_intent_from_row(row) for row in rows]

    def put_order(self, order: ExchangeOrder) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO orders
                    (client_order_id, exchange_order_id, symbol, side, status,
                     quantity, filled_quantity, average_price, fee, updated_at_ms,
                     raw_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (client_order_id) DO UPDATE SET
                    exchange_order_id = excluded.exchange_order_id,
                    symbol = excluded.symbol,
                    side = excluded.side,
                    status = excluded.status,
                    quantity = excluded.quantity,
                    filled_quantity = excluded.filled_quantity,
                    average_price = excluded.average_price,
                    fee = excluded.fee,
                    updated_at_ms = excluded.updated_at_ms,
                    raw_status = excluded.raw_status
                """,
                (
                    order.client_order_id,
                    order.exchange_order_id,
                    order.symbol,
                    order.side,
                    str(order.status),
                    str(order.quantity),
                    str(order.filled_quantity),
                    None if order.average_price is None else str(order.average_price),
                    str(order.fee),
                    order.updated_at_ms,
                    order.raw_status,
                ),
            )

    def get_order(self, client_order_id: str) -> ExchangeOrder | None:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return None if row is None else _order_from_row(row)

    def list_orders(self) -> list[ExchangeOrder]:
        rows = self._conn.execute(
            "SELECT * FROM orders ORDER BY updated_at_ms, client_order_id"
        ).fetchall()
        return [_order_from_row(row) for row in rows]

    def open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]:
        if symbol is None:
            rows = self._conn.execute(
                "SELECT * FROM orders WHERE status = ? ORDER BY updated_at_ms, client_order_id",
                (str(OrderStatus.OPEN),),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM orders WHERE status = ? AND symbol = ?"
                " ORDER BY updated_at_ms, client_order_id",
                (str(OrderStatus.OPEN), symbol),
            ).fetchall()
        return [_order_from_row(row) for row in rows]

    def events_for(self, intent_id: str) -> list[OrderEvent]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE intent_id = ? ORDER BY id", (intent_id,)
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def event_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"])

    def payload_of(self, intent_id: str, event_type: EventType) -> list[dict[str, Any]]:
        return [
            event.payload for event in self.events_for(intent_id) if event.event_type is event_type
        ]
