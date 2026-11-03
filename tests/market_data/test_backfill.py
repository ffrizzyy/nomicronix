import pytest

from nomicronix.market_data.adapter import AdapterError, NetworkError, RateLimitError
from nomicronix.market_data.backfill import (
    BackfillConfig,
    SeriesFailure,
    backfill_range,
    backfill_series,
    run_backfill,
)
from nomicronix.market_data.models import Candle
from nomicronix.market_data.store import SqliteStore
from tests.market_data.fakes import JAN_1_2025_MS, FakeExchangeAdapter, make_candle, make_candles

HOUR_MS = 3_600_000
START = JAN_1_2025_MS
NOW_MS = START + 250 * HOUR_MS  # candles 0..249 are closed, the candle at 250 is still open


def t(hours: int) -> int:
    return START + hours * HOUR_MS


def make_store(now_ms: int = NOW_MS) -> SqliteStore:
    return SqliteStore(":memory:", "fake", now_ms=lambda: now_ms)


def make_config(
    sleeps: list[float] | None = None,
    *,
    now_ms: int = NOW_MS,
    limit: int = 100,
    max_retries: int = 5,
) -> BackfillConfig:
    record = sleeps if sleeps is not None else []
    return BackfillConfig(
        limit=limit,
        max_retries=max_retries,
        base_delay_s=1.0,
        max_delay_s=60.0,
        sleep=record.append,
        now_ms=lambda: now_ms,
    )


def stored_open_times(store: SqliteStore) -> list[int]:
    return [c.open_time_ms for c in store.get_candles("BTC/USDT", "1h", 0, t(10_000))]


# --- core behaviour -------------------------------------------------------------------------


def test_backfill_fills_the_store_up_to_the_last_closed_candle_across_pages() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 250))
    store = make_store()

    stored = backfill_range(adapter, store, "BTC/USDT", "1h", START, config=make_config())

    assert stored == 250
    assert stored_open_times(store) == [t(i) for i in range(250)]
    assert store.find_gaps("BTC/USDT", "1h", t(0), t(250)) == []


def test_backfill_pages_forward_from_the_last_candle_received() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 250))

    backfill_range(adapter, make_store(), "BTC/USDT", "1h", START, config=make_config(limit=100))

    assert [since for (_, _, since, _) in adapter.calls] == [t(0), t(100), t(200)]


def test_backfill_never_stores_the_candle_that_is_still_open() -> None:
    candles = make_candles(START, 250) + [make_candle(t(250))]  # exchange also returns the open one
    adapter = FakeExchangeAdapter(candles)
    store = make_store()

    backfill_range(adapter, store, "BTC/USDT", "1h", START, config=make_config())

    latest = store.latest_candle("BTC/USDT", "1h")
    assert latest is not None and latest.open_time_ms == t(249)


def test_running_backfill_twice_gives_identical_rows() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 250))
    store = make_store()

    backfill_range(adapter, store, "BTC/USDT", "1h", START, config=make_config())
    first = store.get_candles("BTC/USDT", "1h", 0, t(10_000))
    backfill_range(adapter, store, "BTC/USDT", "1h", START, config=make_config())

    assert store.get_candles("BTC/USDT", "1h", 0, t(10_000)) == first


def test_backfill_ignores_duplicate_rows_from_the_exchange() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 10), duplicate_first_row=True)
    store = make_store()

    stored = backfill_range(adapter, store, "BTC/USDT", "1h", START, config=make_config())

    assert stored == 10
    assert len(stored_open_times(store)) == 10


def test_backfill_rounds_an_unaligned_since_up_to_the_next_candle() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 10))
    store = make_store()

    backfill_range(adapter, store, "BTC/USDT", "1h", START + 1, config=make_config())

    assert stored_open_times(store) == [t(i) for i in range(1, 10)]


def test_backfill_stops_when_the_exchange_has_nothing_more() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 10))
    store = make_store()

    stored = backfill_range(adapter, store, "BTC/USDT", "1h", t(100), config=make_config())

    assert stored == 0
    assert len(adapter.calls) == 1


def test_backfill_does_not_ask_the_exchange_for_a_candle_that_has_not_closed() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 250))

    stored = backfill_range(adapter, make_store(), "BTC/USDT", "1h", t(250), config=make_config())

    assert stored == 0
    assert adapter.calls == []


def test_backfill_respects_an_upper_bound() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 250))
    store = make_store()

    backfill_range(adapter, store, "BTC/USDT", "1h", t(10), until_ms=t(20), config=make_config())

    assert stored_open_times(store) == [t(i) for i in range(10, 20)]


# --- retries --------------------------------------------------------------------------------


def test_backfill_retries_rate_limits_with_exponential_backoff() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 10))
    adapter.fail_next(RateLimitError("429"), times=3)
    sleeps: list[float] = []
    store = make_store()

    stored = backfill_range(adapter, store, "BTC/USDT", "1h", START, config=make_config(sleeps))

    assert sleeps == [1.0, 2.0, 4.0]
    assert stored == 10


