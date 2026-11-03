# Spec: backtesting

Module id: `backtesting` (depends on: market-data)

## Objective

Answer one question honestly: would this strategy have made money after real
costs, and does it still work on data it was not tuned on? The module replays
stored candles through a strategy, simulates long-only spot fills with fees and
slippage, and reports an equity curve plus risk-adjusted metrics. Walk-forward
validation separates parameter selection from out-of-sample evaluation.

**Consumers:** the owner via the CLI, and `strategy-engine` as a library when it
needs to evaluate candidate strategies. Results are research evidence, never a
live trading signal.

**User stories**

- As the owner, I can backtest a strategy over a symbol, timeframe and date range
  and get a summary I can compare across runs.
- As the owner, I can run a parameter grid walk-forward and see out-of-sample
  performance, not just the best in-sample number.
- As `strategy-engine`, I can call `run_backtest` and `run_walk_forward` with a
  strategy object and receive structured results.
- As the owner, I can trust that no future candle influenced any decision and
  that fees and slippage were charged on every fill.

**Out of scope for this module:** live trading, exchange connectivity, order
book or partial-fill simulation, short selling, leverage, futures, margin,
multi-symbol portfolio accounting, tax, and AI strategy generation.

**Assumptions**

- One symbol and one timeframe per run. Multi-symbol portfolio backtests are a
  later revision (see Open Questions).
- Long-only spot. A run is either flat or long one position at a time.
- Signals are evaluated at a candle close and filled at the **next** candle open.
  This is the primary defense against lookahead.
- Stops and targets are checked intrabar. If both are touched in one candle, the
  stop is assumed to fill first. This is deliberately conservative.
- Gaps in the requested candle range are a hard error, not a warning.
- The module does not import `risk-engine` in v1. It uses a fixed-fraction
  position sizer and documents that live risk limits are not simulated. A
  `SizingPolicy` protocol leaves room for a future adapter.

## Tech Stack

- Python 3.12
- `nomicronix.market_data` (`MarketDataStore`, `Candle`, `Timeframe`)
- Standard library only: `decimal`, `dataclasses`, `enum`, `json`, `math`,
  `statistics`, `datetime`
- `pytest`, `pytest-cov`, `ruff`, `mypy --strict`
- `uv` for dependency and environment management
- No `pandas`, `numpy` or other new runtime dependencies

## Commands

```
Install:       uv sync
Backtest:      uv run python -m nomicronix.backtesting run --strategy mypkg.strategies:SmaCross --param fast=20 --param slow=50 --symbol BTC/USDT --timeframe 1h --since 2025-01-01 --until 2026-01-01 --cash 1000 --fee-bps 10 --slippage-bps 5 --out results/btc-sma.json
Walk-forward:  uv run python -m nomicronix.backtesting walk-forward --strategy mypkg.strategies:SmaCross --grid fast=10,20,50 --grid slow=50,100 --symbol BTC/USDT --timeframe 1h --since 2025-01-01 --until 2026-01-01 --train 90d --test 30d --step 30d --objective sharpe --out results/btc-sma-wf.json
Test:          uv run pytest tests/backtesting -m "not network" --cov=nomicronix.backtesting --cov-fail-under=90
Lint:          uv run ruff check src tests --fix
Format:        uv run ruff format src tests
Types:         uv run mypy src/nomicronix/backtesting
```

There are no network tests. The module reads only from a `MarketDataStore`.

CLI exit codes: `0` success, `1` invalid config or data error, `2` strategy or
signal error, `3` output file already exists without `--force`.

## Project Structure

```
src/nomicronix/backtesting/
  models.py       -> Action, Signal, Trade, EquityPoint, SimEvent, BacktestConfig,
                     BacktestResult, Metrics, WalkForwardConfig, WalkForwardResult
  strategy.py     -> Strategy protocol, StrategyFactory
  sizing.py       -> SizingPolicy protocol, FixedFractionSizer
  broker.py       -> simulated broker: fills, fees, slippage, stops, targets, cash
  engine.py       -> candle event loop, history window, no-lookahead enforcement
  metrics.py      -> pure metric functions over trades and the equity curve
  walk_forward.py -> window splitting, parameter grid, selection, OOS stitching
  data.py         -> load and validate candles from MarketDataStore
  results.py      -> deterministic JSON serialization, atomic write, overwrite guard
  __main__.py     -> run and walk-forward commands
tests/backtesting/
  fakes.py        -> candle builders, scripted strategies, spy strategy
  strategies.py   -> SMA cross, buy-and-hold and always-flat test strategies
  test_broker.py
  test_engine.py
  test_metrics.py
  test_walk_forward.py
  test_data.py
  test_results.py
  test_cli.py
```

