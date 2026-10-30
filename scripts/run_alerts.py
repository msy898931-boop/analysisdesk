#!/usr/bin/env python3
"""Run one pass of the alert engine. Meant to be called on a schedule.

Deliberately a one-shot, not a daemon: a cron/Render job invokes it, it checks
every armed subscription against the latest close, fires what crossed, and
exits. That keeps the logic testable (no loop, no clock) and lets the host own
scheduling. Delivery to Telegram happens only when ALERTS_ENABLED is set.

Exit codes:
  0  pass completed (even if nothing fired)
  1  the pass raised -- a real failure, reported to the caller
"""

from __future__ import annotations

import asyncio
import json
import sys

from app import alerts


def main() -> int:
    try:
        summary = asyncio.run(alerts.run_once())
    except Exception as exc:  # noqa: BLE001 - a scheduler needs a nonzero exit
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
