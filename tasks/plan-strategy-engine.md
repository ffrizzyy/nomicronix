# Implementation Plan: strategy-engine

Sources: `SPEC-strategy-engine.md` (approved by owner directive: use best
practice). Module id: `strategy-engine`. Task list: `tasks/todo-strategy-engine.md`.

## Decisions taken for the spec's open questions

| Question | Decision |
|---|---|
| Signal interface | `SignalIdea` from a proposer; engine adds ids, times and validation |
| Built-in strategies | SMA crossover and Donchian breakout, both ATR-stopped |
| Confidence semantics | Bounded score in `[0, 1]`; never bypasses risk |
| AI integration | Protocol plus validated model only; no provider or network in v1 |
| Signal identity | Deterministic `strategy:symbol:timeframe:candle_open_ms` |
| Signal age | `expires_at_ms = close + ttl`, default 5 minutes |
| Warm-up | `min_history` closed candles before the first signal |
| Multiple signals per candle | Allowed by contract; built-ins fire on fresh events only |
| Portfolio awareness | None in v1; delegated to signal-delivery and risk-engine |
| Output | Deterministic JSON plus a CLI summary |
| Dependencies | market-data only; standard library only |

## Architecture Decisions

- **Proposal, not decision.** A proposer returns prices and a rationale. The
  engine owns validation, ids, timestamps and determinism. Risk remains
  downstream.
- **Causal by construction.** `CandleView` slices history at the decision index,
  so future candles are not reachable even by accident.
- **Fresh events only.** Built-ins detect a transition (cross or first breakout),
  not a persistent condition, so a trend does not emit a signal every candle.
- **Explicit drops.** A malformed or low-confidence idea becomes a warning; it
  never becomes a candidate and never disappears silently.
- **Deterministic ids and JSON.** Signal ids come from strategy, symbol,
  timeframe and candle time. No wall clock or randomness.
- **AI is an adapter, not a dependency.** `AIProposer` and `AIProposal` define
  the boundary; v1 ships no network provider.

## Dependency Graph

```
market_data.models
      │
      ▼
   models ──► indicators ──► proposers
      │                        │
      └────────► engine ◄──────┘
                   │
                   ├──► data ──► results ──► CLI
                   └──► config
```

Arrow direction is the build order. Tests mirror it.

## Phases and Checkpoints

### Phase 1: models and indicators
- `Side`, `SignalIdea`, `SignalCandidate`, `ScanConfig`, `SignalBatch`, errors.
- Causal SMA and Wilder ATR helpers plus Donchian channels.
- Checkpoint: model validation table tests; hand-computed indicator fixtures.

### Phase 2: proposers and engine
- `SmaCross`, `DonchianBreakout`, `CandleView`, validation, scanning loop,
  warnings, signal cap.
- Checkpoint: cross/breakout fixtures; no-lookahead spy test; invalid-idea tests.

### Phase 3: data, results and CLI
- Strict gap-free loading, deterministic JSON, atomic write, `scan` command,
  TOML defaults.
- Checkpoint: byte-identical reruns; CLI exit codes; zero-signal run succeeds.

### Phase 4: repository integration
- Docs, examples, README, SPEC status, coverage and type sweep.
- Checkpoint: full offline suite, ruff, mypy and 90% module coverage.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Look-ahead through history | High | `CandleView` slice plus spy test |
| Signal spam in a trend | Med | Fresh-cross/fresh-breakout detection |
| Invalid stop/target geometry | High | Central validation; proposer returns `None` on zero ATR |
| Non-deterministic output | Med | Candle-close timestamps, deterministic ids, sorted JSON |
| Drops hidden from the operator | Med | Every drop appends a stable warning |
| AI provider leaking a network call | Med | No provider in v1; protocol-only tests |

## Verification Checkpoints

- After Phase 1: `uv run pytest tests/strategy_engine/test_models.py`.
- After Phase 2: proposer and engine suites, including the spy test.
- After Phase 3: results and CLI suites, byte-identical rerun.
- Final: `uv run pytest -m "not network" --cov=nomicronix.strategy_engine
  --cov-fail-under=90`, `ruff check`, `ruff format --check`, `mypy`.
