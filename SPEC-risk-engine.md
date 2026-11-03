# Spec: risk-engine

Module id: `risk-engine` (depends on: none)

## Objective

Be the deterministic safety layer between a trade idea and the exchange. The AI
proposes; this module decides. It takes a fully specified trade proposal, the
current portfolio snapshot, the instrument's quantity rules and the configured
limits, then returns either an approved quantity with its risk amount or a
rejection with machine-readable reasons.

**Consumers:** `signal-delivery` (pre-check before a signal is actionable) and
`execution` (final gate immediately before an order). Both must call
`RiskService.check`; neither may size a position or bypass a limit itself.

**User stories**

- As the owner, I can run `status` and see equity, drawdown, daily P&L, open
  positions, open risk and whether trading is currently allowed.
- As `signal-delivery` or `execution`, I submit a proposal and get a
  deterministic decision: approved quantity, notional, risk amount, reward/risk
  and a stable list of rejection reasons.
- As the owner, I can engage a kill switch immediately. It survives restarts and
  only an explicit manual `resume` clears it.
- As the owner, I can audit every decision from an append-only journal and see
  exactly which rule approved or rejected it.

**Out of scope for this module:** placing or cancelling orders, exchange
connectivity, price feeds, strategy logic, backtesting, tax accounting,
leverage, futures and short selling. This module never sees or needs API keys.

**Assumptions**

- Spot long-only trades. `Side.BUY` is the only supported side until a later
  spec adds shorts.
- Full fills only in v1. Partial fills and multiple fills per approval are
  explicitly deferred (see Open Questions).
- The owner runs one process that mutates risk state at a time. The module is
  not a distributed lock.
- Prices are supplied by `market-data` consumers as `Decimal` values. This
  module does not fetch them.

## Tech Stack

- Python 3.12
- Standard library only: `decimal`, `sqlite3`, `tomllib`, `dataclasses`,
  `enum`, `datetime`
- `pytest`, `pytest-cov`, `ruff`, `mypy --strict`
- `uv` for dependency and environment management
- No new runtime dependencies

## Commands

```
Install:      uv sync
Status:       uv run python -m nomicronix.risk_engine status --db data/risk.sqlite
Check:        uv run python -m nomicronix.risk_engine check --proposal proposal.json --db data/risk.sqlite
Mark:         uv run python -m nomicronix.risk_engine mark --prices prices.json --db data/risk.sqlite
Kill:         uv run python -m nomicronix.risk_engine kill --reason "manual stop" --db data/risk.sqlite
Resume:       uv run python -m nomicronix.risk_engine resume --confirm "RESUME TRADING" --db data/risk.sqlite
Test:         uv run pytest tests/risk_engine -m "not network" --cov=nomicronix.risk_engine --cov-fail-under=90
Lint:         uv run ruff check src tests --fix
Format:       uv run ruff format src tests
Types:        uv run mypy src/nomicronix/risk_engine
```

There are no network tests in this module. It must never contact an exchange.

CLI exit codes: `0` success/approved, `2` rejected by risk, `1` invalid input,
corrupt state or another error. `status`, `mark`, `kill` and `resume` return `0`
on success and `1` on failure.

## Project Structure

```
src/nomicronix/risk_engine/
  models.py       -> Side, Instrument, TradeProposal, Position, RiskSnapshot,
                     RiskLimits, RiskDecision, RiskStatus, KillSwitch, RejectReason
  sizing.py       -> risk-based size, notional cap, step rounding
  engine.py       -> pure evaluate(): ordered checks, no I/O and no clock
  state.py        -> PortfolioState: mark-to-market, day rollover, fills, kill switch
  store.py        -> SQLite state, approvals and append-only decision journal
  service.py      -> RiskService: load state, evaluate, persist in one transaction
  config.py       -> load and validate RiskLimits from TOML
  __main__.py     -> status, check, mark, kill, resume
config/risk.toml  -> committed conservative defaults, no secrets
tests/risk_engine/
  fakes.py        -> proposal/state/instrument builders
  test_sizing.py
  test_engine.py
  test_state.py
  test_store.py
  test_config.py
  test_cli.py
```

