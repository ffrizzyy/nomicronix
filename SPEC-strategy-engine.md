# Spec: strategy-engine

Module id: `strategy-engine` (depends on: market-data)

## Objective

Turn stored candles into fully specified, causal trade ideas. A **signal
candidate** is an entry price, a stop, a target, a confidence and a written
rationale for one symbol, timeframe and candle close. It is a proposal, not an
order and not an approval.

The module answers one question: *given only the candles that had closed by time
T, what long trade idea, if any, does this strategy have?*

**Consumers:** `signal-delivery` (delivers and risk-checks each candidate) and
the owner via the CLI. Nothing else may consume raw candles to invent signals.

**User stories**

- As the owner, I can scan stored candles with a named strategy and get zero or
  more signals as deterministic JSON.
- As the owner, I can see why a signal fired: the triggering candle, the
  indicators and a plain-language rationale.
- As `signal-delivery`, I can read a stable signal contract and pass each
  candidate to `risk-engine` without re-deriving prices.
- As the owner, I can add a new deterministic or AI proposer without changing
  the downstream contract.

## Non-responsibilities

- No position sizing, limit checks, kill switch or approval. That is
  `risk-engine`, and it is the only module allowed to decide.
- No order placement, exchange client or API key.
- No delivery, scheduling, Telegram or P&L tracking.
- No portfolio awareness. The module does not know whether a position is open.
  `signal-delivery` and `risk-engine` enforce duplicate and exposure rules.
- No network calls in v1. No LLM provider is wired.

## Scope of v1

- One symbol, one timeframe and one proposer per scan.
- Long-only spot signals. Every candidate has `side = buy` and a stop below
  entry plus a target above entry.
- Deterministic, standard-library-only implementation. `Decimal` for prices and
  money; `float` only for confidence.
- Built-in deterministic proposers: SMA crossover and Donchian breakout.
- An explicit `AIProposer` protocol and validated `AIProposal` model as the
  extension point for a future model-backed proposer.
- Deterministic JSON output and a CLI.

## Domain model

All prices are `Decimal`, all times are UTC epoch milliseconds (`*_ms`), and all
ranges are half-open: `start_ms <= open_time_ms < end_ms`.

**`SignalIdea`** — what a proposer returns, with no identity or timestamps:

| Field | Type | Meaning |
|---|---|---|
| `entry_price` | `Decimal` | Reference entry, normally the decision candle close |
| `stop_price` | `Decimal` | Protective stop, strictly below entry |
| `target_price` | `Decimal` | Take-profit, strictly above entry |
| `confidence` | `float` | Proposer's score in `[0, 1]`; never a bypass |
| `rationale` | `str` | Short, deterministic explanation |
| `features` | `Mapping[str, str]` | Indicator snapshot, values as exact decimal strings |

**`SignalCandidate`** — a validated idea with provenance:

| Field | Type | Meaning |
|---|---|---|
| `signal_id` | `str` | Deterministic id: `strategy: symbol: timeframe: candle_open_ms` |
| `strategy_id` | `str` | Stable strategy name |
| `symbol`, `timeframe` | `str` | Market identity |
| `side` | `Side` | `buy` only in v1 |
| `entry_price`, `stop_price`, `target_price` | `Decimal` | Validated trade levels |
| `confidence` | `float` | Copied from the idea |
| `rationale` | `str` | Copied from the idea |
| `features` | `Mapping[str, str]` | Copied from the idea |
| `created_at_ms` | `int` | Candle close time: `candle.open_time_ms + timeframe.ms` |
| `expires_at_ms` | `int` | `created_at_ms + ttl_ms` |
| `candle_open_time_ms` | `int` | Provenance: the candle that triggered it |

**`ScanConfig`** — requested range, warm-up and output policy:

| Field | Default | Rule |
|---|---|---|
| `symbol`, `timeframe` | required | Supported timeframe from `market_data.models` |
| `start_ms`, `end_ms` | required | `start_ms < end_ms` |
| `min_history` | 50 | Candles the proposer needs before it may fire |
| `min_confidence` | 0.5 | Ideas below this are dropped with a warning |
| `ttl_ms` | 300_000 | Must be positive; matches risk-engine's 5-minute signal age |
| `max_signals` | 100 | Hard cap; exceeding it is an error, not silent truncation |

