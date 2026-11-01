"""The clock the alerts need: run the crossing check every so often.

`alerts.run_once()` is a one-shot by design -- it is testable because it has no
loop and no clock. Something has to call it on a schedule. That something is
here, and it carries a hard truth about the host:

**On a host that sleeps, an in-process clock sleeps with it.** Render's free
plan suspends a web service after ~15 minutes with no *inbound* request. An
outbound poll (Telegram's long poll) is not inbound traffic, so the process can
be frozen while the bot waits on it. A timer inside this process therefore runs
*only while the service happens to be awake* -- it is not a guarantee that a
level is checked every N minutes, and this module refuses to pretend otherwise.

So the loop does two things a naive one does not:

  * It records a **heartbeat** after every pass. `/api/status` exposes it, so a
    clock that has stopped is visible as a gap rather than read as "all clear".
  * It reports its own honesty: `state()["note"]` says the schedule is
    best-effort on this host, in the words of the operator, not a promise.

For a real 24/7 schedule, the caller moves the clock off the web process: a
Render Cron Job, a GitHub Actions workflow, or any external ticker that hits
`scripts/run_alerts.py`. The logic is identical; only the thing that ticks
changes.
"""

from __future__ import annotations

import asyncio
import os
import time

from . import alerts, errors

# Default period. Long enough not to hammer four upstreams, short enough that a
# 1h bar's crossing is seen within the hour it forms.
_DEFAULT_PERIOD_S = 300
_MIN_PERIOD_S = 30

# One-element lists rather than globals: mutated without a `global` statement.
_last_run = [0.0]
_last_summary: dict = {}
_runs = [0]


def scheduler_enabled() -> bool:
    """Whether the in-process clock is on. Off unless explicitly set."""
    return os.getenv("SCHEDULER_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def period_s() -> int:
    """Seconds between passes. Clamped so a typo cannot spin a tight loop."""
    try:
        val = int(os.getenv("SCHEDULER_PERIOD_S", str(_DEFAULT_PERIOD_S)))
    except ValueError:
        return _DEFAULT_PERIOD_S
    return max(_MIN_PERIOD_S, val)


def state() -> dict:
    """Honest heartbeat for /api/status. Never claims a guarantee it lacks."""
    now = time.time()
    last = _last_run[0]
    return {
        "enabled": scheduler_enabled(),
        "period_s": period_s(),
        "runs": _runs[0],
        "last_run_s_ago": round(now - last, 1) if last else None,
        "last_summary": _last_summary or None,
        "note": (
            "in-process clock: runs only while the service is awake. On a host "
            "that sleeps on idle this is best-effort, not a 24/7 schedule -- "
            "point a cron job at scripts/run_alerts.py for that."
        ),
    }


async def run_forever() -> None:
    """Call `alerts.run_once()` every `period_s()`. Never raises out of the task.

    The first pass runs immediately, so a freshly started process does not sit
    idle for a full period before checking anything.
    """
    period = period_s()
    while True:
        try:
            summary = await alerts.run_once()
            _last_summary.clear()
            _last_summary.update(summary)
            _runs[0] += 1
            _last_run[0] = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the clock must outlive one bad pass
            errors.record(exc, where="scheduler.run")
        await asyncio.sleep(period)