## Interface Contract (what consumers depend on)

```python
class Action(StrEnum):
    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"


@dataclass(frozen=True, slots=True)
class Signal:
    action: Action
    stop_price: Decimal | None = None   # required for ENTER_LONG
    target_price: Decimal | None = None  # optional take-profit
    reason: str = ""


class Strategy(Protocol):
    def on_candle(self, candle: Candle, history: Sequence[Candle]) -> Signal | None:
        """Return a signal at this candle's close, or None.

        `history` is a read-only sequence ending at `candle`; it never contains a
        later candle. Strategies may keep their own state and must be reset by
        constructing a fresh instance for each run or walk-forward window.
        """
        ...


StrategyFactory = Callable[[Mapping[str, object]], Strategy]


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    symbol: str
    timeframe: str
    start_ms: int
    end_ms: int
    initial_cash: Decimal = Decimal("1000")
    fee_bps: Decimal = Decimal("10")
    slippage_bps: Decimal = Decimal("5")
    position_fraction: Decimal = Decimal("0.25")
    min_notional: Decimal = Decimal("10")
    quantity_step: Decimal = Decimal("0.00001")
    min_candles: int = 100


def run_backtest(
    store: MarketDataStore,
    strategy: Strategy,
    config: BacktestConfig,
) -> BacktestResult: ...


def run_walk_forward(
    store: MarketDataStore,
    factory: StrategyFactory,
    param_grid: Mapping[str, Sequence[object]],
    config: BacktestConfig,
    wf: WalkForwardConfig,
) -> WalkForwardResult: ...
```

`BacktestResult` contains the resolved config, the data range actually used, all
closed trades, the per-candle equity curve, every skip/exit event, and the
metrics. It contains no wall-clock timestamp, absolute path or random id, so the
same inputs serialize identically.

## Simulation Rules

**Data**

- The range is half-open: `start_ms <= open_time_ms < end_ms`.
- `store.find_gaps(symbol, timeframe, start_ms, end_ms)` must return no gaps.
  Any gap raises `DataGapError` listing every missing range. No partial result is
  produced and no output file is written.
- Fewer than `min_candles` closed candles raises `InsufficientDataError`.
- Candles are ascending and de-duplicated by the store contract; the engine
  still asserts this before running.

**Signal timing**

- A signal returned while processing candle `i` is actionable at the open of
  candle `i+1`. It is never filled at candle `i`'s close or open.
- `EXIT_LONG` while flat is recorded as `IGNORED_EXIT` and does nothing.
- `ENTER_LONG` while long is recorded as `IGNORED_ENTRY` and does nothing.
- An entry is skipped as `SKIPPED_GAP` if the next open is at or below the stop,
  or at or above the target. Chasing a gap through either level is not modeled.

**Fills and costs**

- Buy fill price: `next_open * (1 + slippage_bps / 10_000)`.
- Sell fill price: `next_open * (1 - slippage_bps / 10_000)`.
- Fee on each side: `notional * fee_bps / 10_000`, deducted from cash.
- Position size: `notional = min(equity * position_fraction, cash)`; quantity is
  floored down to `quantity_step`. If the result is below `min_notional`, the
  entry is skipped as `INSUFFICIENT_CASH`.
- Stop-loss: if `low <= stop`, the sell fill is
  `min(open, stop) * (1 - slippage_bps / 10_000)`. A gap through the stop fills
  at the open, not at the stop.
- Target: if `high >= target`, the sell fill is
  `max(open, target) * (1 - slippage_bps / 10_000)`.
- If both stop and target are touched in the same candle, the stop fills first.
- Stops and targets are active on the entry candle after the entry fill.
- At the end of the data range, an open position is closed at the last close with
  slippage and fee, with exit reason `END_OF_DATA`.
- No interest, dividends, borrow fees or taxes are modeled.

**Accounting**

- Cash starts at `initial_cash`. Buy: `cash -= notional + fee`. Sell:
  `cash += notional - fee`.
- Equity at each candle close: `cash + quantity * close`.
- Only `Decimal` is used for cash, prices, quantities, notional, fees and P&L.
  `float` is allowed only for ratio metrics such as Sharpe.

## Metrics

Every metric is a pure function over the trade list and equity curve, unit-tested
against a hand-computed fixture.

