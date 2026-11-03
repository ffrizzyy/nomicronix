"""Deterministic JSON serialization for signal scans."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from nomicronix.strategy_engine.models import (
    SignalBatch,
    SignalCandidate,
    decimal_text,
)


def _config_payload(batch: SignalBatch) -> dict[str, Any]:
    config = batch.config
    return {
        "symbol": config.symbol,
        "timeframe": config.timeframe,
        "start_ms": config.start_ms,
        "end_ms": config.end_ms,
        "min_history": config.min_history,
        "min_confidence": config.min_confidence,
        "ttl_ms": config.ttl_ms,
        "max_signals": config.max_signals,
    }


def _signal_payload(signal: SignalCandidate) -> dict[str, Any]:
    return {
        "signal_id": signal.signal_id,
        "strategy_id": signal.strategy_id,
        "symbol": signal.symbol,
        "timeframe": signal.timeframe,
        "side": signal.side.value,
        "entry_price": decimal_text(signal.entry_price),
        "stop_price": decimal_text(signal.stop_price),
        "target_price": decimal_text(signal.target_price),
        "confidence": signal.confidence,
        "rationale": signal.rationale,
        "features": {key: signal.features[key] for key in sorted(signal.features)},
        "created_at_ms": signal.created_at_ms,
        "expires_at_ms": signal.expires_at_ms,
        "candle_open_time_ms": signal.candle_open_time_ms,
    }


def to_payload(batch: SignalBatch) -> dict[str, Any]:
    config = batch.config
    return {
        "schema_version": 1,
        "kind": "signals",
        "config": _config_payload(batch),
        "data": {
            "symbol": config.symbol,
            "timeframe": config.timeframe,
            "first_candle_ms": batch.first_candle_ms,
            "last_candle_ms": batch.last_candle_ms,
            "candle_count": batch.candle_count,
        },
        "signals": [_signal_payload(signal) for signal in batch.signals],
        "warnings": list(batch.warnings),
    }


def write_result(
    batch: SignalBatch,
    path: str | Path,
    *,
    force: bool = False,
) -> Path:
    destination = Path(path)
    if destination.exists() and not force:
        raise FileExistsError(f"{destination} already exists; pass --force to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_payload(batch), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def summary(batch: SignalBatch) -> str:
    lines = [
        f"strategy: {batch.strategy_id}",
        f"symbol: {batch.config.symbol} {batch.config.timeframe}",
        f"candles: {batch.candle_count}",
        f"signals: {len(batch.signals)}",
    ]
    for signal in batch.signals:
        lines.append(
            f"- {signal.signal_id} entry={decimal_text(signal.entry_price)} "
            f"stop={decimal_text(signal.stop_price)} "
            f"target={decimal_text(signal.target_price)} "
            f"confidence={signal.confidence:.3f}"
        )
    for warning in batch.warnings:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)
