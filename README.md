# Nomicronix

Personal AI crypto trading agent, built in strict, auditable modules. It starts as
a signal generator, grows into autonomous execution on a centralized exchange, and
never bypasses its risk layer.

**The core rule: the AI proposes, `risk-engine` decides.** Every signal and every
future order must pass through a deterministic risk check. There is no
"confidence high enough to skip the limits" path.

This repository currently contains the five foundation modules:

| Module | Status | What it does |
|---|---|---|
| `market-data` | Built | Downloads and stores gap-free OHLCV candles from a ccxt exchange |
| `risk-engine` | Built | Sizes positions, enforces daily/drawdown/exposure limits, owns the kill switch |
| `backtesting` | Built | Replays candles through a strategy with fees, slippage and walk-forward validation |
| `strategy-engine` | Built | Turns stored candles into causal, validated signal candidates |
| `signal-delivery` | Built | Risk-gated alerts plus an honest paper-trade ledger |
| `execution` | Spec written, blocked | Spot order placement, gated on the paper record and human approval |

The full capability map lives in [`SPEC.md`](SPEC.md).

## Safety model

- **Spot only, long only.** No leverage, futures, margin or shorting.
- **Fail closed.** Missing state, stale data, unknown symbols and invalid input all
  reject. There is no permissive fallback.
- **Conservative defaults.** 0.5% risk per trade, 3% daily loss limit, 10% peak
  drawdown kill switch, at most 2 positions, 25% position cap, 50% exposure cap,
  2% aggregate open risk, minimum reward/risk 2.0, 5-minute maximum signal age.
- **Kill switch is sticky.** A drawdown breach engages it automatically. It
  survives restarts and clears only with the exact phrase `RESUME TRADING`.
- **Append-only audit.** Every risk decision, approved or rejected, is journaled
  before it is returned.
- **No secrets in git.** Exchange API keys are read from environment variables by
  the modules that eventually need them. The five built modules need no trading
  keys: market-data uses public endpoints, risk/backtesting/strategy are offline,
  and signal-delivery only reads optional Telegram credentials from the
  environment.
- **Backtests are research.** A backtest result is never a live trading signal.
  Out-of-sample walk-forward metrics are the headline, not the best in-sample
  number.

## Quick start

