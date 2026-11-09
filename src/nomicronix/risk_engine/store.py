"""SQLite persistence for risk state, approvals and the decision journal."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from nomicronix.risk_engine.models import (
    Approval,
    Fill,
    FillAction,
    KillSwitch,
    Position,
    RiskDecision,
    TradeProposal,
)
from nomicronix.risk_engine.state import PortfolioState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS risk_state (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    cash                 TEXT    NOT NULL,
    peak_equity          TEXT    NOT NULL,
    day_start_equity     TEXT    NOT NULL,
    day_utc              TEXT    NOT NULL,
    realized_pnl_today   TEXT    NOT NULL,
    kill_switch_json     TEXT,
    last_mark_ms         INTEGER NOT NULL,
    updated_at_ms        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    symbol         TEXT PRIMARY KEY,
    quantity       TEXT NOT NULL,
    entry_price    TEXT NOT NULL,
    stop_price     TEXT NOT NULL,
    target_price   TEXT NOT NULL,
    current_price  TEXT NOT NULL,
    opened_at_ms   INTEGER NOT NULL,
    signal_id      TEXT NOT NULL UNIQUE,
    entry_fee      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    signal_id      TEXT PRIMARY KEY,
    symbol         TEXT NOT NULL,
    quantity       TEXT NOT NULL,
    notional       TEXT NOT NULL,
    stop_price     TEXT NOT NULL,
    target_price   TEXT NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    consumed_at_ms INTEGER,
    filled_quantity TEXT NOT NULL DEFAULT '0'
);

CREATE TABLE IF NOT EXISTS decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms          INTEGER NOT NULL,
    signal_id      TEXT NOT NULL,
    approved       INTEGER NOT NULL,
    proposal_json  TEXT NOT NULL,
    decision_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fill_keys (
    fill_key       TEXT PRIMARY KEY,
    signal_id      TEXT NOT NULL,
    recorded_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS decisions_signal_idx ON decisions (signal_id);
"""


class StoreError(RuntimeError):
    """Persisted state is missing, corrupt or inconsistent."""


class ApprovalError(StoreError):
    """An approval is missing, already consumed or does not match the fill."""


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _json(value: object) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True)


def _kill_from_json(raw: str | None) -> KillSwitch | None:
    if raw is None:
        return None
    data = json.loads(raw)
    return KillSwitch(
        reason=str(data["reason"]),
        engaged_at_ms=int(data["engaged_at_ms"]),
        engaged_by=str(data["engaged_by"]),
    )


