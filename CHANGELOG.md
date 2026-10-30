# Changelog

Notable changes, newest first. The project is pre-1.0, so the format is kept
plain: what changed, and why it mattered.

## Unreleased

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
