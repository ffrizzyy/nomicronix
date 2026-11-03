from pathlib import Path

import pytest

from nomicronix.strategy_engine.config import ConfigError, ScanDefaults, load_scan_defaults


def write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_default_config_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_scan_defaults() == ScanDefaults()


def test_missing_explicit_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_scan_defaults(tmp_path / "missing.toml")


def test_valid_config_overrides_defaults(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "strategy.toml",
        '[scan]\nmin_confidence = "0.75"\nttl_ms = 60000\nmin_history = 10\nmax_signals = 5\n',
    )
    defaults = load_scan_defaults(path)
    assert defaults == ScanDefaults(
        min_confidence=0.75,
        ttl_ms=60_000,
        min_history=10,
        max_signals=5,
    )


def test_repository_config_loads() -> None:
    defaults = load_scan_defaults(Path("config/strategy.toml"))
    assert defaults == ScanDefaults()


def test_malformed_toml_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path / "strategy.toml", "[scan\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_scan_defaults(path)


def test_missing_scan_table_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path / "strategy.toml", "[other]\nx = 1\n")
    with pytest.raises(ConfigError, match=r"\[scan\] table"):
        load_scan_defaults(path)


@pytest.mark.parametrize(
    "body",
    [
        '[scan]\nmin_confidence = "1.5"\n',
        '[scan]\nmin_confidence = "-0.1"\n',
        "[scan]\nttl_ms = 0\n",
        "[scan]\nmin_history = 0\n",
        "[scan]\nmax_signals = 0\n",
        '[scan]\nttl_ms = "soon"\n',
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, body: str) -> None:
    path = write_config(tmp_path / "strategy.toml", body)
    with pytest.raises(ConfigError):
        load_scan_defaults(path)
