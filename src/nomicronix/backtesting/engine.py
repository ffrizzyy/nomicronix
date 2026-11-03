"""Deterministic candle-by-candle backtest engine."""

from __future__ import annotations

from nomicronix.backtesting.broker import Broker
from nomicronix.backtesting.data import InsufficientDataError, load_candles
from nomicronix.backtesting.metrics import compute_metrics
from nomicronix.backtesting.models import (
    Action,
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    ExitReason,
    Signal,
    SkipReason,
)
from nomicronix.backtesting.sizing import FixedFractionSizer
from nomicronix.backtesting.strategy import HistoryView, Strategy
from nomicronix.market_data.models import Candle
from nomicronix.market_data.store import MarketDataStore


def run_backtest(
    store: MarketDataStore,
    strategy: Strategy,
    config: BacktestConfig,
) -> BacktestResult:
    candles = load_candles(store, config)
    return run_backtest_on_candles(candles, strategy, config)


def run_backtest_on_candles(
    candles: list[Candle],
    strategy: Strategy,
    config: BacktestConfig,
) -> BacktestResult:
    """Run on an already-loaded, gap-free list. Used by walk-forward windows."""
    if not candles:
        raise InsufficientDataError("no candles supplied")

    broker = Broker(config, FixedFractionSizer(config.position_fraction))
    equity_curve: list[EquityPoint] = []
    pending: Signal | None = None

    for index, candle in enumerate(candles):
        in_window = candle.open_time_ms >= config.start_ms

        if pending is not None:
            if in_window:
                if pending.action is Action.ENTER_LONG:
                    broker.try_open(candle, pending)
                else:
                    broker.try_close(candle, candle.open, ExitReason.SIGNAL)
            pending = None

        if broker.position is not None:
            broker.check_stop_target(candle)

        # The strategy sees warm-up candles so indicators and internal state are
        # primed, but only in-window signals may become orders.
        signal = strategy.on_candle(candle, HistoryView(candles, index + 1))
        if in_window:
            if signal is not None:
                if signal.action is Action.ENTER_LONG:
                    if not _valid_entry(signal, candle):
                        broker.skip(
                            candle,
                            SkipReason.INVALID_SIGNAL,
                            "entry stop/target on the wrong side of the close",
                        )
                    else:
                        pending = signal
                else:
                    pending = signal
            equity_curve.append(broker.equity_point(candle))

    if not equity_curve:
        raise InsufficientDataError("no candles inside the requested trade range")

    last = candles[-1]
    if pending is not None:
        broker.skip(last, SkipReason.END_OF_DATA_PENDING, "signal on the final candle")
    broker.force_close(last)
    equity_curve[-1] = EquityPoint(
        time_ms=last.open_time_ms,
        equity=broker.equity(last.close),
        exposed=False,
    )

    window_candles = [c for c in candles if c.open_time_ms >= config.start_ms]
    first_ms = window_candles[0].open_time_ms
    last_ms = window_candles[-1].open_time_ms
    metrics = compute_metrics(
        trades=broker.trades,
        equity_curve=equity_curve,
        initial_cash=config.initial_cash,
        timeframe=config.timeframe,
    )
    return BacktestResult(
        config=config,
        first_candle_ms=first_ms,
        last_candle_ms=last_ms,
        candle_count=len(window_candles),
        trades=tuple(broker.trades),
        equity_curve=tuple(equity_curve),
        events=tuple(broker.events),
        metrics=metrics,
    )


def _valid_entry(signal: Signal, candle: Candle) -> bool:
    if signal.stop_price is None:
        return False
    if signal.stop_price >= candle.close:
        return False
    if signal.target_price is not None and signal.target_price <= candle.close:
        return False
    return True
