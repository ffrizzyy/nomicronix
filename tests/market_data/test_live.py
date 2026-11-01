from nomicronix.market_data.backfill import BackfillConfig
from nomicronix.market_data.live import LiveConfig, run_live, update_series
from nomicronix.market_data.models import Candle
from nomicronix.market_data.store import SqliteStore
from tests.market_data.fakes import (
    JAN_1_2025_MS,
    Clock,
    FakeExchangeAdapter,
    make_candles,
    stop_after,
)

HOUR_MS = 3_600_000
START = JAN_1_2025_MS


def t(hours: float) -> int:
    return START + round(hours * HOUR_MS)


def make_config(clock: Clock, *, max_retries: int = 5) -> LiveConfig:
    return LiveConfig(
        poll_delay_s=5.0,
        retry_interval_s=30.0,
        backfill=BackfillConfig(
            limit=100, max_retries=max_retries, sleep=clock.sleep, now_ms=clock
        ),
    )


def make_store(clock: Clock) -> SqliteStore:
    return SqliteStore(":memory:", "fake", now_ms=clock)


def hours_stored(store: SqliteStore, symbol: str = "BTC/USDT", timeframe: str = "1h") -> list[int]:
    candles: list[Candle] = store.get_candles(symbol, timeframe, 0, t(10_000))
    return [(c.open_time_ms - START) // HOUR_MS for c in candles]


# --- update_series: one catch-up step -------------------------------------------------------


def test_update_series_on_an_empty_store_starts_at_the_latest_closed_candle() -> None:
    clock = Clock(t(10.5))
    adapter = FakeExchangeAdapter(make_candles(START, 11), clock=clock)  # hour 10 is still open
    store = make_store(clock)

    stored = update_series(adapter, store, "BTC/USDT", "1h", make_config(clock))

    assert stored == 1
    assert hours_stored(store) == [9]


def test_update_series_stores_only_candles_closed_since_the_last_stored_one() -> None:
    clock = Clock(t(8) + 10_000)
    adapter = FakeExchangeAdapter(make_candles(START, 12), clock=clock)
    store = make_store(clock)
    store.upsert_candles(make_candles(START, 5))  # hours 0..4

    update_series(adapter, store, "BTC/USDT", "1h", make_config(clock))

    assert hours_stored(store) == [0, 1, 2, 3, 4, 5, 6, 7]


def test_update_series_never_stores_the_candle_that_is_still_open() -> None:
    clock = Clock(t(8.5))
    adapter = FakeExchangeAdapter(make_candles(START, 12), clock=clock)  # hour 8 is open
    store = make_store(clock)
    store.upsert_candles(make_candles(START, 5))

    update_series(adapter, store, "BTC/USDT", "1h", make_config(clock))

    assert hours_stored(store)[-1] == 7


def test_update_series_makes_no_request_when_nothing_new_has_closed() -> None:
    clock = Clock(t(8) + 10_000)
    adapter = FakeExchangeAdapter(make_candles(START, 12), clock=clock)
    store = make_store(clock)
    store.upsert_candles(make_candles(START, 8))  # hours 0..7: hour 7 closed at t(8)

    stored = update_series(adapter, store, "BTC/USDT", "1h", make_config(clock))

    assert stored == 0
    assert adapter.calls == []


# --- run_live: the loop ---------------------------------------------------------------------


def test_the_loop_stores_each_new_candle_shortly_after_it_closes() -> None:
    clock = Clock(t(5) + 100_000)  # 100s after hour 4 closed
    adapter = FakeExchangeAdapter(make_candles(START, 30), clock=clock)
    store = make_store(clock)

    run_live(
        adapter, store, ["BTC/USDT"], ["1h"],
        config=make_config(clock), should_stop=stop_after(4),
    )

    assert hours_stored(store) == [4, 5, 6, 7]
    # first wake: next close (t(6)) + 5s poll delay; afterwards once per hour
    assert clock.sleeps == [3505.0, 3600.0, 3600.0, 3600.0]


def test_the_loop_handles_several_symbols_in_one_process() -> None:
    clock = Clock(t(5) + 10_000)
    adapter = FakeExchangeAdapter(
        make_candles(START, 30, symbol="BTC/USDT") + make_candles(START, 30, symbol="ETH/USDT"),
        clock=clock,
    )
    store = make_store(clock)

    run_live(
        adapter, store, ["BTC/USDT", "ETH/USDT"], ["1h"],
        config=make_config(clock), should_stop=stop_after(2),
    )

    assert hours_stored(store, "BTC/USDT") == [4, 5]
    assert hours_stored(store, "ETH/USDT") == [4, 5]


def test_the_loop_wakes_at_the_soonest_candle_close_across_timeframes() -> None:
    clock = Clock(t(1) + 100_000)  # 100s past the hour
    adapter = FakeExchangeAdapter(
        make_candles(START, 30, timeframe="1h") + make_candles(START, 120, timeframe="15m"),
        clock=clock,
    )
    store = make_store(clock)

    run_live(
        adapter, store, ["BTC/USDT"], ["1h", "15m"],
        config=make_config(clock), should_stop=stop_after(1),
    )

    # next 15m close is 800s away, plus the 5s poll delay
    assert clock.sleeps == [805.0]


def test_the_loop_retries_soon_when_the_exchange_has_not_published_the_candle_yet() -> None:
    clock = Clock(t(5) + 5_000)  # 5s after hour 4 closed
    published = make_candles(START, 30)
    hour_4 = [c for c in published if c.open_time_ms == t(4)]
    adapter = FakeExchangeAdapter([c for c in published if c.open_time_ms != t(4)], clock=clock)
    store = make_store(clock)

    original_sleep = clock.sleep

    def sleep_then_publish(seconds: float) -> None:
        original_sleep(seconds)
        adapter.add_candles(hour_4)  # the exchange catches up while we wait

    config = LiveConfig(
        poll_delay_s=5.0,
        retry_interval_s=30.0,
        backfill=BackfillConfig(limit=100, sleep=sleep_then_publish, now_ms=clock),
    )

    run_live(adapter, store, ["BTC/USDT"], ["1h"], config=config, should_stop=stop_after(2))

    assert clock.sleeps[0] == 30.0  # did not wait a full hour
    assert hours_stored(store) == [4]
