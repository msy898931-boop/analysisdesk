## What changed

<!-- One or two sentences. What does this do that the code did not do before? -->

## Why

<!-- The problem this solves. Link an issue if there is one. -->

## Honesty check

<!-- The project's rule is "never report an unknown as a known". Confirm: -->

- [ ] No failed read is rendered as an all-clear.
- [ ] Every number this touches still carries its provenance (spot vs futures,
      interval, source).
- [ ] No confidence or price is invented where the real value is unavailable.
- [ ] Any new server-supplied string reaching the browser goes through `esc()`.

## Tests

- [ ] `python -m pytest tests/ -m "not network"` passes.
- [ ] New tests cover the change, and any test hitting a live API is marked
      `@pytest.mark.network`.

## Checks

- [ ] `ruff check app/ tests/ scripts/`
- [ ] `mypy app/ --ignore-missing-imports`
- [ ] `bandit -q -r app/ scripts/ -ll`

## Notes for the reviewer

<!-- Anything non-obvious: a trade-off made, a limitation left in place. -->
