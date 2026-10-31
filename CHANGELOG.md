# Changelog

Notable changes, newest first. Versions are semantic; the current one is the
`version` in `pyproject.toml`, which is also what `/api/status` reports.

## 1.1.0

The hardening pass at the top is the newest work; the feature work below it
landed earlier in the same release. One theme runs through the top: a claim the
code makes should be a claim the code can back.

### Fixed — hardening

- **Every successful AI read returned a 500.** `ai.narrate` sets `note=None`
  when a model wrote the prose (there is nothing to explain), but `NarrativeOut`
  typed `note` as a required `str`. The model-success path was the one path no
  API-level test drove — every `/api/analyze` test in the suite took the
  deterministic branch, where `note` is always a string — so the mismatch stayed
  invisible until a *valid* key reached production, and then every read that
  should have worked failed. The bug only surfaced because the key was fixed;
  it had been masked by the very outage it hid behind. `note` is `str | None`
  now, and a test drives the model-success path end to end.
- **`/api/news` returned a 500 whenever any feed failed.** `news.py` builds
  `sources_failed` as `[{name, error}]` so a reader can see *which* source to
  distrust, but `NewsOut` declared `list[str]`. The shape test stubbed the
  aggregator with an empty list, so the mismatch never reached the serializer.
  The schema now matches the richer object (`FeedFailureOut`), and two tests
  cover one feed down and every feed down. The `note`/`sources_failed` pair is
  the same failure twice: **95% line coverage proves the lines ran, not that the
  emitted body matches the declared contract.**
- **The interactive API docs served an empty shell.** `/docs` and `/redoc`
  answered 200 while loading their JavaScript from a CDN that our own
  `script-src 'self'` policy blocks — an all-clear over a page that never
  worked. They are no longer served. `/openapi.json` is kept: it is plain JSON,
  no CDN, and now describes the real shape (below).
- **The version had two homes.** It was typed in `app/main.py` and in
  `pyproject.toml`, so `/api/status` could disagree with the package with
  nothing to catch it. It is read from the one file now, with a fallback that
  reads the same `pyproject.toml` when the package is not installed (the
  container copies code without building a wheel — that is where the deployed
  image was reporting `0.0.0+unknown`).
- **Error strings ignored the language they were asked for.** A read failure
  came back in one language regardless of `lang`. There is one translation
  helper now, applied to every error the API returns.
- **An unvalidated symbol could reach the upstream URL path (partial SSRF).**
  The check lived only in the API layer, but `fetch_series` has other callers
  (`alerts.py`, the scripts), and the pattern did not refuse a `..` — so a symbol
  could walk the upstream path. The check moved to the URL boundary in
  `market.py` (one predicate, every caller), now rejects `..`, and the segment is
  percent-encoded before it is inserted. CodeQL flagged the original as "partial
  SSRF" and now passes; two tests pin the refusal and the encoding.
- **The MIT LICENSE was not detected by GitHub.** It carried a market-data
  disclaimer that made it "Other". The license is now standard MIT and the
  disclaimer moved to `NOTICE`, where it belongs.
- **The base image was pinned by tag only.** `python:3.12-slim` is a moving
  target, so a rebuild months from now pulled different bytes than CI tested.
  The `FROM` is pinned by multi-arch digest; the image is rebuilt and proven to
  answer as a non-root user.

### Added — hardening

- **A typed API contract.** Every data endpoint names a `response_model`
  (`app/schemas.py`), so `/openapi.json` describes the real fields and a renamed
  or dropped field fails a test instead of shipping to a client. The absent
  field is preserved: a failed spot quote omits `price`, it does not send
  `price: null` — "unknown" is not silently turned into a value.
- **CodeQL and secret scanning** (`.github/workflows/security.yml`), the first
  on every push and PR, the second over full history so a removed secret is
  still caught.
- **A favicon and Open Graph metadata**, so the shared link is not a blank card.
- **`SECURITY.md`** (disclosure policy) and a **`Makefile`** whose `make check`
  runs exactly what CI runs.
- **`NOTICE`** for the market-data disclaimer, split out of the license.

### Changed — hardening

- **Alerts are documented as off in production, because they are.** The API can
  arm them and `scripts/run_alerts.py` runs one pass, but no scheduler calls it
  on the free plan: no cron jobs, separate containers, ephemeral store. That is
  now stated in README and ARCHITECTURE rather than left to be discovered.
