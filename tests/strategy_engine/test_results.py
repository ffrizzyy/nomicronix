import json
from decimal import Decimal
from pathlib import Path

import pytest

from nomicronix.strategy_engine.engine import scan_candles
from nomicronix.strategy_engine.models import SignalBatch
from nomicronix.strategy_engine.results import summary, to_payload, write_result
from tests.strategy_engine.fakes import (
    AlwaysFlat,
    FixedProposer,
    make_candles,
    make_config,
)


def batch(with_signals: bool = True) -> SignalBatch:
    config = make_config(
        start_ms=0,
        end_ms=5 * 3_600_000,
        min_history=1,
    )
    proposer = FixedProposer() if with_signals else AlwaysFlat()
    return scan_candles(make_candles(5, start_ms=0), proposer, config)


def test_payload_is_complete_and_typed() -> None:
    result = batch()
    payload = to_payload(result)

    assert payload["schema_version"] == 1
    assert payload["kind"] == "signals"
    assert payload["config"]["symbol"] == "BTC/USDT"
    assert payload["config"]["ttl_ms"] == 300_000
    assert payload["data"]["candle_count"] == 5
    assert len(payload["signals"]) == 5

    signal = payload["signals"][0]
    assert signal["side"] == "buy"
    assert signal["entry_price"] == "100"
    assert signal["stop_price"] == "95"
    assert signal["target_price"] == "115"
    assert signal["confidence"] == 0.6
    assert signal["features"] == {"atr": "2.5"}
    assert signal["created_at_ms"] == signal["candle_open_time_ms"] + 3_600_000


def test_payload_is_json_serializable_without_nan() -> None:
    json.dumps(to_payload(batch()), allow_nan=False)


def test_features_are_sorted_for_stable_bytes() -> None:
    config = make_config(start_ms=0, end_ms=3_600_000, min_history=1)
    proposer = FixedProposer()
    result = scan_candles(make_candles(1, start_ms=0), proposer, config)
    payload = to_payload(result)
    assert list(payload["signals"][0]["features"]) == ["atr"]


def test_written_json_is_byte_identical_across_runs(tmp_path: Path) -> None:
    result = batch()
    first = write_result(result, tmp_path / "first.json")
    second = write_result(result, tmp_path / "second.json")

    assert first.read_bytes() == second.read_bytes()
    assert first.read_text(encoding="utf-8").endswith("\n")


def test_existing_output_requires_force(tmp_path: Path) -> None:
    result = batch()
    destination = tmp_path / "signals.json"
    write_result(result, destination)

    with pytest.raises(FileExistsError):
        write_result(result, destination)

    write_result(result, destination, force=True)
    assert destination.exists()


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    destination = tmp_path / "a" / "b" / "signals.json"
    write_result(batch(), destination)
    assert destination.exists()


def test_summary_lists_signals_and_warnings() -> None:
    text = summary(batch())
    assert "strategy: fixed" in text
    assert "signals: 5" in text
    assert "entry=100" in text
    assert "confidence=0.600" in text

    empty = summary(batch(with_signals=False))
    assert "signals: 0" in empty


def test_summary_prints_warnings_after_signals() -> None:
    result = batch()
    with_warning = SignalBatch(
        config=result.config,
        strategy_id=result.strategy_id,
        first_candle_ms=result.first_candle_ms,
        last_candle_ms=result.last_candle_ms,
        candle_count=result.candle_count,
        signals=result.signals,
        warnings=("1: confidence below floor",),
    )
    text = summary(with_warning)
    assert text.endswith("warning: 1: confidence below floor")


def test_zero_signal_payload_has_empty_lists() -> None:
    payload = to_payload(batch(with_signals=False))
    assert payload["signals"] == []
    assert payload["warnings"] == []


def test_decimal_text_is_used_for_prices() -> None:
    result = batch()
    payload = to_payload(result)
    assert Decimal(payload["signals"][0]["entry_price"]) == Decimal("100")
