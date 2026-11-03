# Implementation Plan: execution

Sources: `SPEC-execution.md` (approved by owner directive: use best practice).
Module id: `execution`. Task list: `tasks/todo-execution.md`.

**This plan is intentionally not started.** The project-wide gate requires
`signal-delivery readiness` to pass and the owner to approve the spec before any
code is written. Nothing below authorizes a live order.

## Entry checklist

- [ ] `uv run python -m nomicronix.signal_delivery readiness` exits `0`
- [ ] The readiness JSON is archived with the owner's approval note
- [x] The risk-engine v1.1 partial-fill change is specified and approved
- [ ] A trade-only Binance key exists, withdrawals are disabled, IP is pinned
- [ ] The owner approves stage 1 (shadow) and stage 2 (human-approved) only

## Decisions taken for the spec's open questions

| Question | Decision |
|---|---|
| Build gate | Readiness passes and the owner approves the spec |
| First stage | Shadow intents, no private calls |
| Order type | Spot market order with a 50 bps default slippage ceiling |
| Approval phrase | Exact `PLACE ORDER`; `dry_run = false` and `--live` also required |
| Idempotency | Client order id derived from the signal id |
| Partial fills | Aggregate and report once; blocked on risk-engine v1.1 |
| Reconciliation | Mandatory before submit; unclean means no trading |
| Kill switch | Blocks submissions; `flatten` only reduces exposure |
| Credentials | Environment-only, trade-only, redacted output |
| Autonomy | Out of scope; separate future spec revision |

## Architecture Decisions

- **Adapter boundary.** A narrow `ExchangeAdapter` protocol keeps `ccxt` out of
  the domain. Tests use a scripted fake; only the real adapter touches the
  network.
- **Journal before output.** Every intent and transition is written before a
  command reports success, so a crash leaves a resumable trail.
- **Client order id is the idempotency key.** Retrying a submit with the same id
  is safe; the module never invents a second id for the same signal.
- **Reconcile, then trade.** A clean reconciliation is a precondition, not a
  background task.
- **Risk owns the books.** Execution reports fills through `RiskService` and
  never writes risk tables directly.
- **Two switches for live.** `dry_run = false` in TOML and `--live` on the CLI.
  Either one alone is inert.

## Dependency Graph

```
risk_engine.service ──► risk gate ──► intent ──► journal ──► adapter
                                            │                   │
signal_delivery (signals, ledger) ──────────┘                   │
                                                                ▼
                                                    reconcile ──► CLI
```

Arrow direction is the build order. The fake adapter mirrors the real one.

## Phases and Checkpoints

### Phase 0: gate and prerequisites
- Run readiness, archive it, get the risk-engine v1.1 partial-fill approval.
- Checkpoint: the entry checklist is fully checked.

### Phase 1: models, journal and fake adapter
- `OrderIntent`, `ExchangeOrder`, `OrderEvent`, `Reconciliation`, errors.
- SQLite journal with idempotent intent ids.
- Scripted fake adapter.
- Checkpoint: duplicate intent creation is a no-op; every event survives reopen.

### Phase 2: shadow mode
- Signal loading, risk re-check, expiry and slippage guards, shadow intents.
- Checkpoint: no private adapter method is called in shadow mode.

### Phase 3: human-approved submission
- Exact-phrase approval, two live switches, submit/poll/cancel, fill reporting.
- Checkpoint: double submit, stale intent, kill switch and slippage each refuse
  with the right exit code; partial fills follow the risk v1.1 contract.

### Phase 4: reconciliation and flatten
- Compare orders and balances, journal mismatches, refuse trading when unclean.
- Panic flatten cancels and reduces only.
- Checkpoint: injected unknown order and unknown position both block submits.

### Phase 5: CLI, docs and opt-in network test
- `shadow`, `status`, `show`, `submit`, `cancel`, `reconcile`, `flatten`.
- Module guide, README table, example config, CI unchanged (no keys).
- Checkpoint: full offline suite, 90% module coverage, offline fake only.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Live order without approval | Critical | Four independent gates: stage, phrase, TOML flag, `--live` |
| Duplicate order after a retry | High | Client order id is the signal id; duplicate submit is a no-op |
| Partial fill recorded as full | High | Risk-engine v1.1 is a hard prerequisite |
| Books diverge from the exchange | High | Mandatory clean reconciliation before submit |
| Slippage on a market order | High | Best-ask ceiling checked immediately before submit |
| Key leak in an error or journal | High | Environment-only keys; redact URLs and bodies |
| Unknown order status treated as filled | High | Fail closed; only terminal exchange states are accepted |
| Accidental autonomy | High | Stage 3 is out of scope and rejected by config |
| Exchange outage mid-order | Med | Timeout leaves `submitted`; reconcile resolves; never auto-resubmit |
| Test suite touching the network | Med | Fake adapter; one opt-in read-only contract test |

## Verification Checkpoints

- After Phase 1: journal and fake-adapter suites.
- After Phase 2: shadow mode makes zero private calls (spy test).
- After Phase 3: refusal matrix for every guard and exit code.
- After Phase 4: reconciliation mismatch fixtures.
- Final: `uv run pytest -m "not network" --cov=nomicronix.execution
  --cov-fail-under=90`, `ruff`, `ruff format --check`, `mypy`.

## Stop conditions

Stop and return to the owner if readiness fails, reconciliation cannot be made
clean, the exchange rejects the key permissions, or any behaviour would need a
change to `risk-engine` that is not already specified.
