from nomicronix.market_data.backfill import BackfillConfig
from nomicronix.market_data.store import SqliteStore
from nomicronix.market_data.verify import format_report, verify_series
from tests.market_data.fakes import JAN_1_2025_MS, FakeExchangeAdapter, make_candles

HOUR_MS = 3_600_000
START = JAN_1_2025_MS
NOW_MS = START + 100 * HOUR_MS


def t(hours: int) -> int:
    return START + hours * HOUR_MS


def make_store() -> SqliteStore:
    return SqliteStore(":memory:", "fake", now_ms=lambda: NOW_MS)


def make_config() -> BackfillConfig:
    return BackfillConfig(sleep=lambda _: None, now_ms=lambda: NOW_MS)


def store_with_hole() -> SqliteStore:
    """Hours 0..9 stored, except hours 4 and 5."""
    store = make_store()
    store.upsert_candles([c for i, c in enumerate(make_candles(START, 10)) if i not in (4, 5)])
    return store


# --- verifying what is stored ---------------------------------------------------------------


def test_verify_reports_count_first_and_last_for_a_contiguous_series() -> None:
    store = make_store()
    store.upsert_candles(make_candles(START, 10))

    report = verify_series(store, "BTC/USDT", "1h")

    assert (report.count, report.first_open_ms, report.last_open_ms) == (10, t(0), t(9))
    assert report.gaps == []
    assert report.ok


def test_verify_reports_a_hole_as_a_gap() -> None:
    report = verify_series(store_with_hole(), "BTC/USDT", "1h")

    assert report.count == 8
    assert report.gaps == [(t(4), t(5))]
    assert not report.refill_attempted
    assert not report.ok


def test_verify_treats_an_empty_series_as_not_ok() -> None:
    report = verify_series(make_store(), "BTC/USDT", "1h")

    assert report.count == 0
    assert report.first_open_ms is None and report.last_open_ms is None
    assert not report.ok


def test_verify_reports_a_missing_start_when_since_is_earlier_than_the_first_candle() -> None:
    store = make_store()
    store.upsert_candles(make_candles(t(3), 7))  # hours 3..9

    report = verify_series(store, "BTC/USDT", "1h", since_ms=START)

    assert report.gaps == [(t(0), t(2))]


# --- refilling from the exchange ------------------------------------------------------------


def test_refill_fills_a_gap_the_exchange_can_supply() -> None:
    store = store_with_hole()
    adapter = FakeExchangeAdapter(make_candles(START, 10))

    report = verify_series(store, "BTC/USDT", "1h", adapter=adapter, config=make_config())

    assert report.refill_attempted
    assert report.refilled_candles == 2
    assert report.gaps == []
    assert report.count == 10
    assert report.ok


def test_refill_only_asks_the_exchange_about_the_gap() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 10))

    verify_series(store_with_hole(), "BTC/USDT", "1h", adapter=adapter, config=make_config())

    assert [since for (_, _, since, _) in adapter.calls] == [t(4)]


def test_gaps_the_exchange_cannot_supply_stay_reported_after_a_refill() -> None:
    store = store_with_hole()
    without_hole = [c for i, c in enumerate(make_candles(START, 10)) if i not in (4, 5)]
    adapter = FakeExchangeAdapter(without_hole)

    report = verify_series(store, "BTC/USDT", "1h", adapter=adapter, config=make_config())

    assert report.refill_attempted
    assert report.refilled_candles == 0
    assert report.gaps == [(t(4), t(5))]
    assert not report.ok


def test_no_refill_is_attempted_when_there_are_no_gaps() -> None:
    store = make_store()
    store.upsert_candles(make_candles(START, 10))
    adapter = FakeExchangeAdapter(make_candles(START, 10))

    report = verify_series(store, "BTC/USDT", "1h", adapter=adapter, config=make_config())

    assert not report.refill_attempted
    assert adapter.calls == []


# --- the text report ------------------------------------------------------------------------


def test_report_for_a_clean_series_says_no_gaps() -> None:
    store = make_store()
    store.upsert_candles(make_candles(START, 10))

    text = format_report(verify_series(store, "BTC/USDT", "1h"))

    assert text == "BTC/USDT 1h: 10 candles, 2025-01-01T00:00Z to 2025-01-01T09:00Z, no gaps"


def test_report_lists_each_gap_with_its_time_range_and_size() -> None:
    text = format_report(verify_series(store_with_hole(), "BTC/USDT", "1h"))

    assert "1 gap" in text
    assert "GAP 2025-01-01T04:00Z to 2025-01-01T05:00Z (2 candles)" in text


def test_report_labels_remaining_gaps_unfillable_after_a_refill_attempt() -> None:
    adapter = FakeExchangeAdapter([])

    text = format_report(
        verify_series(store_with_hole(), "BTC/USDT", "1h", adapter=adapter, config=make_config())
    )

    assert "UNFILLABLE (exchange returned nothing) 2025-01-01T04:00Z to 2025-01-01T05:00Z" in text


def test_report_mentions_how_many_candles_a_refill_recovered() -> None:
    adapter = FakeExchangeAdapter(make_candles(START, 10))

    text = format_report(
        verify_series(store_with_hole(), "BTC/USDT", "1h", adapter=adapter, config=make_config())
    )

    assert "refilled 2 candles" in text
    assert "no gaps" in text


def test_report_for_an_empty_series_says_so() -> None:
    text = format_report(verify_series(make_store(), "BTC/USDT", "1h"))

    assert text == "BTC/USDT 1h: no candles stored"


def test_report_uses_the_singular_for_a_one_candle_gap() -> None:
    store = make_store()
    store.upsert_candles([c for i, c in enumerate(make_candles(START, 10)) if i != 4])

    text = format_report(verify_series(store, "BTC/USDT", "1h"))

    assert "(1 candle)" in text