| Metric | Definition |
|---|---|
| Total return | `final_equity / initial_cash - 1` |
| CAGR | compounded annual growth over the elapsed candle time, 365-day year |
| Max drawdown | largest `1 - equity / running_peak` on the equity curve |
| Sharpe | mean per-candle return / sample std * `sqrt(periods_per_year)`, risk-free 0 |
| Sortino | mean per-candle return / downside deviation * `sqrt(periods_per_year)` |
| Calmar | CAGR / max drawdown, `None` when max drawdown is 0 |
| Win rate | winning closed trades / closed trades |
| Profit factor | gross profit / gross loss; `None` when there are no losses |
| Expectancy | net P&L / closed trades |
| Average win / loss | mean P&L of winning and losing trades |
| Fees paid | sum of all fees |
| Turnover | sum of buy and sell notional |
| Exposure | candles with an open position / total candles |
| Trade count | number of closed trades |

Annualization periods: `15m` 35,040; `1h` 8,760; `4h` 2,190; `1d` 365. When the
sample is too short or variance is zero, the ratio is `None` rather than a
misleading infinity. JSON never contains `NaN` or `Infinity`.

## Walk-Forward Validation

- `WalkForwardConfig`: `train_ms`, `test_ms`, `step_ms`, `objective`
  (`sharpe` by default), `min_train_trades` (default 10), `warmup_candles`
  (default 200), `allow_partial` (default false).
- Windows start at the first candle. Train is
  `[t, t + train_ms)`, test is `[t + train_ms, t + train_ms + test_ms)`, then
  `t += step_ms`. Train and test never overlap.
- For every parameter combination, run on the train window. Rank by the
  objective, then total return descending, then trade count ascending, then a
  deterministic parameter-key sort. Parameter selection never sees test data.
- If no combination reaches `min_train_trades`, use the best available and add a
  `NO_QUALIFYING_PARAMS` warning to that window.
- Run the test window with the chosen parameters fixed. Feed up to
  `warmup_candles` of pre-test history so indicators are warm, but suppress all
  signals before `test_start` and start the test with the current OOS equity.
- Compound each test window's return onto the OOS equity. Aggregate metrics are
  computed from the stitched OOS equity curve.
- A final partial test window is dropped unless `allow_partial` is set and it has
  at least `min_candles` candles. Dropped windows are reported, not hidden.
- The result compares mean in-sample and out-of-sample metrics and lists every
  window's chosen parameters. It must never present only the best window.

## Results Format

`--out` writes one JSON document, atomically (temporary file then replace), and
refuses to overwrite unless `--force` is passed. `sort_keys=True` and fixed
decimal formatting make repeated identical runs byte-identical.

```json
{
  "schema_version": 1,
  "kind": "backtest",
  "config": {"symbol": "BTC/USDT", "timeframe": "1h", "fee_bps": "10"},
  "data": {"first_candle_ms": 1735689600000, "last_candle_ms": 1767225600000, "candle_count": 8760},
  "metrics": {"total_return": 0.1234, "max_drawdown": 0.0567, "sharpe": 1.23},
  "trades": [],
  "equity_curve": [{"time_ms": 1735689600000, "equity": "1000"}],
  "events": [],
  "windows": []
}
```

Money and quantities are JSON strings; ratios are JSON numbers or `null`.
`windows` is present only for `kind: "walk_forward"`.

## Code Style

- `Decimal` for money and quantities; `float` only for ratios.
- Metrics are pure functions with no I/O.
- The engine is an explicit loop with no hidden global state. A strategy instance
  belongs to exactly one run.
- Construct fresh strategies for every walk-forward window; never reuse mutated
  state across windows.
- No bare `except`. Data, config and strategy errors are distinct exception
  types with actionable messages.

```python
def apply_slippage(price: Decimal, bps: Decimal, *, buying: bool) -> Decimal:
    """Move the fill price against the trader by `bps` basis points."""
    factor = bps / Decimal(10_000)
    return price * (1 + factor) if buying else price * (1 - factor)
```

## Testing Strategy

| Level | What | Tool | Runs in CI |
|---|---|---|---|
| Unit | fills, fees, slippage, stop gaps, target gaps, stop-before-target, end-of-data close | pytest | yes |
| Unit | every metric against a hand-computed fixture, zero variance, no losses | pytest | yes |
| Component | engine loop with scripted strategies: timing, ignored signals, skip reasons, no lookahead | pytest | yes |
| Component | data validation: gap rejection, empty range, too few candles | pytest | yes |
| Component | walk-forward: window boundaries, no overlap, parameter selection, OOS stitching, partial windows | pytest | yes |
| Golden | a small deterministic run whose trades and metrics are checked into the repo | pytest | yes |
| CLI | JSON output, byte-identical repeat runs, overwrite guard, exit codes | pytest | yes |

