# AGENTS.md — notes for an automated agent working in this repo

Read this before changing anything. It records what is easy to get wrong here,
not what is obvious from the tree.

## What this project is

AnalysisDesk is an honest technical-analysis service: real market data in,
computed indicators and a transparent rule-by-rule signal out, and an AI layer
that only *explains* the numbers it is given. It never predicts and never
invents a value. Keep it that way.

## Commands that matter

```bash
python -m pytest tests/ -m "not network"   # what CI runs; must always be green
python -m pytest tests/                    # adds live-API tests (need network)
ruff check app/ tests/ scripts/
mypy app/ --ignore-missing-imports
bandit -q -r app/ scripts/ -ll
pip-audit -r requirements.lock
python -m uvicorn app.main:app --port 12000
```

## The invariants (break one and you have broken the point)

1. **An unknown is never rendered as a known.** A failed read says so. No
   silent all-clear, no reassuring default. This applies to the UI, the API,
   and the scripts.
2. **Every number carries its provenance.** Say whether a figure is spot or
   futures, which interval, which source. A bare number is a bug.
3. **No invented confidence.** If the engine cannot decide, the answer is
   NEUTRAL with confidence 0 and a warning — never a guess.
4. **Fail closed, not open.** A gate that cannot read its input denies, and
   says why, rather than allowing.
5. **Escape at the boundary.** Server strings reach the browser through
   `esc()` in `web/app.js`. The CSP header is defence in depth, not the fix.

## Traps found the hard way

- **`l` is the low of an OHLC candle.** E741 is deliberately ignored in
  `pyproject.toml` (`[tool.ruff]`); do not "fix" it by renaming the variable.
- **`scripts/check_deploy.py` probes `/robots.txt` on purpose.** That is the
  one path Render answers while a Free instance is spun down *without waking
  it*. A check that also causes traffic cannot observe the sleep state. Do not
  "simplify" it to `/api/health`.
- **`urlopen` needs both `# noqa: S310` (ruff) and `# nosec B310` (bandit)** —
  the two tools use different marker syntax, and both run in CI.
- **Network tests are marked `@pytest.mark.network`** and deselected in CI. A
  new test that calls Yahoo/Kraken/Coinbase/Swissquote must carry the marker,
  or CI becomes flaky for reasons that are not ours.
- **The rate limiter is per-process and in-memory.** A restart clears it and a
  second worker would not share it. This is stated in the code; do not describe
  it as more than it is.
- **`web/app.js` is external for a reason.** Inline `<script>` and inline
  `onclick` are what CSP `script-src 'self'` forbids. Keep handlers bound in
  the JS file.
- **`ruff check` is what CI enforces, not `ruff format --check`.** Several files
  on `main` are not format-clean by choice; running the formatter rewraps ~1000
  lines and buries the real diff. Use `ruff check --fix` only.
- **CodeQL flags `assert` on a call with a side effect** (`py/side-effect-in-assert`):
  under `python -O` the assert is stripped and the call never runs. Bind the
  response first (`r = c.post(...)`), then `assert r.status_code == ...`. It
  flags POST/DELETE, not GET. The code-scanning alerts API is 403 for this
  token; read the finding from the check-run annotations instead:
  `gh api repos/.../commits/<sha>/check-runs` then `.../check-runs/<id>/annotations`.
- **The `CodeQL` check-run is not the `codeql` job.** The job builds the
  database; the check-run is the default setup's verdict. Read the check-run.
- **biquote's `last` and `volume` are always 0.** Broker CFD pricing has no
  consolidated tape, so those two fields carry nothing, on every symbol,
  forever. The spot price is `mid` (or `(bid+ask)/2`). Never compute from
  `last`/`volume`, and never present a biquote price without its `stale` /
  `marketState` — the feed says a closed market is closed, and dropping that is
  the exact "unknown as known" failure this project exists to refuse.
- **biquote forbids redistributing the raw feed via a public API.** Showing one
  `spot` quote for display is the permitted "inside your product" case; a
  pass-through OHLC endpoint is not. See `docs/SOURCES.md` before extending its
  use (e.g. charting from its OHLC).
