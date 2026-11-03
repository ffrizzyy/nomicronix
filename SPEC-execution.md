# Spec: execution

Module id: `execution` (depends on: risk-engine, signal-delivery)

**Status: spec written, implementation blocked.** The project-wide rule is that
`execution` is not built until `signal-delivery readiness` exits `0` **and** the
owner explicitly approves this spec for implementation. The gate is measurable;
see `SPEC-signal-delivery.md`.

## Objective

Turn an approved signal into an actual spot order, in stages, without ever
bypassing `risk-engine`. Execution is the only module allowed to hold exchange
API credentials or call a private exchange endpoint.

It answers one question: *for an approved signal, what order was placed, what
happened to it, and does the exchange agree with our books?*

**Consumers:** the owner via the CLI. `signal-delivery` remains the alert and
paper-record layer; execution does not send alerts itself.

## Non-responsibilities

- No signal generation and no strategy logic.
- No position sizing, limit checks or kill-switch decisions. Sizing comes from
  the approved `RiskDecision`; `execution` may only submit at most that quantity.
- No AI. No leverage, futures, margin, shorts or withdrawals.
- No second bookkeeping system for cash and positions. `risk-engine` remains the
  source of truth; execution journals orders and reports fills into it.
- No scheduler or always-on daemon in v1. The CLI is owner-invoked, and each
  command is safe to re-run.

## Staged rollout

| Stage | Name | Behaviour | Gate to enter |
|---|---|---|---|
| 0 | paper | `signal-delivery` tracks approved signals with no exchange call | already built |
| 1 | shadow | Build order intents and compare with the live book; never submit | readiness passes, owner approves shadow mode |
| 2 | approved | The owner approves each order with a confirmation phrase, then it is submitted | at least 20 shadow intents with no reconciliation errors |
| 3 | autonomous | Approved signals submit automatically inside risk limits | at least 50 human-approved orders over 30 days with no incidents |

Stage 3 is a separate owner decision and a separate spec revision. This document
specifies stages 1 and 2 only; stage 3 is explicitly out of scope here.

## Domain model

All prices, quantities and fees are `Decimal`; times are UTC epoch milliseconds.

**`OrderIntent`** — one approved signal expressed as a proposed order:

| Field | Type | Meaning |
|---|---|---|
| `intent_id` | `str` | Stable id derived from `signal_id` |
| `signal_id` | `str` | Source signal |
| `symbol` | `str` | Market |
| `side` | `str` | `buy` in v1 |
| `quantity` | `Decimal` | Exactly the approved quantity |
| `entry_price` | `Decimal` | Signal entry, used for the slippage guard |
| `stop_price`, `target_price` | `Decimal` | Protective levels, carried for audit |
| `approved_notional` | `Decimal` | From the risk decision |
| `created_at_ms` | `int` | Signal candle close |
| `expires_at_ms` | `int` | Signal expiry |
| `state` | `str` | `shadow`, `awaiting_approval`, `submitted`, `filled`, `canceled`, `rejected`, `failed` |

**`ExchangeOrder`** — the exchange's view: client order id, exchange order id,
status, filled quantity, average price, fee, timestamps and raw status text.

**`OrderEvent`** — append-only journal row: intent id, event type, timestamp and
payload JSON. Event types are `intent_created`, `shadowed`, `approved`,
`submitted`, `partially_filled`, `filled`, `canceled`, `rejected`, `failed`,
`reconciled`.

**`Reconciliation`** — the result of comparing open orders and balances with the
journal: matched orders, unknown orders, unknown positions and a `clean` flag.

## Safety properties

These are the reason the module exists. Each is enforced, not documented away.

1. **Risk decides.** An intent can only be created from a `RiskDecision` with
   `approved = true`. Execution never sizes or re-derives a quantity.
2. **One order per signal.** `intent_id = f"nx-{signal_id}"` is the exchange
   client order id and the local primary key. A duplicate submit is a no-op, not
   a second order.
3. **Kill switch blocks submission.** Every submit reads `RiskService.status()`;
   a disallowed account cancels nothing automatically but refuses new orders.
4. **Stale intent refusal.** An intent older than the signal's `expires_at_ms`
   or the configured `max_intent_age_ms` is rejected before any network call.
5. **Slippage ceiling.** Before submitting, compare the signal entry with the
   current best ask. If the adverse move exceeds `max_slippage_bps`, reject the
   intent. Never chase.
6. **Spot only.** Exchange market type is fixed to `spot`; a configuration that
   selects another market type fails at startup.
7. **Trade-only keys.** Withdrawals must be disabled on the key. Startup checks
   the key's permissions when the exchange exposes them and otherwise prints a
   reminder that cannot be skipped silently.
8. **Fail closed.** Unknown order status, unknown balance, failed reconciliation,
   missing credentials or a network error all stop the command. Nothing is
   treated as filled because it probably filled.
9. **No secrets in output.** Keys and signed request URLs never appear in logs,
   errors, journals or result JSON.