- Coverage: at least 90% line coverage for `nomicronix.backtesting`.
- A `SpyStrategy` asserts that `history[-1]` is always the current candle and no
  history entry has a later timestamp. This test is mandatory.
- The golden test exists so fill behavior cannot drift silently after a refactor.
- Bug fixes start with a failing test. No test is skipped or deleted to get green.

## Boundaries

**Always**

- Read candles through `MarketDataStore`; never call an exchange directly.
- Execute signals at the next candle open and keep the history window strictly
  causal.
- Charge fees and adverse slippage on every entry and exit.
- Fail on data gaps by default and name the missing ranges.
- Use `Decimal` for money and UTC milliseconds for time.
- Record every trade, skip, exit reason and window decision.
- Produce deterministic output for identical inputs.

**Ask first**

- Adding short selling, leverage, futures or margin.
- Adding multi-symbol portfolio accounting or shared cash across symbols.
- Changing the fill model, including same-close fills, partial fills, order-book
  simulation or a different intrabar path assumption.
- Adding `pandas`, `numpy` or another runtime dependency.
- Changing the result JSON schema or metric definitions.
- Making `backtesting` depend on `risk-engine` to enforce live limits.
- Using order book, funding or other data beyond OHLCV.

**Never**

- Fetch live data or place orders.
- Let a strategy see a candle later than the one being processed.
- Ignore costs or report gross returns as if they were net.
- Continue silently after a data gap.
- Overwrite an existing result file without `--force`.
- Present in-sample results as evidence of live performance.
- Delete, skip or weaken a failing test without approval.

## Success Criteria

1. Two runs with the same store, strategy, parameters and config produce
   byte-identical JSON, excluding nothing.
2. `SpyStrategy` proves that `history` never contains a candle after the current
   one, and the engine refuses a strategy that returns a signal for a future
   candle.
3. A hand-built two-trade fixture matches expected entry/exit fills, fees,
   slippage and P&L exactly, to the last decimal place.
4. A gap-down through a stop fills at the open, not at the stop price, and a
   candle that touches both stop and target exits via the stop.
5. A missing candle range raises `DataGapError` with the exact ranges, produces
   no result and writes no output file.
6. Every metric in the table matches a hand-computed fixture within `1e-9`;
   zero-variance and no-loss cases return `None`, never `NaN` or `Infinity`.
7. Walk-forward windows never overlap; chosen parameters come only from the
   train window; stitched OOS equity matches a manual compounding calculation.
8. A 12-month `BTC/USDT` `1h` run (8,760 candles) completes in under 10 seconds
   and under 200 MB resident memory on the development machine.
9. `uv run pytest ... --cov-fail-under=90`, `ruff check` and `mypy` all pass.
10. The golden fixture test passes, and `docs/backtesting.md` explains the fill
    model, metric definitions and walk-forward assumptions with examples.

## Open Questions

1. **Single symbol vs portfolio:** v1 runs one symbol per run. Do you want
   multi-symbol portfolio accounting with shared cash before `strategy-engine`
   starts comparing BTC and ETH strategies?
2. **Cost defaults:** are 10 bps fee per side and 5 bps slippage right for
   Binance spot? Fees are easy to configure; slippage should reflect the pairs
   and order sizes you actually intend to trade.
3. **Walk-forward windows:** is 90-day train / 30-day test / 30-day step the
   right starting point for 1h data? A shorter train gives more windows but
   noisier parameters.
4. **Selection objective:** Sharpe is the default. Should it be total return,
   Calmar, or a minimum-trade-count-constrained Sharpe instead?
5. **Risk integration:** should backtests apply `risk-engine`'s 0.5% per-trade,
   3% daily-loss and 10% drawdown limits? That would add a
   `backtesting -> risk-engine` dependency and change the approved build order.
   Current spec uses a fixed 25% notional fraction and does not simulate them.
6. **Position sizing:** is a fixed 25% of equity per trade acceptable for
   research, or should the default be risk-based sizing that matches the live
   engine?
7. **Signal interface:** the strategy receives raw `Candle` history. Do you want
   a richer indicator context object, or is a plain causal sequence enough for
   the first strategy set?
8. **Equity curve size:** the spec stores one point per closed candle. For years
   of 15m data that is large; should the JSON optionally downsample or store
   only trade-level points?
