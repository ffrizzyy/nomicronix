import json
from pathlib import Path
from typing import Any

import pytest

from nomicronix.signal_delivery.models import SignalFileError
from nomicronix.signal_delivery.signals import load_signals
from tests.signal_delivery.fakes import make_candidate, write_signals


def write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def raw_payload(signal: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "signals",
        "config": {"symbol": "BTC/USDT", "timeframe": "1h"},
        "signals": [signal if signal is not None else raw_signal()],
    }


def raw_signal() -> dict[str, Any]:
    return {
        "signal_id": "sma-cross:BTC/USDT:1h:1735689600000",
        "strategy_id": "sma-cross",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "side": "buy",
        "entry_price": "100",
        "stop_price": "95",
        "target_price": "115",
        "confidence": 0.6,
        "rationale": "test",
        "features": {"atr": "2.5"},
        "created_at_ms": 1735693200000,
        "expires_at_ms": 1735693500000,
        "candle_open_time_ms": 1735689600000,
    }


def test_loads_a_written_result(tmp_path: Path) -> None:
    candidate = make_candidate()
    path = write_signals(tmp_path / "signals.json", [candidate])
    assert load_signals(path) == [candidate]


def test_empty_signal_list_is_valid(tmp_path: Path) -> None:
    path = write_signals(tmp_path / "signals.json", [])
    assert load_signals(path) == []


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SignalFileError, match="cannot read"):
        load_signals(tmp_path / "missing.json")


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "signals.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(SignalFileError, match="invalid JSON"):
        load_signals(path)


def test_non_object_root_is_rejected(tmp_path: Path) -> None:
    path = write_json(tmp_path / "signals.json", [])
    with pytest.raises(SignalFileError, match="JSON object"):
        load_signals(path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(schema_version=2), "schema_version"),
        (lambda payload: payload.update(kind="orders"), "schema_version"),
        (lambda payload: payload.pop("config"), "config object"),
        (lambda payload: payload["config"].update(symbol=""), "symbol and timeframe"),
        (lambda payload: payload.pop("signals"), "signals list"),
        (lambda payload: payload.update(signals={}), "signals list"),
        (lambda payload: payload.update(signals=["nope"]), "JSON object"),
        (lambda payload: payload["signals"][0].update(features=[]), "features"),
        (lambda payload: payload["signals"][0].update(entry_price="zero"), "invalid signal"),
        (lambda payload: payload["signals"][0].pop("signal_id"), "invalid signal"),
        (lambda payload: payload["signals"][0].update(symbol="ETH/USDT"), "does not match"),
        (lambda payload: payload["signals"][0].update(timeframe="4h"), "does not match"),
    ],
)
def test_invalid_signal_files_are_rejected(tmp_path: Path, mutate: Any, message: str) -> None:
    payload = raw_payload()
    mutate(payload)
    path = write_json(tmp_path / "signals.json", payload)
    with pytest.raises(SignalFileError, match=message):
        load_signals(path)