- **The test client's deprecation warning is gone.** Starlette 1.x prefers
  `httpx2` and falls back to `httpx` with a warning; `httpx2` is a dev
  dependency now, so the warning cannot become noise that hides the next real
  one. The runtime still imports `httpx`.

### Added — feature work

- **A backtest, so the desk's edge can be measured instead of asserted.**
  `GET /api/backtest` walks the signal engine forward over the real candles —
  no lookahead, one bar at a time — and returns its realised record: hit rate,
  expectancy in R, total R, max drawdown, and the sample size. The result
  carries `CAVEATS` on its face, and a small sample is flagged, not dressed up.
  On EURUSD 1h the honest answer is **negative** (expectancy on the order of
  −0.04R, moving with the data — the endpoint is the authority), and the desk
  says so rather than hiding it. Ambiguous bars resolve against the trade
  (the stop is checked before the target), because the optimistic rule — target
  first — is how a backtest lies.
- **A durable store.** `app/store.py` persists alert subscriptions and a visit
  counter to SQLite (`DESK_DATA_DIR`). Adds are idempotent, subscriptions are
  scoped to their owner's chat id, and `/api/status` reports whether the store
  is durable on this host — `durable: false` on Render's ephemeral disk is
  stated, not implied.
- **Price-crossing alerts.** `app/alerts.py` detects a level crossing in pure
  code (`crossed()`), and `/api/alerts` arms, lists, and removes them.
  **Telegram delivery is off by default** and only runs when `ALERTS_ENABLED` is
  set explicitly — an alert loop that starts messaging people because it can is
  not one this desk ships. A message that goes out says "تحليل آلي وليس توصية".
- **Keyless headlines.** `app/news.py` reads public RSS feeds with no API key
  and serves them at `GET /api/news`. Titles only — the feeds carry no body
  text, so "reading the news" here is headlines, and the surface never claims
  sentiment or impact. A feed that fails is reported as failed per source, so a
  silent feed is visible as an outage, not an empty list that looks like calm.
- **Performance tripwires.** Tests that fail on an accidental O(n²) in the
  indicator snapshot and on super-linear scaling, so a slow path is caught in
  CI rather than in a request.
- **Six targets per trade, not one.** The desk used to name a single take-profit.
  A trader scaling out needs the next levels, so every non-neutral read now
  carries a six-rung ladder (`signal.targets`). Each rung says where its number
  came from: `level` when it sits on a pivot the market actually printed, and
  `projection` when the pivots ran out and the rung is one stop-distance beyond
  the last. The two are labelled apart on the card — calling both "target" would
  report an assumption as a level. `take_profit` stays TP1, so nothing that read
  it before changed meaning. A neutral read has an empty ladder, not six invented
  numbers. The ladder reaches the AI fact sheet (`target_1`..`target_6`) and the
  invention guard's allow-list, so a rung on the card is quotable and a seventh
  number is still rejected as an invention.

### Security

- **The alerts API trusted a chat id as its credential.** `/api/alerts*` took a
  `chat_id` query parameter and nothing else, so anyone who knew or guessed a
  Telegram chat id could read, arm, and delete that user's alerts (proved
  against production: `GET /api/alerts?chat_id=123456789` answered `200` with no
  credential). A chat id is an *identifier*, not a secret — it leaks through
  groups and forwarded messages. The endpoints are service-to-service (the bot
  is the only caller; the bundled page never touches them), so the credential is
  now a shared token the bot holds, sent as `x-alerts-token`. With no token
  configured the endpoints answer `503` (closed) rather than serving anyone:
  failing open would just be the bypass under a new name. A chat id still selects
  the row, so a caller can only reach the alerts it names — but it is no longer
  the thing standing between a stranger and another user's data.
- **The rate limit was bypassable by a forged `X-Forwarded-For`.** The client
  identity was read from the *last* `X-Forwarded-For` hop on the theory that our
  edge writes it. That holds only when an edge exists: with nothing in front,
  the last hop is whatever the caller sent, so one script sent a fresh value per
  request, got a fresh bucket each time, and walked straight past the limit
  (reproduced: four spoofed requests, four `200`s against a limit of two). The
  header is now read **only** when `DESK_TRUST_PROXY` is set — which the Render
  blueprint does, because Render's router genuinely appends the real client as
  the last hop (verified in production: fifteen forged values collapsed to one
  bucket). Unset, the identity is the socket peer. The run command also passes
  `--no-proxy-headers`, because uvicorn otherwise rewrites that peer from the
  same header, which would have left the fallback forgeable too.