Requirements: Python 3.12+, [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync

# 1. Download candles (public data, safe to re-run)
uv run python -m nomicronix.market_data backfill \
    --symbols BTC/USDT,ETH/USDT --timeframes 1h --since 2025-01-01

# 2. Inspect the risk account (creates data/risk.sqlite with 1,000 USDT paper cash)
uv run python -m nomicronix.risk_engine status

# 3. Scan the stored candles for causal signal candidates
uv run python -m nomicronix.strategy_engine scan \
    --strategy examples.strategy_models:SmaCross \
    --symbol BTC/USDT --timeframe 1h \
    --since 2025-01-01 --until 2025-02-01 \
    --out results/btc-signals.json

# 4. Deliver the approved signals and open paper trades
uv run python -m nomicronix.signal_delivery deliver \
    --signals results/btc-signals.json \
    --sink console --sink jsonl

# 5. Mark open paper trades at a fresh price
uv run python -m nomicronix.signal_delivery tick --price BTC/USDT=94.50

# 6. Check the execution promotion gate (0 ready, 2 not ready)
uv run python -m nomicronix.signal_delivery readiness

# 7. Generate a fresh proposal and evaluate it against the risk limits
uv run python examples/make_proposal.py
uv run python -m nomicronix.risk_engine check \
    --proposal data/proposal.json --price BTC/USDT=100

# 8. Backtest a strategy over the stored candles
uv run python -m nomicronix.backtesting run \
    --strategy examples.strategies:SmaCross \
    --symbol BTC/USDT --timeframe 1h \
    --since 2025-01-01 --until 2025-02-01 \
    --out results/btc-sma.json
```

`check` exits `0` when approved and `2` when rejected, so scripts can gate on it.

## Repository layout

```
config/risk.toml              committed conservative risk limits
config/strategy.toml          committed scan defaults (confidence, TTL, history)
config/delivery.toml          committed delivery defaults (sinks, filters)
data/                         local SQLite databases (gitignored)
docs/market-data.md           market-data guide
docs/risk-engine.md           risk-engine guide
docs/backtesting.md           backtesting guide
docs/strategy-engine.md       strategy-engine guide
docs/signal-delivery.md       signal-delivery guide
examples/deliver_signals.py   delivers a candidate on paper and prints the report
examples/make_proposal.py     writes a fresh, valid proposal for a manual check
examples/proposal.json        static proposal schema sample (timestamp is stale)
examples/strategies.py        SmaCross, BuyAndHold and AlwaysFlat examples
examples/strategy_models.py   built-in and custom signal proposers
src/nomicronix/
  market_data/                ccxt adapter, SQLite store, backfill, live updater
  risk_engine/                pure rules, sizing, state, SQLite journal, CLI
  backtesting/                broker, engine, metrics, walk-forward, CLI
  strategy_engine/            causal proposers, scan loop, results, CLI
  signal_delivery/            risk-gated outbox, sinks, paper ledger, CLI
tests/
  market_data/                offline fakes plus an opt-in network contract test
  risk_engine/
  backtesting/
  strategy_engine/
  signal_delivery/
tasks/                        implementation plan and task checklist
SPEC*.md                      approved module specs and capability map
```

## Commands

### market-data

```bash
uv run python -m nomicronix.market_data backfill --symbols BTC/USDT --timeframes 1h,15m --since 2025-01-01
uv run python -m nomicronix.market_data live     --symbols BTC/USDT --timeframes 1h,15m
uv run python -m nomicronix.market_data verify   --symbols BTC/USDT --timeframes 1h --since 2025-01-01
```

See [`docs/market-data.md`](docs/market-data.md).

### risk-engine

```bash
uv run python -m nomicronix.risk_engine status
uv run python -m nomicronix.risk_engine check  --proposal proposal.json --price BTC/USDT=100
uv run python -m nomicronix.risk_engine mark   --prices prices.json
uv run python -m nomicronix.risk_engine kill   --reason "manual stop"
uv run python -m nomicronix.risk_engine resume --confirm "RESUME TRADING"
```

Defaults come from [`config/risk.toml`](config/risk.toml). See
[`docs/risk-engine.md`](docs/risk-engine.md) for the rule order, sizing math and
persistence contract.

### backtesting

```bash
# Single backtest
uv run python -m nomicronix.backtesting run \
    --strategy examples.strategies:SmaCross --param fast=20 --param slow=50 \
    --symbol BTC/USDT --timeframe 1h \
    --since 2025-01-01 --until 2026-01-01 \
    --fee-bps 10 --slippage-bps 5 \
    --out results/btc-sma.json

# Walk-forward parameter selection
uv run python -m nomicronix.backtesting walk-forward \
    --strategy examples.strategies:SmaCross \
    --grid fast=10,20,50 --grid slow=50,100 \
    --symbol BTC/USDT --timeframe 1h \
    --since 2025-01-01 --until 2026-01-01 \
    --train 90d --test 30d --step 30d --objective sharpe \
    --out results/btc-sma-wf.json
```

Results are deterministic JSON. Existing output is never overwritten without
`--force`. See [`docs/backtesting.md`](docs/backtesting.md) for the fill model,
cost assumptions and walk-forward rules.

### strategy-engine

```bash
# Built-in SMA crossover
uv run python -m nomicronix.strategy_engine scan \
    --strategy examples.strategy_models:SmaCross \
    --param fast=20 --param slow=50 \
    --symbol BTC/USDT --timeframe 1h \
    --since 2025-01-01 --until 2025-02-01 \
    --min-confidence 0.5 --ttl 5m \
    --out results/btc-signals.json

# Built-in Donchian breakout
uv run python -m nomicronix.strategy_engine scan \
    --strategy examples.strategy_models:DonchianBreakout \
    --symbol BTC/USDT --timeframe 1h \
    --since 2025-01-01 --until 2025-02-01 \
    --out results/btc-donchian.json
```

Signals are proposals, not orders. Every candidate must still pass
`risk-engine`. See [`docs/strategy-engine.md`](docs/strategy-engine.md) for the
proposer contract, causality rules and output schema.

### signal-delivery

```bash
# Risk-check every signal at its candle close, then alert and paper-fill
uv run python -m nomicronix.signal_delivery deliver \
    --signals results/btc-signals.json \
    --sink console --sink jsonl --jsonl data/deliveries.jsonl

# Optional Telegram sink; credentials are environment-only
export NOMICRONIX_TELEGRAM_BOT_TOKEN=...
export NOMICRONIX_TELEGRAM_CHAT_ID=...
uv run python -m nomicronix.signal_delivery deliver \
    --signals results/btc-signals.json --sink telegram

# Mark open paper trades at a price, then report delivery and P&L
uv run python -m nomicronix.signal_delivery tick --price BTC/USDT=94.50
uv run python -m nomicronix.signal_delivery report

# Check whether the paper record is strong enough to build execution
uv run python -m nomicronix.signal_delivery readiness
```

Rejected signals are recorded in the outbox but never sent as trade alerts, and
re-running the same file never duplicates a delivery or a position. Paper fills
are full and fee-free. See
[`docs/signal-delivery.md`](docs/signal-delivery.md) for the outbox, replay and
paper-trade rules.

## Development

```bash
uv sync
uv run pytest -m "not network"                    # full offline suite
uv run pytest tests/risk_engine --cov=nomicronix.risk_engine --cov-fail-under=90
uv run pytest tests/backtesting --cov=nomicronix.backtesting --cov-fail-under=90
uv run pytest tests/strategy_engine --cov=nomicronix.strategy_engine --cov-fail-under=90
uv run pytest tests/signal_delivery --cov=nomicronix.signal_delivery --cov-fail-under=90
uv run ruff format --check src tests examples
uv run ruff check src tests examples
uv run mypy src/nomicronix
uv run pytest tests/market_data -m network        # opt-in, hits a real exchange
```

CI runs the offline suite on Python 3.12 and 3.13, with formatting, lint, strict
typing and a coverage floor. No CI job needs exchange credentials.

## Known limitations

- `market-data` uses SQLite in its default journal mode. Use one writer process per
  database file until WAL is enabled for that module.
- `backtesting` handles one symbol and one timeframe per run, long only, with full
  fills. It does not simulate partial fills or `risk-engine` limits; it uses a
  fixed 25% notional sizer. Out-of-sample results are still research evidence.
- `risk-engine` v1 consumes one full fill per approval. Partial fills are deferred
  to `execution`.
- `strategy-engine` has no portfolio state: a signal can fire while a position is
  already open, and duplicate/exposure rules decide downstream. Its confidences
  are fixed rule scores, not calibrated probabilities, and no AI provider is
  wired yet.
- `signal-delivery` paper fills are full and fee-free at the planned price, with
  stops checked before targets. It has no scheduler or retry daemon, and Telegram
  is its only remote sink. Its `readiness` command gates `execution` on at least
  30 closed trades over 28 days with positive expectancy after assumed costs.
- `execution` does not exist yet. It cannot be built until the `readiness` gate
  passes and its own spec is approved. Nothing in this repository can place an
  order.

## License

MIT. See [`LICENSE`](LICENSE). The software is provided "as is": no warranty,
no promise of profit, and no liability for trading losses.
