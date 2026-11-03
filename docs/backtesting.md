# backtesting

Honest strategy simulation. The module answers one question: would this strategy
have made money after real costs, and does it still work on data it was not tuned
on?

It replays stored candles through a strategy, simulates long-only spot fills with
fees and slippage, and reports an equity curve plus risk-adjusted metrics.
Walk-forward validation keeps parameter selection on the training window and
evaluates the frozen parameters out of sample.

Backtest output is research evidence. It is never a live trading signal.

## Concepts

- **Candle:** one closed OHLCV bar from `market-data`. Prices are `Decimal`, times
  are UTC epoch milliseconds. Ranges are half-open:
  `start_ms <= open_time_ms < end_ms`.
- **Strategy:** any object with
  `on_candle(candle, history) -> Signal | None`. It returns a signal at the
  candle's close. Construct a fresh instance for every run or walk-forward window.
- **History view:** a read-only `Sequence[Candle]` ending at the current candle.
  It physically cannot expose a later candle.
- **Signal:** `ENTER_LONG` with a required stop and optional target, or
  `EXIT_LONG`.
- **Broker:** simulated cash, one open long position, fees, slippage, stops and
  targets.
- **Result:** config, data range, trades, per-candle equity curve, simulation
  events and metrics.

The module uses the standard library only. It reads candles through the
`MarketDataStore` protocol and never calls an exchange.

## The strategy contract

```python
from collections.abc import Sequence

from nomicronix.backtesting.models import Action, Signal
from nomicronix.market_data.models import Candle


class MyStrategy:
    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> Signal | None:
        if len(history) < 50:
            return None
        # `history[-1]` is always `candle`; no later candle is visible.
        ...
        return Signal(
            Action.ENTER_LONG,
            stop_price=candle.close * 0.95,
            target_price=candle.close * 1.10,
            reason="breakout",
        )
```

`examples/strategies.py` contains `SmaCross`, `BuyAndHold` and `AlwaysFlat`.

A signal produced while processing candle `i` is filled at the **open of candle
`i+1`**. It is never filled at candle `i`'s close or open.

## Data requirements

- One symbol and one timeframe per run.
- A gap anywhere in the requested range (plus warm-up) aborts the run with
  `DataGapError`, listing every missing range. No partial result is produced.
- Fewer than `min_candles` candles raises `InsufficientDataError`.
- Candles must be strictly ascending and de-duplicated. The store guarantees this;
  the loader checks it anyway.
- Warm-up candles are loaded before `start_ms`. The strategy sees them so
  indicators are primed, but signals before `start_ms` are discarded.

`BacktestConfig` defaults: 1,000 USDT cash, 10 bps fee per side, 5 bps adverse
slippage, 25% of equity per position, 10 USDT minimum notional, `0.00001`
quantity step, 100 minimum candles, 0 warm-up candles.

## Fill and cost model

**Entry**

- Buy fill: `next_open * (1 + slippage_bps / 10_000)`.
- Fee: `notional * fee_bps / 10_000`, deducted from cash.
- Desired notional: `min(equity * position_fraction, cash)`; quantity is floored
  down to `quantity_step`.
- If quantity is zero or notional is below `min_notional`, the entry is skipped as
  `INSUFFICIENT_CASH`.
- If the next open is at or below the stop, or at or above the target, the entry
  is skipped as `SKIPPED_GAP`. The simulator does not chase a gap through either
  level.
- An entry while already long is `IGNORED_ENTRY`; an exit while flat is
  `IGNORED_EXIT`.

**Exit**

- Sell fill: `exit_price * (1 - slippage_bps / 10_000)`.
- Signal exit: the next open after the signal candle.
- Stop: if `low <= stop`, the reference price is `min(open, stop)`. A gap through
  the stop fills at the open, not at the stop.
- Target: if `high >= target`, the reference price is `max(open, target)`.
- If both stop and target are touched in one candle, **the stop fills first**. This
  is deliberately conservative.
