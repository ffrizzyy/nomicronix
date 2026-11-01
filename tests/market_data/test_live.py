import pytest

from nomicronix.market_data.adapter import AdapterError, NetworkError
from nomicronix.market_data.backfill import BackfillConfig, SeriesFailure
from nomicronix.market_data.live import LiveConfig, run_live, run_tick, update_series
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


# --- outages and restarts -------------------------------------------------------------------


def test_a_short_outage_is_ridden_out_with_backoff_inside_the_tick() -> None:
    clock = Clock(t(10) + 5_000)
    adapter = FakeExchangeAdapter(make_candles(START, 30), clock=clock)
    adapter.fail_next(NetworkError("connection reset"), times=2)
    store = make_store(clock)
    store.upsert_candles(make_candles(START, 9))  # hours 0..8

    update_series(adapter, store, "BTC/USDT", "1h", make_config(clock))

    assert clock.sleeps == [1.0, 2.0]
    assert hours_stored(store)[-1] == 9


def test_after_a_long_outage_the_next_tick_fills_every_missed_candle() -> None:
    clock = Clock(t(12) + 5_000)  # nothing ran since hour 5
    adapter = FakeExchangeAdapter(make_candles(START, 30), clock=clock)
    store = make_store(clock)
    store.upsert_candles(make_candles(START, 5))  # hours 0..4

    run_tick(adapter, store, [("BTC/USDT", "1h")], make_config(clock))

    assert hours_stored(store) == list(range(12))
    assert store.find_gaps("BTC/USDT", "1h", t(0), t(12)) == []


def test_a_restart_catches_up_from_the_latest_stored_candle() -> None:
    clock = Clock(t(9) + 5_000)
    store = make_store(clock)
    store.upsert_candles(make_candles(START, 6))  # the previous process stored hours 0..5

    restarted = FakeExchangeAdapter(make_candles(START, 30), clock=clock)
    run_live(
        restarted, store, ["BTC/USDT"], ["1h"],
        config=make_config(clock), should_stop=stop_after(1),
    )

    assert restarted.calls[0][2] == t(6)  # asked only for what was missing
    assert hours_stored(store) == list(range(9))


def test_run_tick_reports_a_failed_series_and_still_updates_the_others() -> None:
    clock = Clock(t(5) + 5_000)
    adapter = FakeExchangeAdapter(
        make_candles(START, 30, symbol="BTC/USDT") + make_candles(START, 30, symbol="ETH/USDT"),
        clock=clock,
    )
    adapter.fail_next(AdapterError("bad symbol"))

    result = run_tick(
        adapter, make_store(clock), [("BTC/USDT", "1h"), ("ETH/USDT", "1h")], make_config(clock)
    )

    assert result.failures == [SeriesFailure("BTC/USDT", "1h", "bad symbol")]
    assert result.behind == [("BTC/USDT", "1h")]


def test_an_outage_longer_than_the_retry_budget_does_not_crash_the_loop() -> None:
    clock = Clock(t(5) + 5_000)
    adapter = FakeExchangeAdapter(make_candles(START, 30), clock=clock)
    adapter.fail_next(NetworkError("exchange unreachable"), times=2)
    store = make_store(clock)

    run_live(
        adapter, store, ["BTC/USDT"], ["1h"],
        config=make_config(clock, max_retries=1), should_stop=stop_after(2),
    )

    # tick 1: one backoff (1s), retry fails, give up -> wait 30s, not a whole hour
    assert clock.sleeps[:2] == [1.0, 30.0]
    assert hours_stored(store) == [4]


def test_unexpected_errors_still_propagate() -> None:
    clock = Clock(t(5) + 5_000)
    adapter = FakeExchangeAdapter(make_candles(START, 30), clock=clock)
    adapter.fail_next(RuntimeError("a bug, not an outage"))

    with pytest.raises(RuntimeError):
        run_tick(adapter, make_store(clock), [("BTC/USDT", "1h")], make_config(clock))
