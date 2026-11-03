"""SQLite outbox and paper-trade ledger."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from nomicronix.signal_delivery.models import (
    DeliveryRecord,
    DeliveryStatus,
    DeliveryTotals,
    PaperTrade,
    StoreError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    signal_id      TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    recorded_at_ms INTEGER NOT NULL,
    error          TEXT
);

CREATE INDEX IF NOT EXISTS deliveries_status_idx ON deliveries (status);

CREATE TABLE IF NOT EXISTS paper_trades (
    signal_id      TEXT PRIMARY KEY,
    symbol         TEXT NOT NULL,
    quantity       TEXT NOT NULL,
    entry_price    TEXT NOT NULL,
    stop_price     TEXT NOT NULL,
    target_price   TEXT NOT NULL,
    opened_at_ms   INTEGER NOT NULL,
    closed_at_ms   INTEGER,
    exit_price     TEXT,
    exit_reason    TEXT,
    realized_pnl   TEXT
);

CREATE INDEX IF NOT EXISTS paper_trades_open_idx ON paper_trades (closed_at_ms);
"""


def _decimal(value: str) -> Decimal:
    return Decimal(str(value))


def _trade_from_row(row: sqlite3.Row) -> PaperTrade:
    return PaperTrade(
        signal_id=str(row["signal_id"]),
        symbol=str(row["symbol"]),
        quantity=_decimal(row["quantity"]),
        entry_price=_decimal(row["entry_price"]),
        stop_price=_decimal(row["stop_price"]),
        target_price=_decimal(row["target_price"]),
        opened_at_ms=int(row["opened_at_ms"]),
        closed_at_ms=None if row["closed_at_ms"] is None else int(row["closed_at_ms"]),
        exit_price=None if row["exit_price"] is None else _decimal(row["exit_price"]),
        exit_reason=None if row["exit_reason"] is None else str(row["exit_reason"]),
        realized_pnl=None if row["realized_pnl"] is None else _decimal(row["realized_pnl"]),
    )


class DeliveryStore:
    """One SQLite database for the outbox and the paper ledger."""

    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
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

    def put_delivery(self, record: DeliveryRecord) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO deliveries
                    (signal_id, status, payload_json, recorded_at_ms, error)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (signal_id) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    recorded_at_ms = excluded.recorded_at_ms,
                    error = excluded.error
                """,
                (
                    record.signal_id,
                    str(record.status),
                    record.payload_json,
                    record.recorded_at_ms,
                    record.error,
                ),
            )

    def mark_status(
        self,
        signal_id: str,
        status: DeliveryStatus,
        *,
        recorded_at_ms: int,
        error: str | None = None,
    ) -> None:
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE deliveries SET status = ?, recorded_at_ms = ?, error = ?"
                " WHERE signal_id = ?",
                (str(status), recorded_at_ms, error, signal_id),
            )
            if cursor.rowcount != 1:
                raise StoreError(f"no delivery row for signal {signal_id}")

    def get_delivery(self, signal_id: str) -> DeliveryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM deliveries WHERE signal_id = ?", (signal_id,)
        ).fetchone()
        if row is None:
            return None
        return DeliveryRecord(
            signal_id=str(row["signal_id"]),
            status=DeliveryStatus(str(row["status"])),
            payload_json=str(row["payload_json"]),
            recorded_at_ms=int(row["recorded_at_ms"]),
            error=None if row["error"] is None else str(row["error"]),
        )

    def all_deliveries(self) -> list[DeliveryRecord]:
        rows = self._conn.execute(
            "SELECT * FROM deliveries ORDER BY recorded_at_ms, signal_id"
        ).fetchall()
        return [
            DeliveryRecord(
                signal_id=str(row["signal_id"]),
                status=DeliveryStatus(str(row["status"])),
                payload_json=str(row["payload_json"]),
                recorded_at_ms=int(row["recorded_at_ms"]),
                error=None if row["error"] is None else str(row["error"]),
            )
            for row in rows
        ]

    def delivery_totals(self) -> DeliveryTotals:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM deliveries GROUP BY status"
        ).fetchall()
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        return DeliveryTotals(
            total=sum(counts.values()),
            delivered=counts.get(DeliveryStatus.DELIVERED.value, 0),
            rejected=counts.get(DeliveryStatus.REJECTED.value, 0),
            pending=counts.get(DeliveryStatus.PENDING.value, 0),
            failed=counts.get(DeliveryStatus.FAILED.value, 0),
        )

    def put_trade(self, trade: PaperTrade) -> None:
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO paper_trades
                    (signal_id, symbol, quantity, entry_price, stop_price,
                     target_price, opened_at_ms, closed_at_ms, exit_price,
                     exit_reason, realized_pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (signal_id) DO UPDATE SET
                    symbol = excluded.symbol,
                    quantity = excluded.quantity,
                    entry_price = excluded.entry_price,
                    stop_price = excluded.stop_price,
                    target_price = excluded.target_price,
                    opened_at_ms = excluded.opened_at_ms,
                    closed_at_ms = excluded.closed_at_ms,
                    exit_price = excluded.exit_price,
                    exit_reason = excluded.exit_reason,
                    realized_pnl = excluded.realized_pnl
                """,
                (
                    trade.signal_id,
                    trade.symbol,
                    str(trade.quantity),
                    str(trade.entry_price),
                    str(trade.stop_price),
                    str(trade.target_price),
                    trade.opened_at_ms,
                    trade.closed_at_ms,
                    None if trade.exit_price is None else str(trade.exit_price),
                    trade.exit_reason,
                    None if trade.realized_pnl is None else str(trade.realized_pnl),
                ),
            )

    def close_trade(
        self,
        signal_id: str,
        *,
        exit_price: Decimal,
        exit_reason: str,
        closed_at_ms: int,
        realized_pnl: Decimal,
    ) -> None:
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE paper_trades
                SET closed_at_ms = ?, exit_price = ?, exit_reason = ?, realized_pnl = ?
                WHERE signal_id = ? AND closed_at_ms IS NULL
                """,
                (closed_at_ms, str(exit_price), exit_reason, str(realized_pnl), signal_id),
            )
            if cursor.rowcount != 1:
                raise StoreError(f"no open paper trade for signal {signal_id}")

    def get_trade(self, signal_id: str) -> PaperTrade | None:
        row = self._conn.execute(
            "SELECT * FROM paper_trades WHERE signal_id = ?", (signal_id,)
        ).fetchone()
        return None if row is None else _trade_from_row(row)

    def open_trades(self) -> list[PaperTrade]:
        rows = self._conn.execute(
            "SELECT * FROM paper_trades WHERE closed_at_ms IS NULL ORDER BY opened_at_ms, signal_id"
        ).fetchall()
        return [_trade_from_row(row) for row in rows]

    def all_trades(self) -> list[PaperTrade]:
        rows = self._conn.execute(
            "SELECT * FROM paper_trades ORDER BY opened_at_ms, signal_id"
        ).fetchall()
        return [_trade_from_row(row) for row in rows]
