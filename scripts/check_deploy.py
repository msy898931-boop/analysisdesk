#!/usr/bin/env python3
"""Report whether the deployed service is awake, asleep, or unreachable.

Render spins a Free web service down after 15 minutes without inbound traffic.
While a service is spun down, a request to /robots.txt receives Render's
automatic "disallow all" reply and does NOT reach the app or wake it; every
other path, /api/health included, triggers a spin-up. /robots.txt is therefore
the one probe that can read the sleep state without changing it -- a check that
is also an intervention cannot observe.

    python3 scripts/check_deploy.py                 # human report
    python3 scripts/check_deploy.py --json          # machine report
    python3 scripts/check_deploy.py --log f.jsonl   # append the result
    python3 scripts/check_deploy.py --notify        # Telegram on a real problem

Exit status is 0 when the state is known and nothing is wrong (awake and healthy,
or asleep -- both are normal) and 1 when the state is unknown or the app is awake
but unhealthy. A failed read is never reported as an all-clear.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://analysisdesk.onrender.com"
TIMEOUT = 30
UA = "analysisdesk-watchdog/1"


def classify(status: int | None, body: str) -> str:
    """Turn one /robots.txt response into 'awake', 'asleep', or 'unknown'.

    An awake service has no /robots.txt route, so FastAPI answers 404 with JSON.
    A spun-down service gets Render's plain-text disallow-all reply. Anything
    else is genuinely unknown: never guess awake.
    """
    if status is None:
        return "unknown"
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and "detail" in payload:
        return "awake"
    if status == 200 and "Disallow: /" in body and "User-agent" in body:
        return "asleep"
    return "unknown"


def fetch(url: str) -> tuple[int | None, str]:
    # Only ever speak http(s). The base URL is operator-supplied, and urlopen
    # would happily open file:// or a custom scheme if one were passed in.
    if not url.lower().startswith(("http://", "https://")):
        return None, "refused non-http scheme"
    req = urllib.request.Request(url, headers={"User-Agent": UA})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310  # nosec B310
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, ""


def probe(base: str) -> dict:
    root = base.rstrip("/")
    status, body = fetch(root + "/robots.txt")
    state = classify(status, body)

    health = None
    healthy = None
    if state == "awake":
        hstatus, hbody = fetch(root + "/api/health")
        if hstatus == 200:
            try:
                health = json.loads(hbody)
            except ValueError:
                health = None
        healthy = bool(health) and health.get("status") == "ok"

    return {"state": state, "probe_status": status, "healthy": healthy,
            "health": health, "ts": int(time.time())}


def verdict(report: dict) -> tuple[bool, str]:
    """Return (ok, one-line reason). Silence is never rendered as an all-clear."""
    state = report["state"]
    if state == "asleep":
        return True, "asleep (spun down by Render; normal on the free plan)"
    if state == "awake":
        if report["healthy"]:
            return True, "awake and healthy"
        return False, "awake but the health check did not report ok"
    return False, "could not determine the state (the request never completed)"


def notify(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    payload = json.dumps({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"})
    try:
        # The scheme is a literal https URL; nothing caller-supplied reaches it.
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310  # nosec B310
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--log", metavar="FILE")
    ap.add_argument("--notify", action="store_true")
    args = ap.parse_args(argv)

    report = probe(args.base)
    ok, reason = verdict(report)

    if args.log:
        with open(args.log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(report) + "\n")

    if args.json:
        print(json.dumps({**report, "ok": ok, "reason": reason}))
    else:
        mark = {"awake": "AWAKE", "asleep": "ASLEEP", "unknown": "UNKNOWN"}[report["state"]]
        print(f"state  : {mark} — {reason}")
        if report["health"]:
            print(f"health : {report['health']}")
        if report["state"] == "unknown":
            print("         (the request never completed; this is not an all-clear)")

    if args.notify and not ok:
        sent = notify(f"AnalysisDesk: {reason}")
        if not args.json:
            print(f"telegram: {'sent' if sent else 'not sent (no token/chat set)'}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
