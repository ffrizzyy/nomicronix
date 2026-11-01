"""Historical candle download: paginated, idempotent, retrying."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from nomicronix.market_data.adapter import ExchangeAdapter, NetworkError, RateLimitError
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
