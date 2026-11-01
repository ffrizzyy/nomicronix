from pathlib import Path

import pytest

from nomicronix.market_data.__main__ import main, parse_since
from nomicronix.market_data.backfill import BackfillConfig
from nomicronix.market_data.store import SqliteStore
from tests.market_data.fakes import JAN_1_2025_MS, FakeExchangeAdapter, make_candles

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
