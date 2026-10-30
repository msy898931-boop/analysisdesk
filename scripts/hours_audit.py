#!/usr/bin/env python3
"""Estimate Free-instance-hour usage from the watchdog's own history.

Render grants 750 Free instance hours per workspace per calendar month, shared
by every Free service in it. A running service consumes them; a spun-down one
does not, which is why the sleep is a feature on this plan.

Render's public API does NOT expose instance-hour consumption -- there is no
usage, billing, or quota endpoint. The authoritative number lives only in the
dashboard. This script therefore does not pretend to read it: it measures the
one thing it can, how often the service was awake in the watchdog's log, and
turns that into a monthly projection with the assumption stated.

    python3 scripts/hours_audit.py --log deploy-watch.jsonl
    python3 scripts/hours_audit.py --log deploy-watch.jsonl --services 2
    python3 scripts/hours_audit.py --log deploy-watch.jsonl --json

--services is the number of Free services you run in the workspace. The
projection assumes the others run continuously, which is the safe direction to
be wrong in: it over-counts rather than reassuring you.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

FREE_HOURS = 750
HOURS_PER_MONTH = 24 * 30.44


def load(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def audit(rows: list[dict], services: int) -> dict:
    counts = Counter(r.get("state", "unknown") for r in rows)
    known = counts["awake"] + counts["asleep"]
    if known == 0:
        return {"readings": len(rows), "known": 0, "awake_share": None,
                "counts": dict(counts), "projected": None, "headroom": None,
                "verdict": "no usable readings — run check_deploy.py --log first"}

    awake_share = counts["awake"] / known
    # This service alone, if its awake share held for a whole month.
    self_hours = awake_share * HOURS_PER_MONTH
    # The others are assumed to run continuously -- the conservative direction.
    others = max(services - 1, 0) * HOURS_PER_MONTH
    projected = self_hours + others
    headroom = FREE_HOURS - projected

    if headroom < 0:
        verdict = (f"OVER the {FREE_HOURS} h allowance by ~{abs(headroom):.0f} h — "
                   "free services get suspended until the month rolls over")
    elif headroom < 100:
        verdict = f"tight: only ~{headroom:.0f} h of headroom left"
    else:
        verdict = f"safe: ~{headroom:.0f} h of headroom"

    return {"readings": len(rows), "known": known,
            "awake_share": round(awake_share, 3), "counts": dict(counts),
            "self_hours": round(self_hours), "others_hours": round(others),
            "projected": round(projected), "headroom": round(headroom),
            "services": services, "verdict": verdict}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", required=True)
    ap.add_argument("--services", type=int, default=1,
                    help="Free services in the workspace (default 1)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report = audit(load(args.log), args.services)

    if args.json:
        print(json.dumps(report))
        return 0 if (report["headroom"] is None or report["headroom"] >= 0) else 1

    print(f"readings   : {report['readings']}  (awake/asleep/unknown: "
          f"{report['counts'].get('awake',0)}/{report['counts'].get('asleep',0)}/"
          f"{report['counts'].get('unknown',0)})")
    if report["awake_share"] is None:
        print(f"verdict    : {report['verdict']}")
        return 1
    print(f"awake share: {report['awake_share']:.0%} of the readings that had a state")
    print(f"this app   : ~{report['self_hours']} h/month at that rate")
    if report["others_hours"]:
        print(f"others     : ~{report['others_hours']} h/month "
              f"({report['services'] - 1} assumed always on)")
    print(f"projected  : ~{report['projected']} h of {FREE_HOURS} h")
    print(f"verdict    : {report['verdict']}")
    print()
    print("Note: Render's API exposes no instance-hour figure. This is a projection")
    print("from the watchdog's own samples. The authoritative number is in the")
    print("Render dashboard: Settings -> Billing -> Free instance hours.")
    return 0 if report["headroom"] >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
