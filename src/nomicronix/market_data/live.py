"""Keep the store current: poll shortly after each candle closes."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import product

from nomicronix.market_data.adapter import ExchangeAdapter
from nomicronix.market_data.backfill import DEFAULT_CONFIG, BackfillConfig, backfill_range
from nomicronix.market_data.models import Timeframe
from nomicronix.market_data.store import SqliteStore


@dataclass(frozen=True)
class LiveConfig:
    poll_delay_s: float = 5.0  # wait this long after a candle closes before asking for it
    retry_interval_s: float = 30.0  # when a series is behind, ask again after this long
    backfill: BackfillConfig = DEFAULT_CONFIG  # request size, retry policy, clock, sleep


DEFAULT_LIVE_CONFIG = LiveConfig()


def update_series(
    adapter: ExchangeAdapter,
    store: SqliteStore,
    symbol: str,
    timeframe: str,
    config: LiveConfig = DEFAULT_LIVE_CONFIG,
) -> int:
    """Store every candle that has closed since the last stored one. Returns how many.

    On an empty store, starts at the most recent closed candle (history is backfill's job).
    Because it always resumes from the latest stored candle, the same call also catches up
    after an outage or a restart.
    """
    step_ms = Timeframe(timeframe).ms
    latest = store.latest_candle(symbol, timeframe)
    if latest is None:
        since_ms = (config.backfill.now_ms() // step_ms - 1) * step_ms
    else:
        since_ms = latest.open_time_ms + step_ms
    return backfill_range(adapter, store, symbol, timeframe, since_ms, config=config.backfill)


def _is_behind(store: SqliteStore, symbol: str, timeframe: str, now_ms: int) -> bool:
    """True if the most recently closed candle is not in the store yet."""
    step_ms = Timeframe(timeframe).ms
    expected_open_ms = (now_ms // step_ms - 1) * step_ms
    latest = store.latest_candle(symbol, timeframe)
    return latest is None or latest.open_time_ms < expected_open_ms


def run_tick(
    adapter: ExchangeAdapter,
    store: SqliteStore,
    series: Sequence[tuple[str, str]],
    config: LiveConfig = DEFAULT_LIVE_CONFIG,
) -> list[tuple[str, str]]:
    """Update every series once. Returns the series that are still behind afterwards."""
    for symbol, timeframe in series:
        update_series(adapter, store, symbol, timeframe, config)
    now_ms = config.backfill.now_ms()
    return [(s, tf) for s, tf in series if _is_behind(store, s, tf, now_ms)]


def _seconds_until_next_poll(
    series: Sequence[tuple[str, str]], any_behind: bool, config: LiveConfig
) -> float:
    now_ms = config.backfill.now_ms()
    next_close_ms = min((now_ms // Timeframe(tf).ms + 1) * Timeframe(tf).ms for _, tf in series)
    wake_ms = next_close_ms + round(config.poll_delay_s * 1000)
    if any_behind:
        wake_ms = min(wake_ms, now_ms + round(config.retry_interval_s * 1000))
    return max(0.0, (wake_ms - now_ms) / 1000)


def _never() -> bool:
    return False


def run_live(
    adapter: ExchangeAdapter,
    store: SqliteStore,
    symbols: Sequence[str],
    timeframes: Sequence[str],
    *,
    config: LiveConfig = DEFAULT_LIVE_CONFIG,
    should_stop: Callable[[], bool] = _never,
) -> None:
    """Poll forever (until `should_stop`): once per candle close, sooner when behind."""
    series = list(product(symbols, timeframes))
    while not should_stop():
        behind = run_tick(adapter, store, series, config)
        config.backfill.sleep(_seconds_until_next_poll(series, bool(behind), config))
