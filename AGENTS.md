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
pip-audit -r requirements.txt
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

## Style

`pyproject.toml` (`[tool.ruff]`) is the source of truth (line 110, py311+, rules E/F/I/S/B/UP/SIM/PL).
Every ignore there is justified in a comment. Comments in code explain *why*;
if a line only restates the code, delete it.

## Commits

Conventional-ish prefixes (`fix:`, `feat:`, `test:`, `docs:`, `chore:`). Say
what changed and why in the body when the why is not obvious from the diff.
