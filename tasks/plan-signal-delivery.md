# Implementation Plan: signal-delivery

Sources: `SPEC-signal-delivery.md` (approved by owner directive: use best
practice). Module id: `signal-delivery`. Task list:
`tasks/todo-signal-delivery.md`.

## Decisions taken for the spec's open questions

| Question | Decision |
|---|---|
| Delivery input | A `strategy-engine` JSON result, plus a library API for candidates |
| Risk timing | Signal candle close, so replay is deterministic |
| Rejected signals | Recorded in the outbox, never sent as trade alerts |
| Duplicate handling | Outbox keyed by signal id; delivered and rejected rows are skipped |
| Retry behaviour | Pending and failed rows resend the stored payload without re-checking |
| Paper fills | Full, fee-free, at entry; stops before targets |
| Gap handling on a stop | Close at the worse of stop and mark price |
| Credentials | Environment variables only, never persisted or printed |
| Remote sink | Telegram Bot API; console and JSONL work offline |
| Scheduling | Owner-invoked CLI; no daemon in v1 |
| Dependencies | strategy-engine, risk-engine; standard library only |

## Architecture Decisions

- **Outbox first.** Every signal id gets one delivery row. Sending from the
  stored payload makes retries safe and keeps an audit trail.
- **Risk is called, never reimplemented.** Delivery imports `RiskService`,
  converts a candidate to a `TradeProposal`, and lets it decide and persist.
- **Candle time is replay time.** Marking and checking at `created_at_ms` makes
  historical replay deterministic and avoids a wall-clock dependency.
- **Sinks are tiny and injectable.** Console and JSONL need no dependencies;
  Telegram takes a transport callable so tests never touch the network.
- **Paper P&L lives beside the outbox.** The risk database stays the source of
  truth for equity and positions; delivery adds the trade ledger and report.

## Dependency Graph

```
strategy_engine.models ──► models ──► render
                              │
risk_engine  ─────────────────┼──► service ──► store
                              │        │
                              │        └──► sinks ──► telegram
                              │
                              └──► config ──► CLI
```

Arrow direction is the build order. Tests mirror it.

## Phases and Checkpoints

### Phase 1: models and rendering
- `Notification`, `PaperTrade`, `DeliveryRecord`, `DeliveryStatus`, errors.
- Deterministic text and JSON rendering.
- Checkpoint: golden text and sorted JSON tests.

### Phase 2: store and sinks
- SQLite outbox and paper-trade ledger, schema creation, upserts and queries.
- Console, JSONL and Telegram sinks with a fake transport.
- Checkpoint: idempotent delivery rows, append-only JSONL, credential errors.

### Phase 3: delivery service
- Signal-file loader, risk gate, open fills, retry path, `tick`, report.
- Checkpoint: approved/rejected/duplicate/retry tests; stop and target closes;
  report totals.

### Phase 4: CLI and repository integration
- `deliver`, `tick`, `report`; TOML defaults; docs, example, README, SPEC
  status, full quality sweep.
- Checkpoint: CLI exit codes, byte-stable JSONL, 90% module coverage.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Duplicate alerts on retry | Med | Outbox payload plus signal id in every message |
| Delivery bypasses risk | High | Only `RiskService.check` approvals become trade alerts |
| Wall clock leaks into replay | Med | Signal candle close is the only default event time |
| Telegram token leaks in an error | High | Never print the URL; redacted `DeliveryError` messages |
| Stop and target both crossed | Med | Stop-first, documented conservative rule |
| Outbox and risk fill diverge | Med | Documented at-most-once window; no second position possible |

## Verification Checkpoints

- After Phase 1: `uv run pytest tests/signal_delivery/test_render.py`.
- After Phase 2: store and sink suites, fake Telegram transport only.
- After Phase 3: service suite, including replay and P&L fixtures.
- Final: `uv run pytest -m "not network" --cov=nomicronix.signal_delivery
  --cov-fail-under=90`, `ruff check`, `ruff format --check`, `mypy`.
