# Architecture

How AnalysisDesk is put together, why it is split the way it is, and where the
limits are. The README says what it does; this says how, so a reader can decide
whether a change belongs in a given layer.

## The one rule everything else follows

Every number on the screen is computed from a real OHLC candle. Nothing is
estimated, interpolated, or invented — not by the engine and not by the model.
The layers below exist to keep that true, and each one fails *closed*: when it
cannot do its job honestly, it says so rather than returning a friendly default.

## Layers

```
                        ┌─────────────────────────────────────────┐
  browser  ──HTTP──▶    │  app/main.py        FastAPI, routing     │
                        │  ─────────────────────────────────────── │
                        │  guard: API key, per-IP rate limit       │
                        │  headers: CSP, HSTS, nosniff, frame, ... │
                        └───────────────┬─────────────────────────┘
                                        │
                ┌───────────────────────┼───────────────────────┐
                ▼                       ▼                       ▼
        ┌───────────────┐      ┌────────────────┐      ┌────────────────┐
        │ app/market.py │      │ app/indicators │      │   app/ai.py    │
        │  data in      │─────▶│  math          │─────▶│  prose only    │
        └───────┬───────┘      └───────┬────────┘      └───────┬────────┘
                │                      │                       │
                ▼                      ▼                       ▼
          Yahoo / Kraken          app/signals.py            Groq models
          Coinbase / Swissquote   the verdict               (optional)
          gold-api

  web/index.html + web/lib.js + web/app.js  ──  the card (RTL, bilingual)
```

### `app/market.py` — the only place a price enters

Talks to the outside world and returns a `Series` of real candles. Two venues:
Yahoo for futures/forex/crypto/indices, and a spot path for XAUUSD (Kraken
PAXG/USD primary, Coinbase fallback) because there is no keyless spot-gold candle
feed. A separate `fetch_spot` returns the live broker quote (Swissquote, then
gold-api) that the card prints beside the chart.

Rules this layer keeps:

- **No synthetic prices, ever.** A failed read raises `MarketDataError`; it never
  returns a filled or carried-forward value.
- **A gap is dropped, not patched.** A candle with a null field is skipped.
- **Both venues share one cache** (TTL per timeframe, 256 entries, oldest
  evicted). Spot is not special-cased out of it.

### `app/indicators.py` — the math

Pure functions over a `pd.DataFrame`. RSI, MACD, ATR, Bollinger, Stochastic,
EMA stack, swing support/resistance. `price_decimals` and `_clean` live here and
are the **single source** of how many decimals a value of a given magnitude
deserves — the API, the signal text and the browser card all derive from it.

### `app/signals.py` — the verdict

Turns a board into a `Signal`: a direction, a confidence, an entry/stop/target,
and a list of rules that each carry the weight they contributed. Two invariants:

- **`confidence` is indicator agreement, not a probability of profit.** The word
  "probability" does not appear on any surface; a test asserts it.
- **No majority means NEUTRAL**, and a broken board (non-finite ATR, zero or
  negative price) means NEUTRAL with a warning — never a fabricated direction.

### `app/ai.py` — prose only

The model's only job is to explain facts the engine already computed. It cannot
introduce a number:

1. The engine builds a **fact sheet** of pre-resolved tokens
   (`price_vs_ema20=ABOVE`), so the model only has to quote, not re-derive.
2. `_verify` rejects the prose if it invents a figure not in the sheet, uses
   banned profit language, or states a relation backwards.
3. `_strip_markup` removes anything a browser would parse as a tag, so the
   payload is gone before it leaves the server (the client escapes as well).

If every model fails or contradicts the facts, the desk falls back to a
**computed sentence** built from the same numbers — and reports `model: null`,
because no model wrote it.

### `web/` — the card

`index.html` (markup + CSS), `lib.js` (DOM-free helpers: `esc`, `fmt`,
`priceDecimals`, `basisPct`), `app.js` (DOM + rendering). `lib.js` is a module so
the browser and `node --test` share one copy of the escaping and precision rules
— the two defects that lived only in the browser before.

## Data flow for one request

```
GET /api/analyze?symbol=XAUUSD&interval=1h&lang=ar
  guard()            key (if configured) → per-IP rate limit
  market.fetch_series()   cache? → Kraken → Coinbase → Series(real candles)
  indicators.snapshot()   → board of computed numbers (one precision rule)
  signals.evaluate()      → direction, confidence, levels, rules, warnings
  ai.narrate()            → fact sheet → model → verify → strip → prose
  market.fetch_spot()     → live broker quote, shown beside the chart
  → JSON: indicators + signal + narrative + instrument + spot
```

Every field the card renders is escaped at the boundary; every figure is
rounded by the one rule in `indicators`.

## Topology and deployment

- **One container, one process** (see the Dockerfile). `--workers 1` is
  deliberate: the rate limiter counts in process memory, so one worker makes the
  in-process counter the instance counter. Raising it would multiply the limit
  by the worker count — do that only after moving the counter to a shared store.
- Runs as **unprivileged `desk` (uid 10001)** with a `HEALTHCHECK`.
- **Render Free**, via `render.yaml` (Docker, `autoDeploy: true`). The instance
  **sleeps after ~15 minutes idle** and cold-starts on the next request. Nothing
  is persisted; caches are in memory, so a cold start costs latency, not
  correctness.
- `GROQ_API_KEY` is declared in the blueprint with `sync: false` — set in the
  dashboard, never in git. Without it the desk still works (computed prose).
- **The image installs `requirements.lock`**, not `requirements.txt`: the lock is
  the resolved, hash-pinned set, installed with `--require-hashes`, so a rebuild
  produces the same bytes and a substituted package is refused. It is compiled
  for the **lowest** supported Python (3.11), not the image's 3.12, so it
  installs on every version CI tests. `requirements.txt` declares the ranges;
  `scripts/check_lock.py` fails the build if the two drift (a bumped range with a
  stale pin, or a lock compiled for the wrong Python). A `docker` CI job builds
  the image and waits for `/api/health`, because "green in CI" and "the image
  builds" are two facts.

## Honest limits

Stated here rather than left for a reader to discover:

- **The rate limit does not survive a restart and is not shared across
  replicas.** It stops casual scraping and runaway loops, which is what
  threatens the quota. A shared counter (Redis) is the fix if the deployment
  ever scales past one instance.
- **Spot gold is charted from a gold-backed token** (PAXG/USD), not a broker's
  XAU/USD feed, because no keyless spot-gold candle source exists. The real
  broker quote is shown beside it, and the gap between the two is labelled.
- **The AI layer is optional and non-authoritative.** With no key, a bad key, or
  a rate limit, the desk degrades to the computed sentence and says why.
- **`HEAD`/`GET` on an unknown symbol** returns "could not read", never a guess.
- **The Free instance sleeps**, so the first request after idle is slow.

## Testing philosophy

The suite runs against the real code paths, not mocks. Network-touching tests
are marked `network` and deselected in CI, so a red build means our code broke,
not that a provider was slow. The market layer's parsing is pinned with canned
payloads (every venue, every error path); the browser helpers are pinned under
`node --test`. A green suite is treated as necessary, not sufficient — the
defects that mattered most were found by reading a path no test exercised.