## Interface Contract (what consumers depend on)

```python
class Side(StrEnum):
    BUY = "buy"


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal


@dataclass(frozen=True, slots=True)
class TradeProposal:
    signal_id: str
    strategy_id: str
    symbol: str
    side: Side
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    confidence: float          # recorded, never used to bypass a limit
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    current_price: Decimal
    opened_at_ms: int
    signal_id: str


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    equity: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal
    realized_pnl_today: Decimal
    positions: tuple[Position, ...]
    kill_switch: KillSwitch | None
    as_of_ms: int


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    signal_id: str
    approved_quantity: Decimal
    approved_notional: Decimal
    risk_amount: Decimal
    reward_risk: Decimal
    reasons: tuple[RejectReason, ...]
    rule_trace: tuple[str, ...]


class RiskService:
    def status(self, now_ms: int) -> RiskStatus: ...
    def mark(self, prices: Mapping[str, Decimal], now_ms: int) -> RiskStatus: ...
    def check(
        self,
        proposal: TradeProposal,
        instrument: Instrument,
        now_ms: int,
    ) -> RiskDecision: ...
    def record_fill(self, signal_id: str, fill: Fill, now_ms: int) -> PortfolioState: ...
    def engage_kill_switch(self, reason: str, now_ms: int) -> None: ...
    def resume(self, confirmation: str, now_ms: int) -> None: ...
```

`check` evaluates and persists the decision atomically before returning. A
caller cannot receive an approval that is absent from the journal.
`record_fill` consumes the approval: an unknown, duplicate or already-consumed
`signal_id` is rejected, so state cannot drift from the audit trail.

`resume` requires the exact confirmation phrase `RESUME TRADING`. No other value
clears the kill switch.

## Rules and Sizing

Checks run in this stable order. All applicable reasons are collected, so one
decision explains every failed rule; evaluation stops early only when prices are
invalid and later arithmetic would be meaningless.

1. Kill switch engaged.
2. Stale signal: `now_ms - created_at_ms > max_signal_age_ms`.
3. Stale state: `now_ms - as_of_ms > max_signal_age_ms`.
4. Symbol not in `allowed_symbols`.
5. Instrument symbol mismatch.
6. Invalid proposal: non-positive prices, buy stop not below entry, target not
   above entry, confidence outside `0..1`.
7. Duplicate symbol already open (no pyramiding).
8. `max_concurrent_positions`.
9. `max_daily_loss_pct` measured against `day_start_equity`.
10. `max_drawdown_pct` measured against `peak_equity`; a breach also engages the
    kill switch.
11. `min_reward_risk`: `(target - entry) / (entry - stop)`.
12. Position sizing and per-position notional cap.
13. `max_total_exposure_pct` across marked positions plus the proposal.
14. `max_open_risk_pct` across open stops plus the proposal.
15. Approve.

Sizing:

- `risk_amount = equity * risk_per_trade_pct`
- `by_risk = risk_amount / (entry - stop)`
- `by_notional = (equity * max_position_notional_pct) / entry`
- `quantity = floor_to_step(min(by_risk, by_notional), quantity_step)`
- Reject if quantity is below `min_quantity`, or if
  `quantity * entry` is below the larger of `Instrument.min_notional` and the
  configured minimum.
- A notional cap that merely reduces size is not a rejection; the decision
  records the cap in `rule_trace`. A cap that pushes size below the minimum is a
  rejection.

Conservative defaults for a 1,000 USDT paper account:

| Limit | Default |
|---|---|
| Risk per trade | 0.5% of equity |
| Max daily loss | 3% of day-start equity |
| Max drawdown | 10% from peak equity, auto kill switch |
| Max concurrent positions | 2 |
| Max position notional | 25% of equity |
| Max total exposure | 50% of equity |
| Max aggregate open risk | 2% of equity |
| Minimum reward/risk | 2.0 |
| Max signal age | 5 minutes |
| Allowed symbols | `BTC/USDT`, `ETH/USDT` |
| Starting paper capital | 1,000 USDT |