- **The API key was bypassable by a forged header.** With `DESK_API_KEY` set,
  the guard exempted any request carrying `Sec-Fetch-Site: same-origin`, on the
  reasoning that a browser writes that header and page script cannot. True of a
  browser, false of a script: `curl -H 'Sec-Fetch-Site: same-origin'` reached
  every data endpoint with no key at all, so the key protected nothing against
  the caller it exists to stop. A header the client sets is not a credential.
  The exemption is gone; when the key is set it is required from **every**
  caller, compared with `hmac.compare_digest`. A public page cannot hold a
  secret, so there is no honest page exemption to offer — the gate fails closed,
  and the docs now say the key is for an API-only deploy (production leaves it
  unset, so the public page is unaffected).
- **RSS is parsed with `defusedxml`.** `xml.etree` will happily expand a
  crafted entity payload ("billion laughs") in a feed we did not write; the
  parser now refuses entity expansion and external entities, and a malformed
  feed returns an empty list instead of raising.

### Engineering

- **One worker, on purpose.** The cache and the rate-limit counter live in
  memory, so running more than one worker would multiply the effective rate
  limit and split the cache. This is a single-worker service by design, and
  `README` says why and what a correct multi-worker setup would need.

### Fixed

- **The price the desk showed was not the price the user's broker shows.**
  Spot gold is charted from a gold-backed token (PAXG/USD) because there is no
  keyless spot-gold candle feed. The token trades at a small premium, so the big
  number — and every level under it: entry, stop, target, support, resistance —
  was on the token's scale, ~0.2% above the real XAU/USD quote. A user reading a
  stop off the card would place it at a price the market never had. The candles
  are now rescaled onto the live broker quote (`anchor_series_to_spot`) by one
  factor, so every absolute level sits on the broker's scale while every ratio
  (RSI, ATR%, Bollinger position) is unchanged — the factor cancels. The shift is
  reported in the `anchor` field and labelled on the card. Past `MAX_ANCHOR_PCT`
  (1.5%) the proxy is treated as diverged: it is **not** rescaled, and a bilingual
  warning says the levels are the proxy's, not spot's.
- **The footer credited a source that did not serve the candles.** It read "via
  Yahoo Finance" on every instrument, but spot gold comes from Kraken. The
  response now carries `instrument.provider` and the footer names it, so the
  credit matches the data on every symbol.

### Security

- **The rate limit could be walked around.** `X-Forwarded-For` was read from the
  *first* hop, which is the value the caller sends; a script could put a new
  address there on every request and never be counted. It now reads the **last**
  hop, the one our own edge appended, which the caller cannot forge.
- **The rate-limit map is now pruned** (expired buckets swept once a minute).
  It kept one entry per distinct caller forever, so invented identities would
  have grown it without bound.
- **`Strict-Transport-Security` and `Permissions-Policy`** are now sent
  alongside the existing CSP, nosniff, frame and referrer headers.
- **XSS hardening.** Every server-supplied field that reaches the page is now
  escaped through `esc()` in `web/app.js`, and the AI narrative is additionally
  stripped of markup server-side (`_strip_markup` in `app/ai.py`). The inline
  `<script>` block and all inline `onclick` handlers were moved into the
  external `app.js`, which lets the Content-Security-Policy forbid inline
  script outright.
- **Security headers.** `Content-Security-Policy` (script-src 'self'),
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and
  `Referrer-Policy: no-referrer` are set on every response.
- **Abuse guard.** The analysis endpoints are rate-limited per client IP
  (`RATE_LIMIT_PER_MIN`, default 60) and can require a key (`DESK_API_KEY`,
  optional) so an open door cannot quietly spend the owner's model quota.
- **Container runs unprivileged.** The image creates and switches to user
  `desk` (uid 10001) instead of running as root, and carries a `HEALTHCHECK`.

### Fixed

- **Spot gold was never cached.** The cache sat below the spot branch, so
  XAUUSD — the instrument this desk exists for — was the one series that paid
  for a fresh Kraken (then Coinbase) call on every single read. The cache now
  wraps both venues.
- **The card misreported the model on the fallback path.** When every model
  failed, the narrative said `source=deterministic` but still named the primary
  model, which reads as "this model wrote it". It now reports no model, because
  none wrote the text.