10. **Paper first.** `dry_run = true` is the default and refuses to submit until
    the owner sets `dry_run = false` in the config **and** passes `--live` on the
    command. Both are required.

## Order flow

### Stage 1: shadow

1. Load an approved signal file (same schema as `signal-delivery`).
2. Re-run `RiskService.check` at the signal candle close.
3. For an approval, fetch the ticker and build an `OrderIntent` with the exact
   approved quantity.
4. Apply the expiry and slippage guards. Record the intent and a `shadowed`
   event. Make no private call.
5. `report` compares shadow intents with the paper ledger so the owner can see
   how often fulfilment would have differed.

### Stage 2: human-approved

1. Same steps as shadow, but the intent state is `awaiting_approval`.
2. `submit --intent <id> --confirm "PLACE ORDER"` requires the exact phrase. Any
   other phrase is a typed error with exit `1`.
3. Before the exchange call, re-check the risk status and re-fetch the ticker.
   If the price moved beyond the slippage ceiling, reject and journal it.
4. Submit a **market** order with the intent's quantity and client order id.
   Market orders are used because a resting limit order can miss the move that
   created the signal; the slippage ceiling, not the order type, is the control.
5. Poll until the order is terminal (`closed`, `canceled`, `rejected`,
   `expired`) or the configured timeout elapses. A timeout leaves the intent
   `submitted`; `reconcile` resolves it later. Never resubmit automatically.
6. Aggregate fills and call `RiskService.record_fill` **once** with the filled
   quantity, average price and summed fee.
7. Journal the terminal event and print a deterministic summary.

### Cancellation and flatten

- `cancel --intent <id>` cancels an open order and journals the result.
- `flatten --confirm "FLATTEN NOW"` cancels every open order and market-sells
  every open position at the exchange's best bid after re-reading the risk
  status. It is the panic button; it still reports fills through `record_fill`.
  It never bypasses size limits because it only reduces exposure.

## Partial fills and the risk contract

`risk-engine` v1.1 accepts any positive piece of an approval and consumes it only
when the cumulative filled quantity reaches the approved quantity. Spot market
orders can fill in pieces or only partially before cancellation, so execution
aggregates the pieces and reports them once:

- `record_fill` accepts a CLOSE or OPEN fill with
  `0 < quantity <= approved_quantity - filled_quantity` (a CLOSE is bounded by
  the open position).
- The approval is consumed only when the filled quantity reaches the approved
  quantity; a partial fill leaves the remainder usable.
- A canceled remainder is expired with `RiskService.expire_approval`, after which
  no later fill can use it.

That change has landed in `risk-engine` v1.1 and is covered by
`tests/risk_engine/test_partial_fills.py`. Execution must never report a partial
fill as a full one.

## Exchange adapter

A narrow protocol, implemented once for Binance through `ccxt`:

```python
class ExchangeAdapter(Protocol):
    def market_type(self) -> str: ...
    def fetch_ticker(self, symbol: str) -> Ticker: ...
    def fetch_permissions(self) -> set[str]: ...
    def create_market_order(
        self, symbol: str, side: str, quantity: Decimal, client_order_id: str
    ) -> ExchangeOrder: ...
    def fetch_order(self, order_id: str, symbol: str) -> ExchangeOrder: ...
    def cancel_order(self, order_id: str, symbol: str) -> ExchangeOrder: ...
    def open_orders(self, symbol: str | None = None) -> list[ExchangeOrder]: ...
    def balances(self) -> dict[str, Decimal]: ...
```

- `ccxt` is only imported by the real adapter. Tests use a fake adapter and never
  touch the network.
- `enableRateLimit = True`; retries are bounded and never retry a submit unless
  the client order id makes it idempotent. A submit retry with the same client
  id is safe; a new id is not.
- Exchange time is fetched and compared with local time at startup. A skew above
  the configured limit aborts.
- The adapter always maps exchange errors into `ExecutionError` with a stable
  code; raw response bodies are redacted of keys.

## Credentials

Environment only:

| Variable | Meaning |
|---|---|
| `NOMICRONIX_BINANCE_API_KEY` | Trade-only key, withdrawals disabled |
| `NOMICRONIX_BINANCE_API_SECRET` | Secret |

The key must be IP-restricted to the host. The CLI never accepts keys as flags,
never writes them to the config, and prints only the last four characters of the
key when identifying it. Missing credentials are a configuration error before
any order work begins.

## CLI

```bash
# Stage 1: build shadow intents from an approved signal file
uv run python -m nomicronix.execution shadow --signals results/btc-signals.json

# Inspect intents and the journal
uv run python -m nomicronix.execution status
uv run python -m nomicronix.execution show --intent nx-sma-cross:BTC/USDT:1h:1735776000000

# Stage 2: submit one intent (requires config dry_run=false and --live)
uv run python -m nomicronix.execution submit \
    --intent nx-sma-cross:BTC/USDT:1h:1735776000000 \
    --confirm "PLACE ORDER" --live

# Cancel, reconcile, panic
uv run python -m nomicronix.execution cancel --intent nx-...
uv run python -m nomicronix.execution reconcile
uv run python -m nomicronix.execution flatten --confirm "FLATTEN NOW"
```