At 1,000 USDT equity, a 100 entry with a 95 stop risks 5 USDT, so the risk-based
size is 1 unit (100 USDT notional), which is inside the 250 USDT position cap.

## State and Persistence

- Default database: `data/risk.sqlite` (gitignored). The CLI accepts `--db`.
- SQLite runs in WAL mode. Mutations use `BEGIN IMMEDIATE`; one writer process at
  a time. Hand-editing the database is forbidden.
- Tables:
  - `risk_state`: singleton row with cash, peak equity, day-start equity, UTC
    day, realized P&L today, kill switch, last mark time.
  - `positions`: open positions keyed by symbol.
  - `approvals`: every approved `signal_id`, approved quantity/notional,
    timestamp and consumption timestamp.
  - `decisions`: append-only journal of every check, including rejected ones.
- A UTC day rollover resets `day_start_equity` and `realized_pnl_today`. The
  kill switch does **not** reset on a day boundary.
- `mark` updates current prices, recomputes equity, updates `peak_equity` and
  engages the kill switch if drawdown reaches the limit.
- A drawdown breach is fail-closed in `check` even if `mark` has not persisted it
  yet: the decision is `MAX_DRAWDOWN` and the service persists the kill switch in
  the same transaction.
- Every check writes one journal row with the full proposal and decision JSON.
  Journal rows are never updated or deleted.

## Configuration

`config/risk.toml` is committed with the conservative defaults above. It contains
no secrets. `--config` overrides the path.

- All percentages are decimal strings parsed as `Decimal`, not floats.
- Validation: `0 < pct <= 1`, positive integers where applicable,
  `min_reward_risk >= 1`, non-empty allowed symbols, positive signal age.
- Instrument quantity rules are **not** stored here as the source of truth.
  `execution` passes the current exchange filters at check time; static stale
  filters are worse than no filters. The CLI's `--proposal` file includes an
  `instrument` object for manual checks.

Example proposal file:

```json
{
  "signal_id": "sig-2026-01-01-btc-001",
  "strategy_id": "sma-cross",
  "symbol": "BTC/USDT",
  "side": "buy",
  "entry_price": "100",
  "stop_price": "95",
  "target_price": "115",
  "confidence": 0.8,
  "created_at_ms": 1735689600000,
  "instrument": {
    "symbol": "BTC/USDT",
    "quantity_step": "0.00001",
    "min_quantity": "0.00001",
    "min_notional": "10"
  }
}
```

## Code Style

- `Decimal` for every price, quantity, notional and P&L value. `float` is allowed
  only for `confidence` and for metrics/reporting, never for money.
- `evaluate` is pure: no I/O, no wall clock, no network, no globals. `now_ms` is
  always passed in.
- One rule per small function, returning `RejectReason | None`. The ordered list
  of rules is data, so rule order is testable.
- Frozen dataclasses for inputs and decisions; mutable state stays inside
  `state.py`.
- No bare `except`. Persistence errors surface and fail the check.

```python
def position_size(
    equity: Decimal,
    entry: Decimal,
    stop: Decimal,
    limits: RiskLimits,
    instrument: Instrument,
) -> Decimal:
    """Largest quantity whose loss at the stop stays inside the per-trade limit."""
    risk_per_unit = entry - stop
    if risk_per_unit <= 0 or equity <= 0:
        return Decimal(0)
    by_risk = (equity * limits.risk_per_trade_pct) / risk_per_unit
    by_notional = (equity * limits.max_position_notional_pct) / entry
    raw = min(by_risk, by_notional)
    return (raw // instrument.quantity_step) * instrument.quantity_step
```

## Testing Strategy

| Level | What | Tool | Runs in CI |
|---|---|---|---|
| Unit | sizing arithmetic, step rounding, each rule in isolation, config validation | pytest | yes |
| Component | `RiskService` against temporary SQLite: mark, check, fill, restart, kill/resume | pytest | yes |
| Table-driven | every `RejectReason` has a case; rule order is asserted | pytest `parametrize` | yes |
| CLI | exit codes, JSON output, corrupt input, confirmation phrase | pytest | yes |

