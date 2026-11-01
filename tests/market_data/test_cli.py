from pathlib import Path

import pytest

from nomicronix.market_data.adapter import AdapterError
from nomicronix.market_data.__main__ import main, parse_since
from nomicronix.market_data.backfill import BackfillConfig
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
NOW_MS = JAN_1_2025_MS + 50 * HOUR_MS


def make_config() -> BackfillConfig:
    return BackfillConfig(sleep=lambda _: None, now_ms=lambda: NOW_MS)


def open_store(db: Path) -> SqliteStore:
    return SqliteStore(db, "fake", now_ms=lambda: NOW_MS)


def test_parse_since_reads_an_iso_date_as_utc_midnight() -> None:
    assert parse_since("2025-01-01") == JAN_1_2025_MS


def test_parse_since_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_since("last tuesday")


def test_backfill_command_stores_candles_and_exits_zero(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite"
    adapter = FakeExchangeAdapter(make_candles(JAN_1_2025_MS, 50))

    code = main(
        [
            "backfill",
            "--exchange", "fake",
            "--symbols", "BTC/USDT",
            "--timeframes", "1h",
            "--since", "2025-01-01",
            "--db", str(db),
        ],
        adapter=adapter,
        config=make_config(),
    )

    assert code == 0
    stored = open_store(db).get_candles("BTC/USDT", "1h", 0, JAN_1_2025_MS + 1000 * HOUR_MS)
    assert len(stored) == 50


def test_backfill_command_handles_several_symbols_and_timeframes(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite"
    adapter = FakeExchangeAdapter(
        make_candles(JAN_1_2025_MS, 50, symbol="BTC/USDT", timeframe="1h")
        + make_candles(JAN_1_2025_MS, 50, symbol="ETH/USDT", timeframe="1h")
        + make_candles(JAN_1_2025_MS, 200, symbol="BTC/USDT", timeframe="15m")
        + make_candles(JAN_1_2025_MS, 200, symbol="ETH/USDT", timeframe="15m")
    )

    code = main(
        [
            "backfill", "--exchange", "fake",
            "--symbols", "BTC/USDT,ETH/USDT", "--timeframes", "1h,15m",
            "--since", "2025-01-01", "--db", str(db),
        ],
        adapter=adapter,
        config=make_config(),
    )

    store = open_store(db)
    end = JAN_1_2025_MS + 1000 * HOUR_MS
    assert code == 0
    assert len(store.get_candles("ETH/USDT", "1h", 0, end)) == 50
    assert len(store.get_candles("BTC/USDT", "15m", 0, end)) == 200


def test_backfill_command_exits_non_zero_when_a_series_fails_but_keeps_the_rest(
    tmp_path: Path,
) -> None:
    db = tmp_path / "market.sqlite"
    adapter = FakeExchangeAdapter(
        make_candles(JAN_1_2025_MS, 50, symbol="BTC/USDT")
        + make_candles(JAN_1_2025_MS, 50, symbol="ETH/USDT")
    )
    adapter.fail_next(AdapterError("bad symbol"))

    code = main(
        [
            "backfill", "--exchange", "fake",
            "--symbols", "BTC/USDT,ETH/USDT", "--timeframes", "1h",
            "--since", "2025-01-01", "--db", str(db),
        ],
        adapter=adapter,
        config=make_config(),
    )

    store = open_store(db)
    assert code == 1
    assert store.latest_candle("BTC/USDT", "1h") is None
    assert store.latest_candle("ETH/USDT", "1h") is not None


def test_backfill_command_rejects_an_unsupported_timeframe(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "backfill", "--exchange", "fake", "--symbols", "BTC/USDT",
                "--timeframes", "2h", "--since", "2025-01-01", "--db", str(tmp_path / "m.sqlite"),
            ],
            adapter=FakeExchangeAdapter([]),
            config=make_config(),
        )


# --- verify command -------------------------------------------------------------------------


def seed(db: Path, candles: list[Candle]) -> None:
    store = open_store(db)
    store.upsert_candles(candles)
    store.close()


def verify_args(db: Path, *extra: str) -> list[str]:
    return [
        "verify", "--exchange", "fake", "--symbols", "BTC/USDT", "--timeframes", "1h",
        "--db", str(db), *extra,
    ]


def test_verify_command_exits_zero_for_a_clean_series(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite"
    seed(db, make_candles(JAN_1_2025_MS, 10))

    assert main(verify_args(db)) == 0


def test_verify_command_exits_non_zero_when_gaps_are_found(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite"
    candles = make_candles(JAN_1_2025_MS, 10)
    seed(db, [c for i, c in enumerate(candles) if i not in (4, 5)])

    assert main(verify_args(db)) == 1


def test_verify_command_exits_non_zero_when_nothing_is_stored(tmp_path: Path) -> None:
    assert main(verify_args(tmp_path / "market.sqlite")) == 1


def test_verify_command_with_refill_repairs_gaps_and_exits_zero(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite"
    candles = make_candles(JAN_1_2025_MS, 10)
    seed(db, [c for i, c in enumerate(candles) if i not in (4, 5)])

    code = main(
        verify_args(db, "--refill"),
        adapter=FakeExchangeAdapter(candles),
        config=make_config(),
    )

    assert code == 0
    assert len(open_store(db).get_candles("BTC/USDT", "1h", 0, JAN_1_2025_MS + 100 * HOUR_MS)) == 10


def test_verify_command_without_refill_never_calls_the_exchange(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite"
    candles = make_candles(JAN_1_2025_MS, 10)
    seed(db, [c for i, c in enumerate(candles) if i not in (4, 5)])
    adapter = FakeExchangeAdapter(candles)

    main(verify_args(db), adapter=adapter, config=make_config())

    assert adapter.calls == []


# --- live command ---------------------------------------------------------------------------


def test_live_command_stores_new_candles_and_exits_zero_when_stopped(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite"
    clock = Clock(JAN_1_2025_MS + 5 * HOUR_MS + 10_000)
    adapter = FakeExchangeAdapter(make_candles(JAN_1_2025_MS, 30), clock=clock)

    code = main(
        [
            "live", "--exchange", "fake", "--symbols", "BTC/USDT", "--timeframes", "1h",
            "--db", str(db),
        ],
        adapter=adapter,
        config=BackfillConfig(sleep=clock.sleep, now_ms=clock),
        should_stop=stop_after(2),
    )

    stored = open_store(db).get_candles("BTC/USDT", "1h", 0, JAN_1_2025_MS + 1000 * HOUR_MS)
    assert code == 0
    assert [(c.open_time_ms - JAN_1_2025_MS) // HOUR_MS for c in stored] == [4, 5]


def test_live_command_exits_cleanly_on_ctrl_c(tmp_path: Path) -> None:
    clock = Clock(JAN_1_2025_MS + 5 * HOUR_MS + 10_000)

    def ctrl_c() -> bool:
        raise KeyboardInterrupt

    code = main(
        [
            "live", "--exchange", "fake", "--symbols", "BTC/USDT", "--timeframes", "1h",
            "--db", str(tmp_path / "market.sqlite"),
        ],
        adapter=FakeExchangeAdapter([], clock=clock),
        config=BackfillConfig(sleep=clock.sleep, now_ms=clock),
        should_stop=ctrl_c,
    )

    assert code == 0