**`SignalBatch`** — one scan result: `config`, `data` metadata, `signals` (tuple)
and `warnings` (tuple).

## Proposer contract

```python
class SignalProposer(Protocol):
    strategy_id: str
    min_history: int

    def propose(self, view: CandleView) -> SignalIdea | None: ...
```

- `CandleView` exposes `candle` (the decision candle) and `history` (a read-only
  `Sequence[Candle]` ending at that candle). It cannot expose a later candle.
- `propose` is called once per closed candle, in ascending order, after at least
  `min_history` candles are available.
- Returning `None` means no idea. Raising `ProposalError` records a warning and
  the scan continues; any other exception aborts the scan.
- Proposers are stateless or constructed fresh per scan. Calling the same
  proposer twice with the same view must return the same idea.

### Built-in proposers

**`SmaCross`** (`strategy_id = "sma-cross"`)

- Params: `fast` (20), `slow` (50), `atr_period` (14), `stop_atr` (2.0),
  `target_rr` (3.0), `confidence` (0.6).
- Fires when the fast SMA crosses strictly above the slow SMA on the decision
  candle: previous fast `<=` previous slow and current fast `>` current slow.
- `entry = close`, `stop = entry - stop_atr * ATR(atr_period)`,
  `target = entry + target_rr * (entry - stop)`.
- Requires `slow + 1` and `atr_period + 1` closed candles; `min_history` is the
  larger of the two.
- Validates `fast < slow` and positive ATR parameters at construction.

**`DonchianBreakout`** (`strategy_id = "donchian-breakout"`)

- Params: `entry_period` (20), `stop_atr` (2.0), `target_rr` (2.0),
  `confidence` (0.55).
- Fires on a *fresh* breakout: the decision close is above the highest high of
  the previous `entry_period` candles, while the previous close was not above
  the corresponding prior channel. This prevents a signal on every candle of a
  sustained trend.
- Requires `entry_period + 1` candles; ATR uses 14 candles.

Both use **Wilder's ATR** over true ranges. If ATR is zero or the computed stop
is not below entry, the proposer returns `None`; it never emits an invalid idea.

### AI extension point

```python
class AIProposer(Protocol):
    model_id: str

    def propose(self, request: ProposalRequest) -> AIProposal | None: ...
```

- `ProposalRequest` carries the symbol, timeframe, decision candle, a bounded
  recent-history summary and explicit output constraints.
- `AIProposal` is validated exactly like `SignalIdea`: entry, stop and target
  are required, stops must be below entry, targets above entry, confidence in
  `[0, 1]`, rationale non-empty.
- v1 ships no provider and makes no network call. A future provider adapter must
  use the same validation and record a model id in `features["model_id"]`.
- An AI proposal is still only a candidate. `risk-engine` remains the decider.

## Causal and data rules

- A scan may read candles before `start_ms` for warm-up, but signals are emitted
  only for candles with `start_ms <= open_time_ms < end_ms`.
- `created_at_ms` is the decision candle's close, never the wall clock, so reruns
  are reproducible.
- Candles must be strictly ascending and free of duplicates. A gap anywhere in
  the loaded range raises `DataGapError` with the missing ranges; a series
  shorter than `min_history` raises `InsufficientDataError`.
- The scan stops at the first candle that would exceed `max_signals`; it does
  not return a truncated batch.

## Validation and rejection

A `SignalIdea` is rejected with a warning when:

- entry, stop or target is non-positive;
- `stop >= entry` or `target <= entry`;
- confidence is outside `[0, 1]`;
- rationale is empty;
- confidence is below `min_confidence`.

Rejected ideas never become candidates. Accepted candidates always satisfy the
long-only geometry above.

## Determinism

- `signal_id` is `f"{strategy_id}:{symbol}:{timeframe}:{candle_open_time_ms}"`.
- Features are serialized as exact decimal strings with trailing zeros trimmed.
- JSON uses `sort_keys=True`, fixed decimal formatting and a trailing newline.
  Two identical scans produce byte-identical files.
- No wall clock, randomness or absolute paths appear in result JSON.