- Coverage: at least 90% line coverage for `nomicronix.risk_engine`.
- Every rejection reason has at least one named test.
- Every success criterion maps to a named test in the final traceability table.
- Determinism test: same snapshot, proposal, limits and `now_ms` produce an
  identical `RiskDecision` across repeated calls and a fresh process.
- Bug fixes start with a failing test. No test is skipped or deleted to get green.

## Boundaries

**Always**

- Fail closed. Missing state, unknown instrument, invalid input or an internal
  error means reject, never approve.
- Keep `evaluate` pure and deterministic; pass `now_ms` explicitly.
- Persist every decision before returning it to a consumer.
- Keep all money in `Decimal` and all timestamps in UTC milliseconds (`*_ms`).
- Record the full rule trace, including caps that reduced size without rejecting.
- Check market-data freshness in the calling module before submitting a proposal;
  a stale price is a stale signal even if its timestamp looks recent.

**Ask first**

- Changing any default limit in the conservative table.
- Adding short selling, leverage, futures or margin.
- Changing the SQLite schema, `TradeProposal`, `RiskDecision` or `RiskService`
  contract.
- Adding partial fills or multiple fills per approval.
- Adding a runtime dependency.
- Adding an automatic kill-switch reset.
- Letting `confidence` change position size.

**Never**

- Place, amend or cancel an order.
- Call an exchange, an LLM or any network service.
- Use `float` for money or silently round a quantity up.
- Bypass, weaken or auto-reset the kill switch.
- Delete or rewrite journal rows.
- Store API keys or secrets in the repository or the risk database.
- Approve a trade without a stop-loss.

## Success Criteria

1. With 1,000 USDT equity, 100 entry, 95 stop and 115 target, the engine
   approves 1 unit, 100 USDT notional, 5 USDT risk and reward/risk 3.0.
2. No approved trade ever risks more than 0.5% of equity at its stop, including
   after step rounding and the 25% position notional cap.
3. A 3% loss from `day_start_equity` blocks new approvals for the rest of the UTC
   day; the next UTC day starts with a fresh daily loss budget.
4. A 10% drawdown from peak equity engages the kill switch, persists across a
   process restart, rejects every proposal, and clears only after
   `resume --confirm "RESUME TRADING"`.
5. A third concurrent position is rejected, and a second position in an already
   open symbol is rejected.
6. The 50% total exposure cap, 2% aggregate open-risk cap and 2.0 minimum
   reward/risk are each enforced with a named test.
7. Invalid prices, a buy stop above entry, a stale signal, an unknown symbol and
   a stale snapshot are rejected with the correct `RejectReason`.
8. Identical inputs produce identical decisions, and a decision that was approved
   but never persisted cannot be returned.
9. Every decision, approved or rejected, is queryable from the journal after a
   restart, with its proposal and reasons intact.
10. `uv run pytest ... --cov-fail-under=90`, `ruff check` and `mypy` all pass.

## Open Questions

1. **Daily loss basis:** the spec measures the drop in equity from the UTC
   day-start, including unrealized P&L. Do you want realized-only instead?
2. **Drawdown basis:** the spec uses intraday marked-to-market equity against the
   peak. End-of-day peaks would be less twitchy; confirm which you prefer.
3. **Signal age:** 5 minutes is strict for a 1h strategy. Should the limit be per
   timeframe (for example, 2x the timeframe) instead of a flat 5 minutes?
4. **Aggregate open risk:** is a 2% cap across all open positions right, or
   should it also be 3% to match the daily loss limit?
5. **Partial fills:** v1 consumes an approval with one full fill. Do you need
   partial fills before `execution`, or can that wait?
6. **Resume safety:** is typing `RESUME TRADING` enough, or do you want a second
   confirmation or a cooldown after a drawdown stop?
7. **Instrument filters:** confirm that `execution` should pass live exchange
   filters into every check rather than this module caching them.
8. **Kill-switch notifications:** should engaging the kill switch also emit a
   `signal-delivery` alert, or is the journal plus CLI enough for now?
