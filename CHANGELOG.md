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
- **`ruff.toml`** and **`pytest.ini`** — the lint rules and test defaults are
  now declared rather than implied.

### Tests

- Coverage 67% → 74%; the suite grew from 77 to 91 tests, including the abuse
  guard, the forwarded-IP fix, the degenerate-price cases, the spot cache, the
  fallback attribution, and the markup-stripping path.