## Output contract

```json
{
  "schema_version": 1,
  "kind": "signals",
  "config": {
    "symbol": "BTC/USDT",
    "timeframe": "1h",
    "start_ms": 1735689600000,
    "end_ms": 1738368000000,
    "min_history": 50,
    "min_confidence": 0.5,
    "ttl_ms": 300000,
    "max_signals": 100
  },
  "data": {
    "symbol": "BTC/USDT",
    "timeframe": "1h",
    "first_candle_ms": 1735689600000,
    "last_candle_ms": 1738364400000,
    "candle_count": 744
  },
  "signals": [
    {
      "signal_id": "sma-cross:BTC/USDT:1h:1735776000000",
      "strategy_id": "sma-cross",
      "symbol": "BTC/USDT",
      "timeframe": "1h",
      "side": "buy",
      "entry_price": "100",
      "stop_price": "95",
      "target_price": "115",
      "confidence": 0.6,
      "rationale": "fast SMA crossed above slow SMA",
      "features": {"atr": "2.5", "fast_sma": "99.5", "slow_sma": "98.1"},
      "created_at_ms": 1735779600000,
      "expires_at_ms": 1735779900000,
      "candle_open_time_ms": 1735776000000
    }
  ],
  "warnings": []
}
```

Money and prices are JSON strings. Confidence is a JSON number. `warnings`
records dropped ideas and scan-level notes in a stable order.

## CLI

```bash
uv run python -m nomicronix.strategy_engine scan \
    --strategy examples.strategy_models:SmaCross \
    --param fast=20 --param slow=50 \
    --symbol BTC/USDT --timeframe 1h \
    --since 2025-01-01 --until 2025-02-01 \
    --min-confidence 0.5 --ttl 5m \
    --out results/btc-signals.json
```

- `--db` defaults to `data/market.sqlite`; `--exchange` defaults to `binance`.
- `--strategy` is `module:Class`; params parse as integer, then float, then
  string, and unknown params are constructor errors.
- `--since` and `--until` are UTC dates or epoch milliseconds; the range is
  half-open.
- `--min-history`, `--min-confidence`, `--ttl`, `--max-signals`, `--out`,
  `--force` are available.
- The summary lists each signal id, entry, stop, target and confidence, then the
  count. `--out` writes JSON atomically and refuses to overwrite without
  `--force`.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Scan completed, including zero signals |
| `1` | Invalid config, bad data or malformed argument |
| `2` | Strategy import, constructor or proposal-contract error |
| `3` | Output exists and `--force` was not passed |

## Configuration file

`config/strategy.toml` holds scan defaults, not strategy parameters:

```toml
[scan]
min_confidence = "0.5"
ttl_ms = 300000
min_history = 50
max_signals = 100
```

CLI flags override file values. Missing file falls back to the defaults above.
Percentages and confidence are TOML strings parsed as `Decimal` then converted
to `float` only at the model boundary. Malformed TOML, non-positive TTL,
confidence outside `[0, 1]` and non-positive counts are rejected.

## Guarantees

- **Causal.** `CandleView` physically slices at the decision index; a spy test
  proves no later candle is reachable.
- **Proposal only.** The module cannot size, approve or place anything.
- **Deterministic.** Same candles, strategy and config produce identical signal
  ids, ordering and bytes.
- **Explicit failure.** Bad data, bad params and overflowing signal counts fail
  loudly; dropped ideas appear in `warnings`.
- **Reproducible provenance.** Every signal names its strategy, its trigger
  candle and its exact indicator values.

## Known limitations (v1)

- No portfolio state: a signal may fire while a position is already open. Risk
  and duplicate rules decide downstream.
- Rule-based confidences are fixed constants, not calibrated probabilities.
- No AI/LLM provider is shipped; only the validated extension point.
- No multi-timeframe confluence, order-book or funding inputs.
- No shorting, leverage or futures.
- Signals are research inputs, never orders and never profit guarantees.

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
| Multiple signals per candle | Allowed by the contract; built-ins fire on fresh events only |
| Portfolio awareness | None in v1; documented and delegated downstream |
| Output | Deterministic JSON plus a human-readable CLI summary |