- Stops and targets become active on the entry candle after the entry fill.
- At the end of the data range an open position is closed at the last close with
  slippage and fee, exit reason `END_OF_DATA`.

**Accounting**

- Cash starts at `initial_cash`. Buy: `cash -= notional + fee`. Sell:
  `cash += notional - fee`.
- Equity at each candle close: `cash + quantity * close`.
- Only `Decimal` is used for cash, prices, quantities, notional, fees and P&L.
  `float` is used only for ratio metrics.
- No interest, dividends, borrow fees or taxes are modeled.

## Metrics

| Metric | Definition |
|---|---|
| Total return | `final_equity / initial_cash - 1` |
| CAGR | Compounded annual growth over elapsed candle time, 365-day year |
| Max drawdown | Largest `1 - equity / running_peak` on the equity curve |
| Sharpe | Mean per-candle return / sample standard deviation × `sqrt(periods_per_year)`, risk-free 0 |
| Sortino | Mean per-candle return / downside deviation × `sqrt(periods_per_year)` |
| Calmar | CAGR / max drawdown; `None` when max drawdown is 0 |
| Win rate | Winning closed trades / closed trades |
| Profit factor | Gross profit / gross loss; `None` when there are no losses |
| Expectancy | Net P&L / closed trades |
| Average win / loss | Mean P&L of winning and losing trades |
| Fees paid | Sum of all fees |
| Turnover | Sum of buy and sell notional |
| Exposure | Candle closes with an open position / total candle closes |
| Trade count | Number of closed trades |

Annualization periods: `15m` 35,040; `1h` 8,760; `4h` 2,190; `1d` 365.

When the sample is too short or variance is zero, a ratio is `None`, never `NaN` or
`Infinity`.

## Walk-forward validation

`WalkForwardConfig` defaults: 90-day train, 30-day test, 30-day step, `sharpe`
objective, 10 minimum training trades, 200 warm-up candles, partial windows
disabled.

- Windows start at `start_ms`. Train is `[t, t + train_ms)`, test is
  `[t + train_ms, t + train_ms + test_ms)`, then `t += step_ms`.
- `step_ms` must be at least `test_ms`; otherwise windows would overlap and the
  run is rejected.
- For every parameter combination, run on the train window. Rank by the objective,
  then total return descending, then trade count ascending, then a deterministic
  parameter-key sort. Parameter selection never sees test data.
- If no combination reaches `min_train_trades`, use the best available and add a
  `NO_QUALIFYING_PARAMS` warning to that window. The warning is also surfaced at
  the top level as `window N: NO_QUALIFYING_PARAMS`.
- Run the test window with the chosen parameters fixed. Load up to
  `warmup_candles` of pre-test history so indicators are warm, suppress all signals
  before `test_start`, and start the test from the current out-of-sample equity.
- Compound each test window's final equity into the next window. Aggregate metrics
  are computed from the stitched OOS equity curve.
- A final partial test window is dropped unless `allow_partial` is set and it has
  at least `min_candles` candles.

The result reports every window's chosen parameters, per-window train and test
metrics, mean in-sample metrics, OOS trades, OOS equity and warnings. It never
presents only the best window.

## Commands

### Single backtest

```bash
uv run python -m nomicronix.backtesting run \
    --strategy examples.strategies:SmaCross \
    --param fast=20 --param slow=50 \
    --symbol BTC/USDT --timeframe 1h \
    --since 2025-01-01 --until 2025-02-01 \
    --fee-bps 10 --slippage-bps 5 \
    --out results/btc-sma.json
```

### Walk-forward

```bash
uv run python -m nomicronix.backtesting walk-forward \
    --strategy examples.strategies:SmaCross \
    --grid fast=10,20,50 --grid slow=50,100 \
    --symbol BTC/USDT --timeframe 1h \
    --since 2025-01-01 --until 2026-01-01 \
    --train 90d --test 30d --step 30d --objective sharpe \
    --min-train-trades 10 --warmup-candles 200 \
    --out results/btc-sma-wf.json
```