- **An in-process timer sleeps with the host.** Render Free suspends a service
  after ~15 minutes with no *inbound* request; an outbound long poll (Telegram's
  `getUpdates`) is **not** inbound traffic, so `SCHEDULER_ENABLED` does not keep
  the service awake and its clock freezes with it. This is why `scheduler.py`
  records a heartbeat and why `state()["note"]` calls itself best-effort. Do not
  describe it as a 24/7 schedule; the supported way is a Render Cron Job on
  `scripts/run_alerts.py`.
- **A background task must never take the web process down.** Both
  `bot.poll_forever` and `scheduler.run_forever` catch their own exceptions and
  continue; they are cancelled on shutdown via the app lifespan. A new task
  added there must follow the same rule, and must be gated on its own env var —
  the default deploy starts no background work at all.
- **A bot allowlist is not the token.** `TELEGRAM_BOT_TOKEN` is a credential,
  not a permission; the set of chats the bot will answer is
  `TELEGRAM_ALLOWED_CHATS` (falling back to `TELEGRAM_CHAT_ID`), and empty means
  it answers nobody. Fail closed: never treat "a token is set" as "anyone may
  command it".

## Environment variables

| var | default | what it does |
|---|---|---|
| `GROQ_API_KEY` | — | the AI narrator's key; no key ⇒ computed sentence, never a fake one |
| `ANALYSIS_MODELS` | built-in list | comma-separated model chain, tried in order |
| `RATE_LIMIT_PER_MIN` | `60` | per-identity request cap; per-process, in-memory |
| `DESK_API_KEY` | unset | when set, **every** caller must send `x-api-key` — no page exemption |
| `DESK_TRUST_PROXY` | unset | **off by default**: trust the last `X-Forwarded-For` hop only when a proxy we control appends it. Render's blueprint sets it. Requires `--no-proxy-headers` so uvicorn cannot rewrite the peer from the same header |
| `ALERTS_TOKEN` | unset | credential for `/api/alerts*` (`x-alerts-token`). **Unset ⇒ 503 (closed), not open.** A chat id is an identifier, not a secret |
| `ALERTS_ENABLED` | unset | turns on Telegram *delivery* of fired alerts |
| `BOT_ENABLED` | unset | turns on the Telegram command receiver (`getUpdates` long poll). Needs `TELEGRAM_BOT_TOKEN` **and** an allowlist, else it stays shut |
| `TELEGRAM_ALLOWED_CHATS` | unset | comma-separated chat ids the bot answers; falls back to `TELEGRAM_CHAT_ID`. **Empty ⇒ answers nobody (fail closed)** |
| `SCHEDULER_ENABLED` | unset | turns on the in-process clock calling `alerts.run_once()`. **Best-effort on a host that sleeps** — an outbound poll is not inbound traffic, so Render Free freezes the timer too |
| `SCHEDULER_PERIOD_S` | `300` | seconds between passes; clamped to ≥30 so a typo cannot spin a tight loop |
| `DESK_DATA_DIR` | `data/` | where the SQLite store lives; point it at a mounted disk to survive a redeploy |

**Two credentials, two jobs, deliberately separate.** `DESK_API_KEY` gates the
read API (left unset in production so the public page stays open);
`ALERTS_TOKEN` gates the service-to-service alerts API. Coupling them would
force a choice between closing the page and leaving alerts open. Both fail
closed. `/api/status` reports `api_key_required` and `alerts_configured` so a
client can tell "closed" from "wrong credential".

## Style

`pyproject.toml` (`[tool.ruff]`) is the source of truth (line 110, py311+, rules E/F/I/S/B/UP/SIM/PL).
Every ignore there is justified in a comment. Comments in code explain *why*;
if a line only restates the code, delete it.

## Commits

Conventional-ish prefixes (`fix:`, `feat:`, `test:`, `docs:`, `chore:`). Say
what changed and why in the body when the why is not obvious from the diff.
