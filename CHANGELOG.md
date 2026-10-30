# Changelog

Notable changes, newest first. The project is pre-1.0, so the format is kept
plain: what changed, and why it mattered.

## Unreleased

### Security

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

- Coverage 67% → 71%; the suite grew from 77 to 84 tests, including the abuse
  guard, the degenerate-price cases, and the markup-stripping path.
