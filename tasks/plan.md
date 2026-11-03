# Implementation Plan: risk-engine and backtesting

Sources: `SPEC-risk-engine.md`, `SPEC-backtesting.md` (approved by owner
directive: use best practice). Module ids: `risk-engine`, `backtesting`.
Task list lives in `tasks/todo.md`.

## Decisions taken for the spec's open questions

| Question | Decision |
|---|---|
| Daily loss basis | Equity drawdown from UTC day-start, including unrealized P&L |
| Drawdown basis | Intraday marked-to-market equity against the all-time peak |
| Signal age | 5 minutes, configurable; stale signals fail closed |
| Aggregate open risk | 2% of equity; tighter than the 3% daily limit, so it binds first |
| Partial fills | Deferred to `execution`; v1 consumes one full fill per approval |
| Resume safety | Exact phrase `RESUME TRADING`; no auto-reset |
| Instrument filters | Passed by `execution` at check time; never cached here |
| Kill-switch alerts | Journal plus CLI for now; `signal-delivery` will consume status later |
| Backtest universe | One symbol and timeframe per run; long-only spot |
| Backtest costs | 10 bps fee per side, 5 bps adverse slippage, both configurable |
| Backtest sizing | Fixed 25% of equity notional; `SizingPolicy` allows a future risk adapter |
| Walk-forward | 90d train / 30d test / 30d step, Sharpe objective, 200-candle warm-up |
| Risk integration in backtests | Not in v1; documented limitation, no dependency change |
| Results size | One equity point per closed candle; JSON is the source of record |

## Architecture Decisions

- **Pure rules, thin service.** `risk-engine.evaluate` is a pure function over a
  snapshot. `RiskService` owns SQLite, transactions and persistence. This keeps
  every rule table-testable without a database.
- **Fail closed.** Missing state, stale input, unknown symbols and internal
  errors reject. There is no permissive fallback.
- **Append-only audit.** Every decision is journaled before it is returned.
  Approvals are single-use and fills that do not match an approval are rejected.
- **Kill switch is sticky.** Drawdown engages it automatically; only an explicit
  human confirmation clears it.
- **Causal backtests.** Signals at close, fills at next open, and a read-only
  history view that physically cannot expose a later candle.
- **Costs are not optional.** Every fill pays fee and adverse slippage. A result
  that ignores costs is a bug, not a configuration.
- **Strict data.** A gap in the requested range aborts the run with the missing
  ranges. Research on incomplete data is worse than no research.
- **Deterministic output.** No wall clock, random ids or absolute paths in result
  JSON. Same inputs produce byte-identical output.
- **Small dependency surface.** Standard library only for both modules. `Decimal`
  for money; `float` only for ratios.

## Dependency Graph

```
risk-engine
  models ──► sizing ──► engine ──► state ──► store ──► service ──► CLI
                                      │
                                      └── config

backtesting
  market_data.models
        │
        ▼
  models ──► strategy ──► data ──► broker ──► engine ──► metrics
        │                                      │
        └── sizing ────────────────────────────┘
                                               │
                                               ▼
                                          walk_forward ──► results ──► CLI
```

The two modules are independent and can be built in parallel. Within each, the
arrow direction is the build order.

## Phases and Checkpoints

### Phase 1: risk-engine core
- Models, limits validation, sizing, pure rules, state transitions.
- Checkpoint: unit tests cover every rejection reason and the conservative
  sizing fixture; `ruff` and `mypy --strict` clean.

### Phase 2: risk-engine persistence and CLI
- SQLite store, atomic service, TOML config, `status`/`check`/`mark`/`kill`/
  `resume`.
- Checkpoint: restart preserves kill switch and journal; CLI exit codes match
  the spec.

### Phase 3: backtesting core
- Models, data validation, broker fills, engine loop, metrics.
- Checkpoint: no-lookahead spy test, fee/slippage fixture, gap rejection.

### Phase 4: backtesting walk-forward and CLI
- Parameter grid, window selection, OOS stitching, deterministic JSON, CLI.
- Checkpoint: walk-forward windows do not overlap; repeated runs are
  byte-identical.

### Phase 5: repository completion
- README, module guides, example strategies/configs, CI workflow, coverage and
  type sweep.
- Checkpoint: full offline suite, lint, types and coverage pass locally.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Decimal floor/rounding drift in sizing | High | Table tests at step boundaries; round down only; never round up |
| Drawdown kill switch not persisted on crash | High | `persist_check` writes state and journal in one transaction |
| Strategy accidentally sees future data | High | `HistoryView` physically slices at the current index; spy test |
| Stop/target intrabar ambiguity | Med | Stop-first rule; gap-through-stop fills at open; golden fixture |
| Walk-forward leaks test data into parameter choice | High | Parameter selection runs only on train; test uses frozen params |
| `pytest-cov` missing lines from CLI branches | Med | CLI tests inject temp DB; enforce 90% per module |
| Python 3.14 vs project 3.12 | Low | Standard-library-only modules; CI pins 3.12 and 3.13 |
| Backtest results mistaken for live performance | Med | Docs and JSON warning field; OOS metrics are the headline |

## Verification Checkpoints

- After Phase 1: `uv run pytest tests/risk_engine -q` and `mypy`.
- After Phase 2: restart and CLI integration tests.
- After Phase 3: `uv run pytest tests/backtesting -q` and golden fixture.
- After Phase 4: walk-forward and deterministic JSON tests.
- Final: `uv run pytest -m "not network" --cov ...`, `ruff check`, `mypy`.
