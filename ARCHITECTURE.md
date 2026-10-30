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
                        │  app/errors.py  unhandled → log + count  │
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

### Monitoring the deployment (without waking it)

- **`/api/status`** is the diagnostics surface: uptime, requests served, the
  deployed revision (`RENDER_GIT_COMMIT`), model + key, cache size, and the
  number of distinct clients in the rate window. Every field is process-local
  and resets on restart; `uptime_s` is what tells "recently restarted" from
  "nothing happened". `/api/health` only says "alive".
- **`scripts/check_deploy.py`** reads `/robots.txt` to learn the sleep state
  without waking the service — the one probe that observes without intervening.
  It also watches itself: a last reading older than `--gap-threshold` hours
  means the schedule stopped firing, and that is alerted, because a watchdog
  that never runs is silent in exactly the way a healthy service is.
- **The watcher runs on push** (`deploy-watch.yml`), not only on a cron.
  GitHub's `schedule` is best-effort and heavily throttled — this workflow had a
  `*/30` cron and produced zero scheduled runs, while a sibling repo's `*/5`
  cron fired every 4–6 hours. The push trigger is the reliable event; the cron
  stays as a backstop. On push the check also waits for the pushed revision to
  become the running one, so a green run proves the deploy landed rather than
  that "something is serving".
- **`app/errors.py`** records unhandled exceptions as one JSON line on stdout
  (Render captures it — that is the durable log) and keeps an aggregate count by
  type for `/api/status`. The full traceback stays in the log; the caller gets a
  generic 500 with the request id, never the traceback. Any configured secret
  that appears in the text is redacted, and the type map is capped. On push the
  watcher reads that count (`--max-errors`) — a deploy that answers `/api/health`
  while throwing on real requests is exactly the failure a liveness probe cannot
  see. A market source being down is a 502 with a reason, and is *not* counted:
  an upstream outage must not read as a bug of ours.

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
- **Monitoring is a heartbeat plus a local error log, not full observability.**
  Unhandled exceptions are recorded as one structured line on stdout (which
  Render captures) and counted in memory, and `/api/status` reports the count
  by type. There is deliberately **no third-party tracker**: sending stack
  traces and request context to an external account is not a decision this
  project makes on the owner's behalf. What is missing as a result: no
  cross-process aggregation (a restart resets the count — `uptime_s` is the
  tell), no alerting on error *rate*, no tracing across the upstream calls, and
  no error tracking at all for a request that is served but returns the wrong
  number — only for one that raises. The probe runs on push and on a throttled
  cron, so detection can lag a real outage by hours; that is stated here rather
  than implied to be instant.
- **An upstream outage is not counted as our error.** A market source being
  down is reported to the caller as 502 with the reason, and is excluded from
  the error count on purpose — a source outage must not read as the service
  being broken, or the count becomes noise and is ignored.

## Testing philosophy

The suite runs against the real code paths, not mocks. Network-touching tests
are marked `network` and deselected in CI, so a red build means our code broke,
not that a provider was slow. The market layer's parsing is pinned with canned
payloads (every venue, every error path); the browser helpers are pinned under
`node --test`. A green suite is treated as necessary, not sufficient — the
defects that mattered most were found by reading a path no test exercised.
