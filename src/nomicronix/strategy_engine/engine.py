"""The causal scan loop that turns proposer ideas into signal candidates."""

from __future__ import annotations

from collections.abc import Sequence

from nomicronix.market_data.models import Candle, Timeframe
from nomicronix.market_data.store import MarketDataStore
from nomicronix.strategy_engine.data import (
    DataError,
    InsufficientDataError,
    load_candles,
)
from nomicronix.strategy_engine.models import (
    ProposalError,
    ScanConfig,
    Side,
    SignalBatch,
    SignalCandidate,
    SignalIdea,
)
from nomicronix.strategy_engine.proposers import SignalProposer
from nomicronix.strategy_engine.views import CandleView


class ScanError(ValueError):
    """The scan cannot proceed with the supplied candles or policy."""


class SignalLimitError(ScanError):
    """A scan produced more signals than `max_signals` allows."""


def signal_id(strategy_id: str, symbol: str, timeframe: str, candle_open_time_ms: int) -> str:
    return f"{strategy_id}:{symbol}:{timeframe}:{candle_open_time_ms}"


def _candidate(
    idea: SignalIdea,
    *,
    strategy_id: str,
    config: ScanConfig,
    candle: Candle,
) -> SignalCandidate:
    created_at_ms = candle.open_time_ms + Timeframe(config.timeframe).ms
    return SignalCandidate(
        signal_id=signal_id(strategy_id, config.symbol, config.timeframe, candle.open_time_ms),
        strategy_id=strategy_id,
        symbol=config.symbol,
        timeframe=config.timeframe,
        side=Side.BUY,
        entry_price=idea.entry_price,
        stop_price=idea.stop_price,
        target_price=idea.target_price,
        confidence=idea.confidence,
        rationale=idea.rationale,
        features=dict(idea.features),
        created_at_ms=created_at_ms,
        expires_at_ms=created_at_ms + config.ttl_ms,
        candle_open_time_ms=candle.open_time_ms,
    )


def _validate_candles(candles: Sequence[Candle], config: ScanConfig) -> None:
    if not candles:
        raise InsufficientDataError("no candles supplied")
    for previous, current in zip(candles, candles[1:], strict=False):
        if current.open_time_ms <= previous.open_time_ms:
            raise DataError("candles must be strictly ascending and de-duplicated")
    for candle in candles:
        if candle.symbol != config.symbol or candle.timeframe != config.timeframe:
            raise ScanError(
                f"candle {candle.symbol} {candle.timeframe} does not match "
                f"{config.symbol} {config.timeframe}"
            )


def scan_candles(
    candles: Sequence[Candle],
    proposer: SignalProposer,
    config: ScanConfig,
) -> SignalBatch:
    """Run a proposer over an already-loaded, gap-free candle sequence."""
    _validate_candles(candles, config)
    if len(candles) < config.min_history:
        raise InsufficientDataError(
            f"need at least {config.min_history} candles, got {len(candles)}"
        )

    in_window = [
        candle for candle in candles if config.start_ms <= candle.open_time_ms < config.end_ms
    ]
    if not in_window:
        raise InsufficientDataError("no candles inside the requested scan range")

    required = max(config.min_history, proposer.min_history)
    signals: list[SignalCandidate] = []
    warnings: list[str] = []

    for index, candle in enumerate(candles):
        if candle.open_time_ms < config.start_ms:
            continue
        if candle.open_time_ms >= config.end_ms:
            break
        if index + 1 < required:
            continue

        view = CandleView(candles, index + 1)
        try:
            idea = proposer.propose(view)
        except ProposalError as exc:
            warnings.append(f"{candle.open_time_ms}: rejected proposal: {exc}")
            continue
        if idea is None:
            continue
        if idea.confidence < config.min_confidence:
            warnings.append(
                f"{candle.open_time_ms}: confidence {idea.confidence:.3f} "
                f"below {config.min_confidence:.3f}"
            )
            continue

        try:
            candidate = _candidate(
                idea,
                strategy_id=proposer.strategy_id,
                config=config,
                candle=candle,
            )
        except ProposalError as exc:
            warnings.append(f"{candle.open_time_ms}: rejected proposal: {exc}")
            continue

        if len(signals) >= config.max_signals:
            raise SignalLimitError(
                f"scan produced more than {config.max_signals} signals; "
                "narrow the range or raise --max-signals"
            )
        signals.append(candidate)

    return SignalBatch(
        config=config,
        strategy_id=proposer.strategy_id,
        first_candle_ms=candles[0].open_time_ms,
        last_candle_ms=candles[-1].open_time_ms,
        candle_count=len(candles),
        signals=tuple(signals),
        warnings=tuple(warnings),
    )


def scan(
    store: MarketDataStore,
    proposer: SignalProposer,
    config: ScanConfig,
) -> SignalBatch:
    """Load a strict candle range and run a proposer over it."""
    warmup = max(config.min_history, proposer.min_history)
    candles = load_candles(store, config, warmup_candles=warmup)
    return scan_candles(candles, proposer, config)
