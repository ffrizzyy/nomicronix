# Implementation Plan: market-data

Source spec: `SPEC-market-data.md` (approved). Module id: `market-data`.
Task list lives in `tasks/todo.md`.

## Overview

Build the data foundation for Nomicronix: a SQLite-backed store of closed OHLCV
candles, filled by a resumable backfill and kept current by a live updater,
behind a small `MarketDataStore` interface that `backtesting` and
`strategy-engine` will consume. Public endpoints only, no API keys.

## Defaults used for unanswered open questions

The spec's open questions were not answered, so the plan proceeds with the
spec defaults. Change any of these before Task 1 and the plan adapts cheaply.

| Question | Default used |
|---|---|
| Symbols and timeframes | `BTC/USDT`, `ETH/USDT` on `1h` and `15m` |
| History depth | Since 2025-01-01 |
| Exchange | Binance public API (adapter is generic) |
| Live mode | REST polling at candle close, no websockets |
| Tooling | `uv` |

## Architecture Decisions

- **Adapter first, to fail fast.** The riskiest unknown is whether the exchange
  API is reachable and behaves as expected, so the real `ccxt` adapter and its
  contract test come right after scaffolding, before any storage logic.
- **Fake adapter for everything else.** All backfill and live logic is tested
  against a deterministic `FakeExchangeAdapter`, so CI never touches the network.
- **Pure core, I/O at the edges.** Gap detection and timeframe math are pure
  functions. The store, adapter and CLI are thin wrappers.
- **Idempotency by primary key.** Table key is
  `(exchange, symbol, timeframe, open_time_ms)` with upsert semantics. This one
  decision makes re-runs, resume and overlap-fill safe.
- **Closed candles only.** The store never persists or returns the in-progress
  candle. Enforced at write time (updater/backfill drop it) and covered at read time.
- **Vertical slices.** After the foundation, the first working slice is
  "backfill one symbol end to end", then widened, then the live path.

## Dependency Graph

```
pyproject + models (T1)
   │
   ├── ExchangeAdapter + Fake + ccxt (T2)
   │        │
   │        └──────────────┐
   └── SQLite store (T3)   │
            │              │
            └── find_gaps (T4)
                   │
                   └── backfill one symbol (T5) ◄── needs T2, T3
                          │
                          └── resumable multi-symbol (T6)
                                 │
                                 └── verify CLI (T7)
                                        │
                                        └── live updater (T8)
                                               │
                                               ├── reconnect + catch-up (T9)
                                               └── staleness detection (T10)
                                                      │
                                                      └── docs and final sweep (T11)
```

## Task List

Detailed acceptance criteria and verification steps are in `tasks/todo.md`.

### Phase 1: Foundation
- [x] T1: Project scaffold, `Candle` model, `Timeframe` helpers
- [x] T2: `ExchangeAdapter` protocol, `FakeExchangeAdapter`, `ccxt` adapter
- [x] T3: SQLite store (upsert, `get_candles`, `latest_candle`)
- [x] T4: Gap detection (`find_gaps`)

### Checkpoint: Foundation
- [ ] Tests, lint and types pass; real-exchange contract test passes once

### Phase 2: Backfill
- [x] T5: Backfill one symbol and timeframe end to end (CLI)
- [x] T6: Resumable, multi-symbol, multi-timeframe backfill
- [x] T7: `verify` command

### Checkpoint: Backfill
- [ ] Spec success criteria 1 to 4 met

### Phase 3: Live
- [ ] T8: Live updater polling at candle close
- [ ] T9: Reconnect and missed-candle catch-up
- [ ] T10: Staleness detection

### Phase 4: Wrap-up
- [ ] T11: Docs and final spec sweep

### Checkpoint: Complete
- [ ] All 7 success criteria met, ready for review

## Parallelization

- T2 and T3 touch different files and share only `models.py` (done in T1), so they can run in parallel.
- T9 and T10 are independent of each other once T8 lands.
- Everything else is sequential.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Binance API not reachable from your network or jurisdiction | High | T2 runs the contract test early. If it fails, switch the first integration to Bybit or Coinbase. Only the `ccxt` adapter config changes. |
| Exchange rate limits during large backfills | Med | Use ccxt's built-in rate limiter, paginate in fixed-size batches, retry with backoff on rate-limit errors. |
| Exchange returns the in-progress candle, polluting stored data | High | Drop any candle whose close time is in the future at write time. Dedicated unit test. |
| Gaps from exchange maintenance windows that can never be filled | Med | `verify` distinguishes unfillable gaps (exchange returns nothing) from fixable ones and reports them instead of looping forever. |
| Clock drift between local machine and exchange | Med | Staleness and closed-candle logic uses the exchange server time when available, falling back to local UTC. |
| SQLite schema change later breaks consumers | Low | Schema and `Candle` contract changes are "ask first" in the spec. |

## Open Questions

- Confirm or change the defaults table above before starting T1.
- Should `verify` exit non-zero when gaps are found, so it can be used in scripts? (Plan assumes yes.)
