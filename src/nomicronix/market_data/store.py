"""SQLite store for closed candles.

A store is bound to one exchange. Only closed candles are ever written, so only
closed candles can ever be read back.
"""

import sqlite3
import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from nomicronix.market_data.models import Candle, Timeframe

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    exchange     TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    timeframe    TEXT    NOT NULL,
    open_time_ms INTEGER NOT NULL,
    open         TEXT    NOT NULL,
    high         TEXT    NOT NULL,
    low          TEXT    NOT NULL,
    close        TEXT    NOT NULL,
    volume       TEXT    NOT NULL,
    PRIMARY KEY (exchange, symbol, timeframe, open_time_ms)
) WITHOUT ROWID
"""

_UPSERT = """
INSERT INTO candles
    (exchange, symbol, timeframe, open_time_ms, open, high, low, close, volume)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (exchange, symbol, timeframe, open_time_ms) DO UPDATE SET
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    volume = excluded.volume
"""

_SELECT_COLUMNS = "open_time_ms, open, high, low, close, volume"


class MarketDataStore(Protocol):
    """What consumer modules (backtesting, strategy-engine) depend on.

    Results are ascending by open time, de-duplicated, closed candles only.
    Ranges are half-open: start_ms <= open_time_ms < end_ms.

    Freshness: before trusting prices, check `is_stale(latest_candle(...), now_ms)` from
    nomicronix.market_data.models. The live updater logs STALE_DATA by the same rule.
    """

    def get_candles(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int
    ) -> list[Candle]: ...

    def latest_candle(self, symbol: str, timeframe: str) -> Candle | None: ...

    def find_gaps(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, int]]: ...


class OpenCandleError(ValueError):
    """A candle that has not closed yet was passed to the store."""


def utc_now_ms() -> int:
    return time.time_ns() // 1_000_000


class SqliteStore:
    def __init__(
        self,
        path: str | Path,
        exchange: str,
        *,
        now_ms: Callable[[], int] = utc_now_ms,
    ) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute(_SCHEMA)
        self._exchange = exchange
        self._now_ms = now_ms

    def close(self) -> None:
        self._conn.close()

    def upsert_candles(self, candles: Iterable[Candle]) -> None:
        """Insert or replace candles. All-or-nothing.

        Raises OpenCandleError (and writes nothing) if any candle has not closed yet.
        """
        batch = list(candles)
        now = self._now_ms()
        for c in batch:
            if c.close_time_ms > now:
                raise OpenCandleError(
                    f"{c.symbol} {c.timeframe} candle opening at {c.open_time_ms} "
                    f"has not closed yet (closes at {c.close_time_ms}, now is {now})"
                )
        rows = [
            (
                self._exchange,
                c.symbol,
                c.timeframe,
                c.open_time_ms,
                str(c.open),
                str(c.high),
                str(c.low),
                str(c.close),
                str(c.volume),
            )
            for c in batch
        ]
        with self._conn:
            self._conn.executemany(_UPSERT, rows)

    def get_candles(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int
    ) -> list[Candle]:
        """Candles with start_ms <= open_time_ms < end_ms, ascending."""
        cursor = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM candles"
            " WHERE exchange = ? AND symbol = ? AND timeframe = ?"
            " AND open_time_ms >= ? AND open_time_ms < ?"
            " ORDER BY open_time_ms",
            (self._exchange, symbol, timeframe, start_ms, end_ms),
        )
        return [_to_candle(symbol, timeframe, row) for row in cursor]

    def latest_candle(self, symbol: str, timeframe: str) -> Candle | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM candles"
            " WHERE exchange = ? AND symbol = ? AND timeframe = ?"
            " ORDER BY open_time_ms DESC LIMIT 1",
            (self._exchange, symbol, timeframe),
        ).fetchone()
        return None if row is None else _to_candle(symbol, timeframe, row)

    def earliest_candle(self, symbol: str, timeframe: str) -> Candle | None:
        """Oldest stored candle. Used by backfill to resume; not part of the consumer contract."""
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM candles"
            " WHERE exchange = ? AND symbol = ? AND timeframe = ?"
            " ORDER BY open_time_ms ASC LIMIT 1",
            (self._exchange, symbol, timeframe),
        ).fetchone()
        return None if row is None else _to_candle(symbol, timeframe, row)

    def find_gaps(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int
    ) -> list[tuple[int, int]]:
        """Missing-candle ranges within [start_ms, end_ms). See find_gaps_in_range."""
        cursor = self._conn.execute(
            "SELECT open_time_ms FROM candles"
            " WHERE exchange = ? AND symbol = ? AND timeframe = ?"
            " AND open_time_ms >= ? AND open_time_ms < ?"
            " ORDER BY open_time_ms",
            (self._exchange, symbol, timeframe, start_ms, end_ms),
        )
        return find_gaps_in_range(
            [row[0] for row in cursor], Timeframe(timeframe).ms, start_ms, end_ms
        )


def find_gaps(open_times_ms: list[int], step_ms: int) -> list[tuple[int, int]]:
    """Missing-candle ranges inside a sorted series of candle open times.

    Each range is (first_missing_open_ms, last_missing_open_ms), both inclusive.
    """
    gaps: list[tuple[int, int]] = []
    for prev, curr in zip(open_times_ms, open_times_ms[1:], strict=False):
        if curr - prev > step_ms:
            gaps.append((prev + step_ms, curr - step_ms))
    return gaps


def find_gaps_in_range(
    open_times_ms: list[int], step_ms: int, start_ms: int, end_ms: int
) -> list[tuple[int, int]]:
    """Missing-candle ranges for the candle slots in [start_ms, end_ms).

    Candle slots sit on multiples of step_ms (UTC epoch grid). An unaligned start is
    rounded up to the next slot. Input may be unsorted and contain duplicates or
    times outside the range. Same range format as find_gaps.
    """
    first = -(-start_ms // step_ms) * step_ms  # first slot >= start_ms
    last = (end_ms - 1) // step_ms * step_ms  # last slot < end_ms
    if first > last:
        return []
    inside = sorted({t for t in open_times_ms if first <= t <= last})
    # Sentinels one slot outside the range turn gaps at either edge into inner gaps.
    return find_gaps([first - step_ms, *inside, last + step_ms], step_ms)


def _to_candle(symbol: str, timeframe: str, row: tuple[int, str, str, str, str, str]) -> Candle:
    open_time_ms, open_, high, low, close, volume = row
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time_ms=open_time_ms,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
    )