def test_backfill_retries_network_errors() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 10))
    adapter.fail_next(NetworkError("connection reset"))
    sleeps: list[float] = []

    stored = backfill_range(
        adapter, make_store(), "BTC/USDT", "1h", START, config=make_config(sleeps)
    )

    assert sleeps == [1.0]
    assert stored == 10


def test_backoff_delay_is_capped() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 10))
    adapter.fail_next(RateLimitError("429"), times=8)
    sleeps: list[float] = []

    backfill_range(
        adapter, make_store(), "BTC/USDT", "1h", START, config=make_config(sleeps, max_retries=8)
    )

    assert max(sleeps) == 60.0


def test_backfill_gives_up_after_max_retries_and_raises() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 10))
    adapter.fail_next(RateLimitError("429"), times=10)
    sleeps: list[float] = []

    with pytest.raises(RateLimitError):
        config = make_config(sleeps, max_retries=2)
        backfill_range(adapter, make_store(), "BTC/USDT", "1h", START, config=config)

    assert sleeps == [1.0, 2.0]
    assert len(adapter.calls) == 3


def test_backfill_does_not_retry_errors_that_are_not_transient() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 10))
    adapter.fail_next(AdapterError("unknown symbol"))
    sleeps: list[float] = []

    with pytest.raises(AdapterError):
        backfill_range(adapter, make_store(), "BTC/USDT", "1h", START, config=make_config(sleeps))

    assert sleeps == []
    assert len(adapter.calls) == 1


# --- resume ---------------------------------------------------------------------------------


class InterruptingAdapter:
    """Delegates to an inner adapter, but call number `interrupt_on` raises KeyboardInterrupt."""

    name = "fake"

    def __init__(self, inner: FakeExchangeAdapter, interrupt_on: int) -> None:
        self._inner = inner
        self._interrupt_on = interrupt_on
        self._calls = 0

    def fetch_candles(self, symbol: str, timeframe: str, since_ms: int, limit: int) -> list[Candle]:
        self._calls += 1
        if self._calls == self._interrupt_on:
            raise KeyboardInterrupt
        return self._inner.fetch_candles(symbol, timeframe, since_ms, limit)


def test_an_interrupted_backfill_resumes_without_redownloading_stored_ranges() -> None:
    data = make_candles(START, 250)
    store = make_store()
    interrupted = InterruptingAdapter(FakeExchangeAdapter(data), interrupt_on=3)

    with pytest.raises(KeyboardInterrupt):
        backfill_series(interrupted, store, "BTC/USDT", "1h", START, config=make_config())
    assert stored_open_times(store) == [t(i) for i in range(200)]

    resumed = FakeExchangeAdapter(data)
    backfill_series(resumed, store, "BTC/USDT", "1h", START, config=make_config())

    assert resumed.calls[0][2] == t(200)
    assert stored_open_times(store) == [t(i) for i in range(250)]
    assert store.find_gaps("BTC/USDT", "1h", t(0), t(250)) == []


def test_a_second_run_downloads_only_the_new_candles() -> None:
    store = make_store(now_ms=t(260))  # store clock never behind the backfill clock
    backfill_series(
        FakeExchangeAdapter(make_candles(START, 250)),
        store,
        "BTC/USDT",
        "1h",
        START,
        config=make_config(),
    )

    later = FakeExchangeAdapter(make_candles(START, 260))
    backfill_series(later, store, "BTC/USDT", "1h", START, config=make_config(now_ms=t(260)))

    assert later.calls[0][2] == t(250)
    assert stored_open_times(store) == [t(i) for i in range(260)]


def test_a_rerun_with_nothing_new_makes_no_requests() -> None:
    data = make_candles(START, 250)
    store = make_store()
    backfill_series(FakeExchangeAdapter(data), store, "BTC/USDT", "1h", START, config=make_config())

    again = FakeExchangeAdapter(data)
    backfill_series(again, store, "BTC/USDT", "1h", START, config=make_config())

    assert again.calls == []


def test_an_earlier_since_fills_in_the_missing_start() -> None:
    data = make_candles(START, 250)
    store = make_store()
    first = FakeExchangeAdapter(data)
    backfill_series(first, store, "BTC/USDT", "1h", t(100), config=make_config())

    earlier = FakeExchangeAdapter(data)
    backfill_series(earlier, store, "BTC/USDT", "1h", START, config=make_config())

    assert [since for (_, _, since, _) in earlier.calls] == [t(0)]
    assert stored_open_times(store) == [t(i) for i in range(250)]