class RiskStateStore:
    """One SQLite database for one risk account.

    All mutations go through a transaction. The decision journal is append-only.
    """

    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add v1.1 columns to databases created by v1."""
        columns = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(approvals)")}
        if "filled_quantity" not in columns:
            self._conn.execute(
                "ALTER TABLE approvals ADD COLUMN filled_quantity TEXT NOT NULL DEFAULT '0'"
            )

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def load_state(self, now_ms: int, initial_cash: Decimal) -> PortfolioState:
        row = self._conn.execute("SELECT * FROM risk_state WHERE id = 1").fetchone()
        if row is None:
            state = PortfolioState.new(initial_cash, now_ms)
            self.save_state(state, now_ms)
            return state

        positions = {
            r["symbol"]: Position(
                symbol=str(r["symbol"]),
                quantity=Decimal(str(r["quantity"])),
                entry_price=Decimal(str(r["entry_price"])),
                stop_price=Decimal(str(r["stop_price"])),
                target_price=Decimal(str(r["target_price"])),
                current_price=Decimal(str(r["current_price"])),
                opened_at_ms=int(r["opened_at_ms"]),
                signal_id=str(r["signal_id"]),
                entry_fee=Decimal(str(r["entry_fee"])),
            )
            for r in self._conn.execute("SELECT * FROM positions")
        }
        return PortfolioState(
            cash=Decimal(str(row["cash"])),
            positions=positions,
            peak_equity=Decimal(str(row["peak_equity"])),
            day_start_equity=Decimal(str(row["day_start_equity"])),
            day_utc=date.fromisoformat(str(row["day_utc"])),
            realized_pnl_today=Decimal(str(row["realized_pnl_today"])),
            kill_switch=_kill_from_json(row["kill_switch_json"]),
            last_mark_ms=int(row["last_mark_ms"]),
        )

    def save_state(self, state: PortfolioState, now_ms: int) -> None:
        with self.transaction() as conn:
            self._save_state(conn, state, now_ms)

    def _save_state(
        self,
        conn: sqlite3.Connection,
        state: PortfolioState,
        now_ms: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO risk_state
                (id, cash, peak_equity, day_start_equity, day_utc,
                 realized_pnl_today, kill_switch_json, last_mark_ms, updated_at_ms)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                cash = excluded.cash,
                peak_equity = excluded.peak_equity,
                day_start_equity = excluded.day_start_equity,
                day_utc = excluded.day_utc,
                realized_pnl_today = excluded.realized_pnl_today,
                kill_switch_json = excluded.kill_switch_json,
                last_mark_ms = excluded.last_mark_ms,
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                str(state.cash),
                str(state.peak_equity),
                str(state.day_start_equity),
                state.day_utc.isoformat(),
                str(state.realized_pnl_today),
                None if state.kill_switch is None else _json(asdict(state.kill_switch)),
                state.last_mark_ms,
                now_ms,
            ),
        )
        conn.execute("DELETE FROM positions")
        for position in state.positions.values():
            conn.execute(
                """
                INSERT INTO positions
                    (symbol, quantity, entry_price, stop_price, target_price,
                     current_price, opened_at_ms, signal_id, entry_fee)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.symbol,
                    str(position.quantity),
                    str(position.entry_price),
                    str(position.stop_price),
                    str(position.target_price),
                    str(position.current_price),
                    position.opened_at_ms,
                    position.signal_id,
                    str(position.entry_fee),
                ),
            )

    def persist_check(
        self,
        state: PortfolioState,
        decision: RiskDecision,
        proposal: TradeProposal,
        now_ms: int,
    ) -> None:
        with self.transaction() as conn:
            self._save_state(conn, state, now_ms)
            conn.execute(
                """
                INSERT INTO decisions
                    (ts_ms, signal_id, approved, proposal_json, decision_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    now_ms,
                    decision.signal_id,
                    int(decision.approved),
                    _json(asdict(proposal)),
                    _json(asdict(decision)),
                ),
            )
            if decision.approved:
                conn.execute(
                    """
                    INSERT INTO approvals
                        (signal_id, symbol, quantity, notional, stop_price,
                         target_price, created_at_ms, consumed_at_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        decision.signal_id,
                        proposal.symbol,
                        str(decision.approved_quantity),
                        str(decision.approved_notional),
                        str(proposal.stop_price),
                        str(proposal.target_price),
                        now_ms,
                    ),
                )

    def load_approval(self, signal_id: str) -> Approval | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE signal_id = ?", (signal_id,)
        ).fetchone()
        if row is None:
            return None
        return Approval(
            signal_id=str(row["signal_id"]),
            symbol=str(row["symbol"]),
            quantity=Decimal(str(row["quantity"])),
            notional=Decimal(str(row["notional"])),
            stop_price=Decimal(str(row["stop_price"])),
            target_price=Decimal(str(row["target_price"])),
            created_at_ms=int(row["created_at_ms"]),
            consumed_at_ms=None if row["consumed_at_ms"] is None else int(row["consumed_at_ms"]),
            filled_quantity=Decimal(str(row["filled_quantity"])),
        )

    def expire_approval(self, signal_id: str, now_ms: int) -> None:
        """Void the unfilled remainder of an approval, e.g. after a cancel."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET consumed_at_ms = ?"
                " WHERE signal_id = ? AND consumed_at_ms IS NULL",
                (now_ms, signal_id),
            )
            if cursor.rowcount != 1:
                raise ApprovalError(f"approval {signal_id} is missing or already consumed")

    def persist_fill(
        self,
        state: PortfolioState,
        signal_id: str,
        fill: Fill,
        now_ms: int,
        *,
        approval: Approval | None = None,
        fill_key: str | None = None,
    ) -> bool:
        with self.transaction() as conn:
            if fill_key is not None:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO fill_keys (fill_key, signal_id, recorded_at_ms)"
                    " VALUES (?, ?, ?)",
                    (fill_key, signal_id, now_ms),
                )
                if cursor.rowcount != 1:
                    return False
            if fill.action is FillAction.OPEN:
                if approval is None:
                    raise ApprovalError("an OPEN fill requires an approval")
                filled = approval.filled_quantity + fill.quantity
                consumed_at = now_ms if filled == approval.quantity else None
                cursor = conn.execute(
                    """
                    UPDATE approvals
                       SET filled_quantity = ?, consumed_at_ms = ?
                     WHERE signal_id = ?
                       AND consumed_at_ms IS NULL
                       AND filled_quantity = ?
                    """,
                    (str(filled), consumed_at, signal_id, str(approval.filled_quantity)),
                )
                if cursor.rowcount != 1:
                    raise ApprovalError(
                        f"approval {signal_id} is missing, already consumed or changed"
                    )
            self._save_state(conn, state, now_ms)
            return True

    def has_fill_key(self, fill_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM fill_keys WHERE fill_key = ?", (fill_key,)
        ).fetchone()
        return row is not None

    def decision_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()
        return int(row["n"])

    def decisions_for(self, signal_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE signal_id = ? ORDER BY id", (signal_id,)
        )
        return [dict(row) for row in rows]
