# Contributing to AnalysisDesk

Thanks for looking. This project has one rule that outranks the others: **never
report an unknown as a known**. A wrong answer stated confidently is worse than
"could not read". Most of the review comments here come back to that.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock
pip install pytest pytest-cov ruff mypy bandit pip-audit
```

Run the app:

```bash
python -m uvicorn app.main:app --reload --port 12000
```

## Before you open a pull request

Everything CI runs, run locally. They are quick.

```bash
python -m pytest tests/ -m "not network"   # the suite CI runs
python -m pytest tests/                    # the same plus live-API tests
ruff check app/ tests/ scripts/
mypy app/ --ignore-missing-imports
bandit -q -r app/ scripts/ -ll
pip-audit -r requirements.lock
```

## The two test classes

- **Offline** (the default) — deterministic, no third party involved. These run
  in CI and must stay green regardless of the network.
- **`@pytest.mark.network`** — reaches Yahoo, Kraken, Coinbase, or Swissquote.
  Useful while developing against real data; deselected in CI because a red
  build must mean *we* broke something, not that a feed was slow.

Add the marker to any test that hits a live endpoint.

## Code expectations

- **Fail loudly.** If a read fails, say so. Do not return a plausible default,
  do not swallow the exception, do not render a silent all-clear.
- **Keep the numbers honest.** A price, a confidence, or a stop is either real
  and labelled with where it came from, or it is absent. Never invent one.
- **Provenance travels with the value.** Spot XAU/USD and the COMEX future are
  different instruments; say which one a number belongs to.
- **Escape at the boundary.** Anything server-supplied that reaches the browser
  goes through `esc()` in `web/app.js`. The CSP is the second line, not the first.
- **Comment the why, not the what.** A comment earns its place by explaining a
  non-obvious decision, an invariant, or a deliberate trade-off.

## Dependencies

`requirements.txt` holds the ranges; `requirements.lock` is the resolved,
hash-pinned set the image installs. After changing a range, regenerate it:

```bash
uv pip compile requirements.txt --python-version 3.12 \
  --generate-hashes --no-emit-index-url --no-header -o requirements.lock
python scripts/check_lock.py        # must pass before you push
```

## Style

`ruff` decides formatting and most style (line length 110, py311+). The ignores
in `pyproject.toml` (`[tool.ruff]`) are each justified in a comment; if you need a new one, say why
next to it.

## Data sources

All market data is keyless and fetched at runtime — nothing is bundled and no
key is required to run the app. `GROQ_API_KEY` is optional: without it the
narrative falls back to the computed explanation, and the app still works.
