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

It also watches itself: if the log's last reading is older than
--gap-threshold hours (default 3), the watchdog reports that it stopped
firing. A probe that never runs is silent in exactly the way a healthy
service is, and the two must not be confused.

Exit status is 0 when the state is known and nothing is wrong (awake and healthy,
or asleep -- both are normal) and 1 when the state is unknown, the app is awake
but unhealthy, or the watchdog has gone quiet. A failed read is never reported
as an all-clear.
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


def watch_gap(readings: list[dict], now: int, max_gap_s: int) -> tuple[bool, str]:
    """Report whether the watchdog itself has gone quiet.

    A probe that never runs produces exactly the same thing as a probe that ran
    and found nothing: silence. The two must not be confused, so the watchdog
    checks the age of its own last reading. An empty log is *not* an alarm -- a
    fresh install has no history yet and the first run writes the first line --
    but a log whose last entry is older than max_gap_s means the schedule
    stopped, and that is reported, never swallowed.
    """
    if not readings:
        return True, "no prior readings (first run; nothing to compare)"
    last = max(int(r.get("ts", 0)) for r in readings)
    age = now - last
    if age > max_gap_s:
        hours = age / 3600
        return False, (f"the watchdog last ran {hours:.1f}h ago "
                       f"(threshold {max_gap_s / 3600:.1f}h) — the schedule is not firing")
    return True, f"last reading {age // 60}m ago"


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


def _read_log(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        return []
    return rows


def deployed_commit(base: str) -> str | None:
    """Read the revision the running service reports, or None if unavailable."""
    root = base.rstrip("/")
    status, body = fetch(root + "/api/status")
    if status != 200:
        return None
    try:
        return (json.loads(body).get("commit") or "") or None
    except ValueError:
        return None


def await_commit(base: str, expected: str, *, timeout: float,
                 interval: float = 10.0) -> tuple[bool, str]:
    """Wait for the deployed revision to become `expected`.

    A push-triggered check must prove the revision that was just pushed is the
    one actually running; otherwise a green run says only "some revision is
    serving", which is the false all-clear this whole desk refuses to emit. A
    Render deploy lags the push by a minute or two, so a single immediate read
    would cry wolf. Poll until it matches or the budget runs out, then report
    what was actually seen -- never a bare pass.
    """
    expected = expected[:12]
    deadline = time.time() + timeout
    last = None
    while True:
        last = deployed_commit(base)
        if last == expected:
            return True, f"the running revision is the one pushed ({expected})"
        if time.time() >= deadline:
            break
        time.sleep(min(interval, max(0.0, deadline - time.time())))
    if last is None:
        return False, (f"could not read the running revision within "
                       f"{timeout:.0f}s (expected {expected}); /api/status did not answer")
    return False, (f"the running revision is {last}, but {expected} was pushed "
                   f"(the deploy has not landed)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--log", metavar="FILE")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--expect-commit", metavar="SHA",
                    help="require the running revision to be this commit "
                         "(used on push to prove the deploy landed)")
    ap.add_argument("--wait-seconds", type=float, default=240.0, metavar="S",
                    help="how long to wait for --expect-commit to appear (default 240)")
    ap.add_argument("--gap-threshold", type=float, default=3.0, metavar="HOURS",
                    help="alert when the watchdog's own last reading is older than "
                         "this many hours (default 3; the schedule targets 0.5)")
    args = ap.parse_args(argv)

    # Read the existing log *before* appending, so the gap reflects the last
    # run, not this one.
    prior = _read_log(args.log) if args.log else []

    report = probe(args.base)
    ok, reason = verdict(report)

    # Prove the pushed revision is live, when asked. This is the only path that
    # reads /api/status (which wakes a sleeping service); the scheduled run never
    # passes --expect-commit, so its zero-wake property is preserved. On push a
    # deploy is in flight anyway, so waking is expected -- and if the revision
    # cannot be confirmed, that is reported, not passed over.
    if args.expect_commit:
        cok, creason = await_commit(args.base, args.expect_commit, timeout=args.wait_seconds)
        report["commit"] = deployed_commit(args.base)
        if not cok:
            ok = False
            reason = f"{reason}; {creason}"
        else:
            reason = f"{reason}; {creason}"

    # The watchdog must also watch itself: a schedule that stopped firing is
    # silence, and silence must not pass for an all-clear.
    gap_ok, gap_reason = watch_gap(prior, report["ts"], int(args.gap_threshold * 3600))
    if not gap_ok:
        ok = False
        reason = f"{reason}; {gap_reason}"
    report["gap"] = gap_reason

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
        print(f"watch  : {gap_reason}")
        if report["state"] == "unknown":
            print("         (the request never completed; this is not an all-clear)")

    if args.notify and not ok:
        sent = notify(f"AnalysisDesk: {reason}")
        if not args.json:
            print(f"telegram: {'sent' if sent else 'not sent (no token/chat set)'}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