def test_a_later_since_does_not_download_anything_before_it() -> None:
    data = make_candles(START, 250)
    clock = [t(100)]  # mutable clock: first run happens at hour 100, second at hour 250
    store = SqliteStore(":memory:", "fake", now_ms=lambda: clock[0])
    config = BackfillConfig(limit=100, sleep=lambda _: None, now_ms=lambda: clock[0])
    backfill_series(FakeExchangeAdapter(data), store, "BTC/USDT", "1h", START, config=config)
    clock[0] = NOW_MS

    later = FakeExchangeAdapter(data)
    backfill_series(later, store, "BTC/USDT", "1h", t(200), config=config)

    assert later.calls[0][2] == t(200)
    assert stored_open_times(store) == [t(i) for i in range(100)] + [t(i) for i in range(200, 250)]


def test_backfill_series_on_an_empty_store_downloads_from_since() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 250))
    store = make_store()

    stored = backfill_series(adapter, store, "BTC/USDT", "1h", START, config=make_config())

    assert stored == 250
    assert adapter.calls[0][2] == t(0)


# --- many series ----------------------------------------------------------------------------


def test_run_backfill_handles_every_symbol_and_timeframe() -> None:
    adapter = FakeExchangeAdapter(
        make_candles(START, 10, symbol="BTC/USDT", timeframe="1h")
        + make_candles(START, 10, symbol="ETH/USDT", timeframe="1h")
        + make_candles(START, 40, symbol="BTC/USDT", timeframe="15m")
        + make_candles(START, 40, symbol="ETH/USDT", timeframe="15m")
    )
    store = make_store()

    summary = run_backfill(
        adapter, store, ["BTC/USDT", "ETH/USDT"], ["1h", "15m"], START, config=make_config()
    )

    assert summary.failures == []
    assert summary.stored == {
        ("BTC/USDT", "1h"): 10,
        ("BTC/USDT", "15m"): 40,
        ("ETH/USDT", "1h"): 10,
        ("ETH/USDT", "15m"): 40,
    }


def test_run_backfill_continues_after_one_series_fails() -> None:
    adapter = FakeExchangeAdapter(
        make_candles(START, 10, symbol="BTC/USDT") + make_candles(START, 10, symbol="ETH/USDT")
    )
    adapter.fail_next(AdapterError("bad symbol"))
    store = make_store()

    summary = run_backfill(
        adapter, store, ["BTC/USDT", "ETH/USDT"], ["1h"], START, config=make_config()
    )

    assert summary.failures == [SeriesFailure("BTC/USDT", "1h", "bad symbol")]
    assert summary.stored == {("ETH/USDT", "1h"): 10}
    assert store.latest_candle("BTC/USDT", "1h") is None
    assert store.latest_candle("ETH/USDT", "1h") is not None


def test_run_backfill_reports_a_series_that_runs_out_of_retries() -> None:
    adapter = FakeExchangeAdapter(
        make_candles(START, 10, symbol="BTC/USDT") + make_candles(START, 10, symbol="ETH/USDT")
    )
    adapter.fail_next(RateLimitError("429"), times=2)  # first series: attempt + 1 retry, both fail

    summary = run_backfill(
        adapter,
        make_store(),
        ["BTC/USDT", "ETH/USDT"],
        ["1h"],
        START,
        config=make_config(max_retries=1),
    )

    assert summary.failures == [SeriesFailure("BTC/USDT", "1h", "429")]
    assert summary.stored == {("ETH/USDT", "1h"): 10}


# --- spec success criterion 1, at full scale ------------------------------------------------


def test_twelve_months_of_hourly_candles_backfill_with_no_gaps() -> None:
    count = 24 * 365
    now = START + count * HOUR_MS
    adapter = FakeExchangeAdapter(make_candles(START, count + 1))  # the extra one is still open
    store = SqliteStore(":memory:", "fake", now_ms=lambda: now)
    config = BackfillConfig(limit=1000, sleep=lambda _: None, now_ms=lambda: now)

    summary = run_backfill(adapter, store, ["BTC/USDT"], ["1h"], START, config=config)

    assert summary.failures == []
    assert summary.stored == {("BTC/USDT", "1h"): count}
    assert store.find_gaps("BTC/USDT", "1h", START, START + count * HOUR_MS) == []
    assert len(adapter.calls) == 9  # 1000 candles per request


# --- safety: never loop forever ---------------------------------------------------------------


class StuckAdapter:
    """A misbehaving exchange that keeps answering with an old candle, whatever is asked."""

    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def fetch_candles(self, symbol: str, timeframe: str, since_ms: int, limit: int) -> list[Candle]:
        self.calls += 1
        if self.calls > 3:
            raise AssertionError("backfill kept asking: no progress guard")
        return [make_candle(START)]


def test_backfill_stops_when_the_exchange_makes_no_progress() -> None:
    adapter = StuckAdapter()

    backfill_range(adapter, make_store(), "BTC/USDT", "1h", t(100), config=make_config())

    assert adapter.calls == 1
