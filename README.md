# Nomicronix

Personal AI crypto trading agent, built in strict, auditable modules. It starts as
a signal generator, grows into autonomous execution on a centralized exchange, and
never bypasses its risk layer.

**The core rule: the AI proposes, `risk-engine` decides.** Every signal and every
future order must pass through a deterministic risk check. There is no
"confidence high enough to skip the limits" path.

This repository currently contains the three foundation modules:

| Module | Status | What it does |
|---|---|---|
| `market-data` | Built | Downloads and stores gap-free OHLCV candles from a ccxt exchange |
| `risk-engine` | Built | Sizes positions, enforces daily/drawdown/exposure limits, owns the kill switch |
| `backtesting` | Built | Replays candles through a strategy with fees, slippage and walk-forward validation |
| `strategy-engine` | Not started | AI/ML signal generation |
| `signal-delivery` | Not started | Telegram/log delivery and paper-trade tracking |
| `execution` | Not started | Paper, human-approved, then autonomous order placement |

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
  the modules that eventually need them. The three built modules need no keys:
  market-data uses public endpoints, and risk/backtesting are offline.
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

# 3. Generate a fresh proposal and evaluate it against the risk limits
uv run python examples/make_proposal.py
uv run python -m nomicronix.risk_engine check \
    --proposal data/proposal.json --price BTC/USDT=100

# 4. Backtest a strategy over the stored candles
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
data/                         local SQLite databases (gitignored)
docs/market-data.md           market-data guide
docs/risk-engine.md           risk-engine guide
docs/backtesting.md           backtesting guide
examples/make_proposal.py     writes a fresh, valid proposal for a manual check
examples/proposal.json        static proposal schema sample (timestamp is stale)
examples/strategies.py        SmaCross, BuyAndHold and AlwaysFlat examples
src/nomicronix/
  market_data/                ccxt adapter, SQLite store, backfill, live updater
  risk_engine/                pure rules, sizing, state, SQLite journal, CLI
  backtesting/                broker, engine, metrics, walk-forward, CLI
tests/
  market_data/                offline fakes plus an opt-in network contract test
  risk_engine/
  backtesting/
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

## Development

```bash
uv sync
uv run pytest -m "not network"                    # full offline suite
uv run pytest tests/risk_engine --cov=nomicronix.risk_engine --cov-fail-under=90
uv run pytest tests/backtesting --cov=nomicronix.backtesting --cov-fail-under=90
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
- `execution` does not exist yet. Nothing in this repository can place an order.

## License

No license has been declared yet. Treat the code as private until one is added.