- **`r.statusCode` on the client is undefined** (`Response.status` is the real
  property); the error path now prints the status it actually got.
- **The card's precision disagreed with the API.** `fmt()` in `web/app.js`
  rounded 57.2346 to 57.235 while the server said 57.2346; it now uses the same
  magnitude rule as `indicators.price_decimals`.
- **`drawChart` assumed a good payload** and threw on an error body; it now
  checks the response and the shape before drawing.
- **An undefined CSS variable** (`--fg`, the real token is `--txt`) left the
  provenance figures unstyled.
- **A zero or negative price no longer crashes the signal engine.** The
  level-room rule divided by price before the `fatal` check ran; a broken read
  now degrades to NEUTRAL with a warning instead of raising.
- **The market cache is bounded** (256 entries, oldest evicted). It was
  unbounded, so walking distinct symbols grew it without limit.
- **`urlopen` call sites in `scripts/` verify the URL scheme** rather than
  trusting an operator-supplied base to be http(s).

### Added

- **CI** (`.github/workflows/ci.yml`): the test suite on Python 3.11 and 3.12,
  plus ruff, mypy, bandit, and a dependency audit. Network tests are marked
  `@pytest.mark.network` and deselected so a red build means we broke
  something, not that a feed was slow.
- **`LICENSE`** (MIT), `CONTRIBUTING.md`, `AGENTS.md`, and this changelog.
- **`pyproject.toml`** — the runtime dependencies, the lint rules, the test
  defaults, the coverage floor and the type-check settings live in one file, so
  `ruff.toml` and `pytest.ini` are gone. `requirements.txt` is kept in step and
  a test asserts the two cannot drift apart.
- **`web/lib.js`** — the browser helpers (`esc`, `fmt`, `priceDecimals`,
  `basisPct`) as a module the page and node's test runner share, so the escaping
  and precision rules have one copy instead of two.
- **`ARCHITECTURE.md`** — the layers, the data flow for one request, the
  deployment topology, and the honest limits, written down where a reader meets
  them.
- **The chart has axes.** Price labels on the right and dates along the bottom;
  a chart without them is decoration, not a chart.
- **The card is usable without sight, a mouse, or a wide screen.** The canvas
  carries a text description, the selects are labelled, the status line is a
  live region, focus is visible, reduced-motion is honoured, and the controls
  stack under 560px.
- **A `ui` CI job** runs the browser helpers under `node --test`.

### Engineering

- **`requirements.lock`** — the resolved, hash-pinned dependency set, and the
  thing the image now installs (`--require-hashes`). Before this, `>=` ranges
  meant two builds of the same commit could install different bytes, and the
  image that passed CI was not provably the image that shipped. It is compiled
  for the **lowest** supported Python (3.11), not the image's 3.12: compiling
  for 3.12 pinned `numpy 2.5.4` (>=3.12 only) and the 3.11 CI job died on
  `pip-audit -r requirements.lock` while the image built fine — a lock must
  install everywhere the project claims to run.
- **`scripts/check_lock.py`** — fails when the lock pins a version the declared
  range no longer allows, when a direct dependency is missing from it, or when
  any entry is unpinned or unhashed. Deterministic on purpose: a re-resolve-and-
  diff check would go red on every routine upstream release and be ignored.
- **A `docker` CI job** builds the image and waits for `/api/health`, then
  asserts the container is not root. "Green in CI" and "the image builds" were
  two facts; now the build and the healthcheck are proven before a push.
- **`.github/dependabot.yml`** — weekly updates for pip, GitHub Actions, and the
  Docker base image. The lock check turns a pip bump into a red build until the
  lock is regenerated, so the two cannot drift apart quietly.

### Monitoring

- **`/api/status`** — a diagnostics endpoint beside `/api/health`. Health says
  "alive"; this says how long the process has run, how many requests it has
  served, the revision actually deployed (`RENDER_GIT_COMMIT`), whether the
  model path is live, the price-cache size, and the distinct clients in the rate
  window. Every field is process-local and resets on restart, and `uptime_s`
  makes that visible rather than letting a small number read as "idle".
