"""Read and validate a strategy-engine signal result file."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from nomicronix.signal_delivery.models import SignalFileError
from nomicronix.strategy_engine.models import Side, SignalCandidate


def _candidate(raw: Any, symbol: str, timeframe: str) -> SignalCandidate:
    if not isinstance(raw, dict):
        raise SignalFileError("each signal must be a JSON object")
    try:
        features_raw = raw.get("features", {})
        if not isinstance(features_raw, dict):
            raise SignalFileError("signal features must be an object")
        candidate = SignalCandidate(
            signal_id=str(raw["signal_id"]),
            strategy_id=str(raw["strategy_id"]),
            symbol=str(raw["symbol"]),
            timeframe=str(raw["timeframe"]),
            side=Side(str(raw["side"])),
            entry_price=Decimal(str(raw["entry_price"])),
            stop_price=Decimal(str(raw["stop_price"])),
            target_price=Decimal(str(raw["target_price"])),
            confidence=float(raw["confidence"]),
            rationale=str(raw["rationale"]),
            features={str(key): str(value) for key, value in features_raw.items()},
            created_at_ms=int(raw["created_at_ms"]),
            expires_at_ms=int(raw["expires_at_ms"]),
            candle_open_time_ms=int(raw["candle_open_time_ms"]),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise SignalFileError(f"invalid signal: {exc}") from exc

    if candidate.symbol != symbol or candidate.timeframe != timeframe:
        raise SignalFileError(f"signal {candidate.signal_id} does not match {symbol} {timeframe}")
    return candidate


def load_signals(path: str | Path) -> list[SignalCandidate]:
    """Load a `schema_version = 1`, `kind = "signals"` file."""
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SignalFileError(f"cannot read {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SignalFileError(f"invalid JSON in {source}: {exc}") from exc

    if not isinstance(data, dict):
        raise SignalFileError("signal file must contain a JSON object")
    if data.get("schema_version") != 1 or data.get("kind") != "signals":
        raise SignalFileError("signal file must be schema_version 1 and kind 'signals'")

    config = data.get("config")
    if not isinstance(config, dict):
        raise SignalFileError("signal file is missing its config object")
    symbol = str(config.get("symbol", ""))
    timeframe = str(config.get("timeframe", ""))
    if not symbol or not timeframe:
        raise SignalFileError("signal config needs a symbol and timeframe")

    signals = data.get("signals")
    if not isinstance(signals, list):
        raise SignalFileError("signal file is missing its signals list")

    return [_candidate(raw, symbol, timeframe) for raw in signals]
