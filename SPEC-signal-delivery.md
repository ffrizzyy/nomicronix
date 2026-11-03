# Spec: signal-delivery

Module id: `signal-delivery` (depends on: strategy-engine, risk-engine)

## Objective

Carry an approved signal candidate to a human channel and keep an honest paper
record of what happened next. The module is the bridge between
`strategy-engine` (proposes) and `risk-engine` (decides): it never approves a
signal itself and never invents prices.

It answers two questions:

1. *Which signal candidates passed risk, and how do I see them?*
2. *If each approved candidate had been taken on paper, what is the P&L?*

**Consumers:** the owner via the CLI and Telegram. `execution` will later read
the same approval path instead of paper-filling it.

## Non-responsibilities

- No signal generation. It consumes `SignalBatch` output or `SignalCandidate`
  objects from `strategy-engine`.
- No position sizing, limit checks or kill switch. Only `RiskService.check`
  decides, and only `RiskService.record_fill` opens or closes a position.
- No order placement, exchange client, API key for trading, or leverage.
- No scheduler and no long-running daemon in v1. The CLI is invoked by cron, a
  systemd timer or the owner.

## Scope of v1

- Deliver a `strategy-engine` result JSON file (`kind = "signals"`).
- Risk-check every signal at its candle-close time, so replay is deterministic.
- Persist an outbox row per signal and send it to one or more sinks:
  `console` (stdout), `jsonl` (append-only file) and `telegram` (optional,
  environment credentials).
- Skip a signal id that was already delivered; retry a `pending` or `failed`
  outbox row without re-checking risk.
- Paper-track approved signals: open at entry, close at stop or target on
  `tick`, and report realized P&L.
- Deterministic text and JSON. No wall clock in a notification; the signal's
  candle close is the event time.

## Domain model

All prices and money are `Decimal`; times are UTC epoch milliseconds. Confidence
is the only `float`.

**`Notification`** — the stable thing a sink receives:

| Field | Type | Meaning |
|---|---|---|
| `signal_id` | `str` | `strategy:symbol:timeframe:candle_open_ms` |
| `strategy_id` | `str` | Proposal source |
| `symbol`, `timeframe` | `str` | Market identity |
| `side` | `str` | `buy` in v1 |
| `entry_price`, `stop_price`, `target_price` | `Decimal` | Trade levels |
| `confidence` | `float` | Copied from the signal |
| `rationale` | `str` | Copied from the signal |
| `created_at_ms` | `int` | Candle close |
| `expires_at_ms` | `int` | Signal expiry |
| `approved` | `bool` | Risk decision |
| `approved_quantity`, `approved_notional` | `Decimal` | Zero when rejected |
| `risk_amount`, `reward_risk` | `Decimal` | Zero when rejected |
| `reasons` | `tuple[str, ...]` | Stable rejection reasons; empty on approval |

**`PaperTrade`** — one approved signal tracked on paper:

| Field | Type | Meaning |
|---|---|---|
| `signal_id` | `str` | Primary key; one trade per signal |
| `symbol` | `str` | Market |
| `quantity` | `Decimal` | Approved quantity |
| `entry_price`, `stop_price`, `target_price` | `Decimal` | Trade levels |
| `opened_at_ms` | `int` | Signal candle close |
| `closed_at_ms` | `int \| None` | Close time, `None` while open |
| `exit_price` | `Decimal \| None` | Fill price, `None` while open |
| `exit_reason` | `str \| None` | `stop` or `target` |
| `realized_pnl` | `Decimal \| None` | Net of zero fees in v1 |

**`DeliveryRecord`** — outbox and audit row: signal id, status (`pending`,
`delivered`, `rejected`, `failed`), notification payload and error text.

## Delivery flow

For each signal in file order:

1. If a delivery row exists for `signal_id`:
   - `delivered` or `rejected`: skip and report as such.
   - `pending` or `failed`: resend the stored payload; do not re-check risk.
2. Mark the risk account at the signal's candle close, using the signal entry
   price for its symbol. This keeps replay state fresh without a wall clock.
3. Build a `TradeProposal` and call `RiskService.check` with
   `now_ms = signal.created_at_ms`.
4. If rejected: store a `rejected` outbox row and do not send a trade alert.
5. If approved: call `RiskService.record_fill` with one full OPEN fill at the
   entry price, fee zero. Persist the `pending` outbox row and the paper trade.
6. Send the notification to every configured sink. Mark the row `delivered`; on
   a sink error mark it `failed` and raise `DeliveryError`.

The outbox makes local delivery idempotent. A crash between the risk fill and
the outbox insert is the one at-most-once window; it is documented and does not
create a second position.

## Paper tracking

`tick` takes a price map and a time. For each open trade whose symbol has a
price:

- `price <= stop`: close at `min(stop, price)` with reason `stop`.
- `price >= target`: close at `target` with reason `target`.
- otherwise leave it open and mark the risk account.

Stops are checked before targets. This is conservative and deterministic. The
close is a full `RiskService.record_fill` CLOSE with zero fee. v1 ignores
partial fills and slippage; those belong to `execution`.

## Sinks

```python
class Sink(Protocol):
    name: str
    def send(self, notification: Notification) -> None: ...
```

- **`console`** prints a stable multi-line message to stdout.
- **`jsonl`** appends one compact JSON object per notification, sorted keys.
  Each write is a single `write` call followed by `flush`.
- **`telegram`** sends the rendered text through the Bot API. Credentials come
  only from `NOMICRONIX_TELEGRAM_BOT_TOKEN` and
  `NOMICRONIX_TELEGRAM_CHAT_ID`. Missing credentials are a configuration error.
  Tokens are never logged or stored. Tests inject a fake transport; no test
  touches the network.

A notification always includes the signal id, so a consumer can de-duplicate
even if a retry happens after a process crash.

## CLI

```bash
# Risk-check and deliver a strategy-engine result
uv run python -m nomicronix.signal_delivery deliver \
    --signals results/btc-signals.json \
    --sink console --sink jsonl --jsonl data/deliveries.jsonl

# Optional Telegram sink (credentials from the environment)
export NOMICRONIX_TELEGRAM_BOT_TOKEN=...
export NOMICRONIX_TELEGRAM_CHAT_ID=...
uv run python -m nomicronix.signal_delivery deliver \
    --signals results/btc-signals.json --sink telegram

# Mark open paper trades against fresh prices
uv run python -m nomicronix.signal_delivery tick \
    --price BTC/USDT=94.50 --at 1735779900000

# Delivery and paper P&L report
uv run python -m nomicronix.signal_delivery report
```

- `--delivery-db` defaults to `data/delivery.sqlite`; `--risk-db` to
  `data/risk.sqlite`.
- `--cash` defaults to 1,000 USDT, matching the risk account.
- Instrument filters default to `quantity_step=0.00001`, `min_quantity=0.00001`
  and `min_notional=10`; `--quantity-step`, `--min-quantity` and
  `--min-notional` override them.
- `--config` defaults to `config/delivery.toml`; `--risk-config` to
  `config/risk.toml`.
- `--sink` is repeatable and overrides the config list.
- `tick` accepts `--prices FILE` (JSON object of symbol to price) or repeated
  `--price SYMBOL=PRICE`. `--at` defaults to the current UTC time.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Command completed, including zero delivered or zero closed trades |
| `1` | Invalid input, config, signal file, store or price map |
| `2` | A configured sink failed to send |

Rejected signals do not make `deliver` exit nonzero; risk did its job.

## Configuration file

`config/delivery.toml` holds defaults:

```toml
[delivery]
sinks = ["console"]
jsonl_path = "data/deliveries.jsonl"
quantity_step = "0.00001"
min_quantity = "0.00001"
min_notional = "10"
```

Unknown sink names, non-positive instrument values and malformed TOML are
rejected. CLI flags override file values. Selecting `telegram` without both
environment variables fails before any delivery.

## Persistence

One SQLite database at `data/delivery.sqlite`:

- `deliveries`: one row per signal id with status, payload JSON, error and
  record time. Primary key `signal_id`.
- `paper_trades`: one row per approved signal id with entry, stop, target,
  quantity, open/close times, exit price/reason and realized P&L. Primary key
  `signal_id`.

The risk database remains the source of truth for cash, equity, open positions
and the decision journal. Delivery never writes those tables directly; it calls
`RiskService`.

## Guarantees

- **Risk decides.** No signal is delivered as tradeable without an approved
  `RiskDecision`; no position is opened except through `record_fill`.
- **Deterministic replay.** Signal time comes from the candle, not the wall
  clock. Re-running the same file with the same databases does not duplicate a
  delivery or a trade.
- **Auditable.** Every consumed signal has an outbox row, including rejected
  ones, and every paper trade names its signal id.
- **No secrets in git.** Telegram credentials are environment-only and never
  serialized.
- **Explicit failure.** Bad files, bad prices and sink errors fail loudly with
  nonzero exit codes; nothing is silently dropped.

## Known limitations

- Paper fills are full, fee-free and at the planned price. Real execution will
  add fees, slippage and partial fills.
- One position per symbol, inherited from `risk-engine`; no pyramiding.
- No scheduler, retry daemon or rate-limit handling. `tick` is owner-invoked.
- Telegram is the only remote sink. Email and webhooks are future work.
- Signals are research inputs and paper trades are not evidence of future
  profit.

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