Common flags: `--db` (default `data/market.sqlite`), `--exchange` (default
`binance`), `--cash`, `--fee-bps`, `--slippage-bps`, `--position-fraction`,
`--min-notional`, `--quantity-step`, `--min-candles`, `--out`, `--force`.

`--strategy` is `module:Class`. Parameters are parsed as integer, then float, then
string. Grid values are comma-separated and duplicate grid keys are rejected.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Invalid config, data error or malformed argument |
| `2` | Strategy import or constructor error |
| `3` | Output file exists and `--force` was not passed |

## Result format

`--out` writes one JSON document atomically (temporary file, then replace) and
refuses to overwrite unless `--force` is passed. `sort_keys=True` and fixed decimal
formatting make identical runs byte-identical.

```json
{
  "schema_version": 1,
  "kind": "backtest",
  "config": {
    "symbol": "BTC/USDT",
    "timeframe": "1h",
    "start_ms": 1735689600000,
    "end_ms": 1738368000000,
    "initial_cash": "1000",
    "fee_bps": "10",
    "slippage_bps": "5",
    "position_fraction": "0.25",
    "min_notional": "10",
    "quantity_step": "0.00001",
    "min_candles": 100,
    "warmup_candles": 0
  },
  "data": {
    "symbol": "BTC/USDT",
    "timeframe": "1h",
    "first_candle_ms": 1735689600000,
    "last_candle_ms": 1738364400000,
    "candle_count": 744
  },
  "metrics": {"total_return": 0.0123, "max_drawdown": 0.0456, "sharpe": 1.23},
  "trades": [],
  "equity_curve": [{"time_ms": 1735689600000, "equity": "1000", "exposed": false}],
  "events": []
}
```

Money and quantities are JSON strings; ratios are JSON numbers or `null`.
`kind: "walk_forward"` replaces `data`/`trades`/`equity_curve` with
`walk_forward`, `param_grid`, `windows`, `oos_trades`, `oos_equity_curve`,
`in_sample_mean` and `warnings`. JSON never contains `NaN` or `Infinity`.

## Known limitations

- **One symbol, one timeframe.** No shared cash across symbols.
- **Long only, one position at a time.** No shorting, leverage, margin or futures.
- **Full fills only.** No partial fills or order-book simulation.
- **Next-open fills only.** Same-close fills are not modeled.
- **Stop-first assumption.** When both levels are touched in one candle, the stop
  wins even if the target was hit first in reality.
- **No `risk-engine` integration.** The simulator uses a fixed 25% notional sizer
  and does not apply live daily-loss, drawdown or aggregate-open-risk limits. A
  `SizingPolicy` protocol leaves room for a future adapter.
- **Costs are estimates.** 10 bps fee and 5 bps slippage are starting points, not a
  claim about your actual fills.
- **In-sample numbers are not evidence.** Use the walk-forward OOS metrics.

## Development

```bash
uv run pytest tests/backtesting --cov=nomicronix.backtesting --cov-fail-under=90
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/nomicronix/backtesting
```

The test suite includes a causal spy strategy, a hand-computed fee/slippage
fixture, stop/target gap cases, metric fixtures, walk-forward window and
parameter-selection tests, deterministic JSON checks and CLI exit-code tests.

## Decisions taken for the spec's open questions

| Question | Decision |
|---|---|
| Single symbol vs portfolio | One symbol and timeframe per run; portfolio accounting later |
| Cost defaults | 10 bps fee per side, 5 bps adverse slippage, configurable |
| Walk-forward windows | 90-day train / 30-day test / 30-day step |
| Selection objective | Sharpe by default; total return and Calmar selectable |
| Risk integration | Not in v1; fixed 25% notional sizer, documented limitation |
| Position sizing | Fixed fraction of equity via `SizingPolicy` |
| Signal interface | Raw causal `Candle` history sequence |
| Equity curve size | One point per closed candle; JSON is the source of record |
