"""Historical candle download: paginated, idempotent, retrying."""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import product

from nomicronix.market_data.adapter import (
    AdapterError,
    ExchangeAdapter,
    NetworkError,
    RateLimitError,
)
from nomicronix.market_data.models import Candle, Timeframe
from nomicronix.market_data.store import SqliteStore, utc_now_ms

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackfillConfig:
    limit: int = 1000  # candles per request (Binance maximum)
    max_retries: int = 5
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    sleep: Callable[[float], None] = time.sleep
    now_ms: Callable[[], int] = utc_now_ms


DEFAULT_CONFIG = BackfillConfig()


def backfill_range(
    adapter: ExchangeAdapter,
    store: SqliteStore,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int | None = None,
    *,
    config: BackfillConfig = DEFAULT_CONFIG,
) -> int:
    """Download closed candles with since_ms <= open_time_ms < until_ms into the store.

    `since_ms` is rounded up to the next candle boundary. `until_ms=None` means
    "up to the last closed candle". Safe to re-run: writes are upserts.
    Returns the number of distinct candles written.
    """
    step_ms = Timeframe(timeframe).ms
    cursor = -(-since_ms // step_ms) * step_ms
    stored = 0
    while until_ms is None or cursor < until_ms:
        now = config.now_ms()
        if cursor + step_ms > now:  # the candle at `cursor` has not closed yet
            break
        rows = _fetch_with_retry(adapter, symbol, timeframe, cursor, config)
        if not rows:
            break
        wanted = {
            c.open_time_ms: c
            for c in rows
            if c.close_time_ms <= now and (until_ms is None or c.open_time_ms < until_ms)
        }
        store.upsert_candles(wanted.values())
        stored += len(wanted)
        next_cursor = rows[-1].open_time_ms + step_ms
        if next_cursor <= cursor:  # no progress: stop instead of looping forever
            break
        cursor = next_cursor
    return stored


def backfill_series(
    adapter: ExchangeAdapter,
    store: SqliteStore,
    symbol: str,
    timeframe: str,
    since_ms: int,
    *,
    config: BackfillConfig = DEFAULT_CONFIG,
) -> int:
    """Like backfill_range up to the last closed candle, but resumes from what is stored.

    Only the missing ends are downloaded: anything before the earliest stored candle
    (if `since_ms` is earlier) and everything after the latest. A series is stored
    contiguously from where it started, so the middle is never re-downloaded.
    """
    step_ms = Timeframe(timeframe).ms
    since_ms = -(-since_ms // step_ms) * step_ms
    earliest = store.earliest_candle(symbol, timeframe)
    latest = store.latest_candle(symbol, timeframe)
    if earliest is None or latest is None:
        return backfill_range(adapter, store, symbol, timeframe, since_ms, config=config)

    stored = 0
    if since_ms < earliest.open_time_ms:
        until_ms = earliest.open_time_ms
        stored += backfill_range(
            adapter, store, symbol, timeframe, since_ms, until_ms=until_ms, config=config
        )
    tail_start = max(since_ms, latest.open_time_ms + step_ms)
    stored += backfill_range(adapter, store, symbol, timeframe, tail_start, config=config)
    return stored


@dataclass(frozen=True)
class SeriesFailure:
    symbol: str
    timeframe: str
    error: str


@dataclass
class BackfillSummary:
    stored: dict[tuple[str, str], int] = field(default_factory=dict)
    failures: list[SeriesFailure] = field(default_factory=list)


def run_backfill(
    adapter: ExchangeAdapter,
    store: SqliteStore,
    symbols: Sequence[str],
    timeframes: Sequence[str],
    since_ms: int,
    *,
    config: BackfillConfig = DEFAULT_CONFIG,
) -> BackfillSummary:
    """Backfill every symbol x timeframe. One failing series never stops the others."""
    summary = BackfillSummary()
    for symbol, timeframe in product(symbols, timeframes):
        try:
            count = backfill_series(adapter, store, symbol, timeframe, since_ms, config=config)
        except AdapterError as exc:
            logger.error("%s %s: backfill failed: %s", symbol, timeframe, exc)
            summary.failures.append(SeriesFailure(symbol, timeframe, str(exc)))
            continue
        logger.info("%s %s: %d candles written", symbol, timeframe, count)
        summary.stored[(symbol, timeframe)] = count
    return summary


def _fetch_with_retry(
    adapter: ExchangeAdapter, symbol: str, timeframe: str, since_ms: int, config: BackfillConfig
) -> list[Candle]:
    attempt = 0
    while True:
        try:
            return adapter.fetch_candles(symbol, timeframe, since_ms, config.limit)
        except (RateLimitError, NetworkError) as exc:
            if attempt >= config.max_retries:
                raise
            delay = min(config.base_delay_s * 2**attempt, config.max_delay_s)
            logger.warning(
                "%s %s: %s; retry %d/%d in %.1fs",
                symbol,
                timeframe,
                exc,
                attempt + 1,
                config.max_retries,
                delay,
            )
            config.sleep(delay)
            attempt += 1