- **The watcher no longer relies on the schedule alone.** `deploy-watch.yml`
  gained a `push` trigger. GitHub's `schedule` is best-effort and, in practice,
  throttled: this workflow was added with a `*/30` cron and produced **zero
  scheduled runs**, while a sibling repo's `*/5` cron fired only every 4–6 hours.
  A watchdog that never runs is silent in exactly the way a healthy service is,
  so the reliable event (push) now drives it and the cron stays as a backstop.
- **The watcher watches itself.** `check_deploy.py --gap-threshold` fails when
  the log's newest reading is older than the threshold (12h in CI): a stopped
  schedule is reported, not mistaken for an all-clear. An empty log — a fresh
  install — is deliberately *not* an alarm.
- **A push check proves the deploy landed.** On push, `check_deploy.py
  --expect-commit <sha>` polls `/api/status` until the running revision equals
  the pushed one, or reports what it saw instead. A green run that only says
  "something is serving" would be the same false all-clear this desk refuses.

### Error tracking

- **Unhandled exceptions are no longer silent.** Before this, an unexpected
  exception produced an empty 500 and left no trace anywhere — no log, no
  counter. `app/errors.py` now writes one structured JSON line per error to
  stdout (which Render captures — that is the durable log) and keeps an
  aggregate count by type. The full traceback stays in the log; the caller gets
  a generic 500 with an `X-Request-Id`, never the traceback, path, or message.
- **Secrets are redacted and the record is bounded.** Any configured secret
  (`GROQ_API_KEY`, `DESK_API_KEY`, `TELEGRAM_BOT_TOKEN`, …) that appears in the
  message or traceback becomes `***`; the message and traceback are truncated,
  and the distinct exception types are capped so a crash loop cannot grow the
  map without bound.
- **`/api/status` reports the count by type**, aggregate only — no messages, no
  paths, no frames. Every field is process-local; `uptime_s` beside it is what
  tells a zero from "only just started".
- **An upstream outage is not counted as our error.** A market source being down
  is a 502 with the reason, and is excluded from the count on purpose — a source
  outage must not read as the service being broken, or the count becomes noise
  and gets ignored.
- **The watcher fails a deploy that is throwing.** On push, `check_deploy.py
  --max-errors 0` reads that count: a deploy that answers `/api/health` while
  raising on real requests is exactly the failure a liveness probe cannot see.
- **No third-party tracker, by choice.** Sending stack traces and request
  context to an external account is not a decision this project makes on the
  owner's behalf, so there is no Sentry (or equivalent) dependency. The trade is
  stated in ARCHITECTURE: no cross-process aggregation, no error-rate alerting,
  no tracing, and no tracking of a request that is served but returns a wrong
  number — only of one that raises.

### The dead analyze button

- **The Analyze button did nothing, and said nothing.** `web/app.js` is a
  module, so it runs in strict mode, and it referred to its elements by bare
  name (`status`, `go`, `sym`, …) relying on window's named access. `status` is
  a reserved window property — the status-bar string — so a bare `status` was a
  *string*, and `status.textContent = t.load` threw a `TypeError` **before** the
  `try` block: the panel stayed empty, the button stayed disabled, and no error
  appeared anywhere. `sym` and `iv` did not resolve at all. It shipped because
  the UI tests only exercised the pure helpers in `lib.js` and never touched the
  DOM.
- **Fix:** every element is now bound explicitly with `$("id")` and a comment
  says why the shortcut is unsafe here.
- **A test that would have caught it:** `web/dom.test.mjs` runs `app.js` for
  real in a `node:vm` sandbox with a minimal DOM — no browser — and asserts that
  a click renders a card and re-enables the button. Reverting the fix fails all
  three cases; the fix passes them. It also asserts that every id `app.js` binds
  exists in `index.html`, so a rename cannot quietly break the wiring again.

### Tests

- Coverage 67% → **87%** → **95%**; the suite grew from 77 to **128** to
  **178 tests** (plus 11 UI tests under node, 8 helpers + 3 DOM). New this pass:
  every market venue
  and every failure path (`market.py` 49% → 99%), the browser helpers, the
  accessibility and mobile rules, two tests that pin the single-worker
  rate-limit scope, the API layer end to end (`main.py` 74% → 99%), the model
  fallback chain model by model (`ai.py` 77% → 89%), and the monitoring and
  error-tracking surface — `/api/status`, the lapsed-watchdog detector, the
  deploy-landed check, the unhandled-error record with its no-leak and
  secret-redaction rules, and the rule that an upstream outage is not counted as
  our error.
