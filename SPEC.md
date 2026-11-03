# Capability Map: Nomicronix

Personal AI crypto trading agent. Starts as a signal generator, grows into
autonomous execution on a centralized exchange within strict risk limits.

| Module id | Responsibility | Depends on | Spec | Status |
|---|---|---|---|---|
| market-data | Historical and live OHLCV, order book and funding data via a unified exchange adapter | none | SPEC-market-data.md | Built and tested (see [docs/market-data.md](docs/market-data.md)) |
| backtesting | Strategy simulation with fees, slippage and walk-forward validation | market-data | SPEC-backtesting.md | Built and tested (see [docs/backtesting.md](docs/backtesting.md)) |
| risk-engine | Position sizing, max drawdown and daily loss limits, kill switch. Deterministic code, no AI | none | SPEC-risk-engine.md | Built and tested (see [docs/risk-engine.md](docs/risk-engine.md)) |
| strategy-engine | AI/ML signal generation: signal, confidence, rationale | market-data | SPEC-strategy-engine.md | Built and tested (see [docs/strategy-engine.md](docs/strategy-engine.md)) |
| signal-delivery | Telegram and log delivery, paper-trade P&L tracking | strategy-engine, risk-engine | SPEC-signal-delivery.md | Built and tested (see [docs/signal-delivery.md](docs/signal-delivery.md)) |
| execution | Order placement in stages: paper, human-approved, autonomous within risk limits | risk-engine, signal-delivery | SPEC-execution.md | Not started |

**Build order:** market-data → backtesting, risk-engine → strategy-engine → signal-delivery → execution

## Project-wide decisions (apply to every module spec)

- Language and tooling: Python 3.12, `ccxt`, `pytest`, SQLite.
- First exchange: Binance, behind a generic adapter so Coinbase and Bybit can be added later.
- Spot only. No leverage, no futures, until explicitly added to a module spec.
- Module ids are stable and never renamed.
- Every signal and every future order must pass through `risk-engine`. The AI proposes, risk limits decide.
- `execution` is not built until `signal-delivery` has a paper-trading record that meets criteria defined in its spec.
- Exchange API keys are trade-only with withdrawals disabled, loaded from environment variables, never committed.
