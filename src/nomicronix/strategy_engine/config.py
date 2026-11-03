"""Load and validate scan defaults from TOML."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from nomicronix.strategy_engine.models import (
    DEFAULT_MAX_SIGNALS,
    DEFAULT_MIN_CONFIDENCE,
    DEFAULT_MIN_HISTORY,
    DEFAULT_TTL_MS,
)

DEFAULT_CONFIG_PATH = Path("config/strategy.toml")


class ConfigError(ValueError):
    """The strategy configuration is missing, malformed or outside safe bounds."""


@dataclass(frozen=True, slots=True)
class ScanDefaults:
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    ttl_ms: int = DEFAULT_TTL_MS
    min_history: int = DEFAULT_MIN_HISTORY
    max_signals: int = DEFAULT_MAX_SIGNALS

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be inside [0, 1]")
        if self.ttl_ms <= 0:
            raise ValueError("ttl_ms must be positive")
        if self.min_history < 1:
            raise ValueError("min_history must be at least 1")
        if self.max_signals < 1:
            raise ValueError("max_signals must be at least 1")


def _confidence(section: dict[str, Any], default: float) -> float:
    raw = section.get("min_confidence", default)
    return float(Decimal(str(raw)))


def load_scan_defaults(path: str | Path | None = None) -> ScanDefaults:
    config_path = DEFAULT_CONFIG_PATH if path is None else Path(path)
    if not config_path.exists():
        if path is None:
            return ScanDefaults()
        raise ConfigError(f"strategy config not found: {config_path}")

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    section = data.get("scan")
    if not isinstance(section, dict):
        raise ConfigError("config must contain a [scan] table")

    default = ScanDefaults()
    try:
        return ScanDefaults(
            min_confidence=_confidence(section, default.min_confidence),
            ttl_ms=int(section.get("ttl_ms", default.ttl_ms)),
            min_history=int(section.get("min_history", default.min_history)),
            max_signals=int(section.get("max_signals", default.max_signals)),
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ConfigError(f"invalid strategy config in {config_path}: {exc}") from exc