| Option | Meaning |
|---|---|
| `--signals` | Approved signal file (`schema_version = 1`, `kind = "signals"`) |
| `--execution-db` | Order journal; default `data/execution.sqlite` |
| `--risk-db` | Risk database; default `data/risk.sqlite` |
| `--risk-config` | Risk limits; default `config/risk.toml` |
| `--config` | Execution config; default `config/execution.toml` |
| `--intent` | Intent id for `show`, `submit`, `cancel` |
| `--confirm` | Exact confirmation phrase |
| `--live` | Required, together with `dry_run = false`, to submit |
| `--json` | Machine-readable output for `status`, `show`, `reconcile` |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Command completed, including zero intents |
| `1` | Invalid input, config, credentials, intent state or confirmation phrase |
| `2` | A risk check rejected the signal or order submission is not allowed |
| `3` | Exchange or reconciliation error; books are not proven consistent |
| `4` | Shadow or human-approval stage only: live submission is disabled |

Rejections and refusals are journaled, never silently dropped.

## Configuration

`config/execution.toml`:

```toml
[execution]
exchange = "binance"
market_type = "spot"
dry_run = true
stage = "shadow"
max_slippage_bps = 50
max_intent_age_ms = 300000
order_timeout_ms = 60000
poll_interval_ms = 1000
max_clock_skew_ms = 2000

[risk]
require_kill_switch_clear = true
```

Unknown keys, a non-spot `market_type`, `stage = "autonomous"` in this spec
revision, negative bps or a `dry_run = false` file without an explicit
`allow_live = true` are all rejected. `dry_run` defaults to `true`.

## Persistence

One SQLite database at `data/execution.sqlite` (WAL):

- `intents`: intent id (primary key), signal id, symbol, side, quantity, entry,
  stop, target, approved notional, timestamps and state.
- `orders`: intent id and exchange order id, status, filled quantity, average
  price, fee and last update time.
- `events`: append-only journal with autoincrement id, intent id, event type,
  timestamp and payload JSON.

Execution writes risk state only through `RiskService`. It never writes
`risk_state`, `positions`, `approvals` or `journal` tables directly.

## Reconciliation

`reconcile` is mandatory before any new submission and is safe to re-run:

1. Fetch open orders and balances from the exchange.
2. Compare every local `submitted` intent with an exchange order by client id.
3. Journal unknown exchange orders, local intents missing on the exchange, and
   positions that do not match the risk book.
4. Mark the run `clean` only when all three sets match, prices and quantities
   included.
5. If not clean, refuse submissions until the owner resolves it. There is no
   "ignore" flag.

## Testing

- Fake exchange adapter with scripted status transitions, partial fills, errors,
  timeouts and duplicate submits.
- Exact-phrase tests for `PLACE ORDER` and `FLATTEN NOW`.
- Slippage, expiry, kill-switch and double-submit guards each have a test.
- Reconciliation tests for matched, missing and unknown orders.
- One opt-in network contract test that only reads public data; no CI job ever
  uses real credentials.
- Module coverage floor: 90%, with the adapter's real network code excluded and
  exercised by the opt-in test only.

## Guarantees

- **No order without approval.** Submission requires an approved risk decision,
  the exact confirmation phrase, `dry_run = false`, `--live`, and a stage that
  allows live orders.
- **No duplicate order.** The signal id is the client order id.
- **No silent state.** Every intent and transition is journaled before output.
- **No unverified books.** Reconciliation must be clean before submitting.
- **No withdrawal path.** The module has no withdrawal method and requires a
  trade-only key.
- **No secrets.** Credentials are environment-only and redacted from output.

## Known limitations

- Binance only, spot only, long only, market orders only.
- Stage 3 autonomy is out of scope; this spec stops at human-approved orders.
- Partial fills are supported by `risk-engine` v1.1; execution still has to
  aggregate exchange pieces and report them exactly once.
- No portfolio margin, OCO brackets or exchange-side stops; protective stops stay
  in `signal-delivery`/`risk-engine` until a later spec adds exchange stops.
- No multi-account, sub-account or portfolio rebalancing support.
- Live trading is never risk-free, and nothing here is a profit guarantee.

## Decisions taken for the spec's open questions

| Question | Decision |
|---|---|
| When may execution be built | Only after `signal-delivery readiness` passes and the owner approves this spec |
| First stage | Shadow intents with no private call |
| Order type | Spot market order with a slippage ceiling |
| Approval | Exact phrase `PLACE ORDER` plus `dry_run = false` and `--live` |
| Idempotency | Client order id derived from the signal id |
| Partial fills | Aggregate, report once; requires risk-engine v1.1 |
| Reconciliation | Mandatory before submit; not clean means no trading |
| Kill switch | Blocks submissions; `flatten` only reduces exposure |
| Credentials | Environment-only, trade-only, withdrawals disabled |
| Autonomy | Out of scope; a separate future spec revision |
