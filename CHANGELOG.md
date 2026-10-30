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

### Tests

- Coverage 67% → **87%**; the suite grew from 77 to **128 tests** (plus 8 UI
  tests under node). New this pass: every market venue and every failure path
  (`market.py` 49% → 99%), the browser helpers, the accessibility and mobile
  rules, and two tests that pin the single-worker rate-limit scope so raising it
  is a deliberate act.
