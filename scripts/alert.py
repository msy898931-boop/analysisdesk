#!/usr/bin/env python3
"""Raise or clear the deploy-watch alert across the configured channels.

Kept apart from the workflow so the alerting rule can be tested without GitHub.
One open issue labelled `deploy-watch` is reused rather than opening a new one
each run; it is closed automatically when the service recovers.

    python3 scripts/alert.py open  --state awake --reason "..."
    python3 scripts/alert.py close

Channels: a GitHub issue (via gh) and, if the token and chat id are present,
Telegram. Both are optional and both failures are reported, never swallowed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

LABEL = "deploy-watch"
TITLE = "deploy-watch: the deployed service needs attention"


def gh(*args: str) -> tuple[int, str]:
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    return p.returncode, (p.stdout or p.stderr).strip()


def open_issue(state: str, reason: str) -> str:
    body = (
        f"State: **{state}**\n"
        f"Reason: {reason}\n\n"
        f"Checked: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f"Run: {os.environ.get('GITHUB_SERVER_URL','')}/{os.environ.get('GITHUB_REPOSITORY','')}"
        f"/actions/runs/{os.environ.get('GITHUB_RUN_ID','')}\n\n"
        "_A sleeping service is normal on the free plan and is not reported here. "
        "This alert fires only when the state could not be determined, or the app is "
        "awake but unhealthy._\n"
    )
    code, out = gh("issue", "list", "--label", LABEL, "--state", "open",
                   "--json", "number", "--jq", ".[0].number")
    if code == 0 and out:
        c2, o2 = gh("issue", "comment", out, "--body", body)
        return f"commented on #{out}" if c2 == 0 else f"comment failed: {o2}"
    gh("label", "create", LABEL, "--color", "B60205",
       "--description", "Automated deploy health alert")
    c3, o3 = gh("issue", "create", "--title", TITLE, "--label", LABEL, "--body", body)
    return f"opened {o3}" if c3 == 0 else f"create failed: {o3}"


def close_issue() -> str:
    code, out = gh("issue", "list", "--label", LABEL, "--state", "open",
                   "--json", "number", "--jq", ".[0].number")
    if code != 0 or not out:
        return "nothing open"
    gh("issue", "comment", out,
       "--body", f"Recovered: the service is reachable again as of "
                 f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}.")
    c2, o2 = gh("issue", "close", out)
    return f"closed #{out}" if c2 == 0 else f"close failed: {o2}"


def telegram(text: str) -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return "skipped (no token/chat)"
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": text}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        # The scheme is a literal https URL; nothing caller-supplied reaches it.
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310  # nosec B310
            return "sent" if r.status == 200 else f"http {r.status}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"failed: {type(e).__name__}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["open", "close"])
    ap.add_argument("--state", default="unknown")
    ap.add_argument("--reason", default="")
    args = ap.parse_args(argv)

    if args.action == "open":
        print("issue   :", open_issue(args.state, args.reason))
        print("telegram:", telegram(f"AnalysisDesk deploy-watch: {args.state} — {args.reason}"))
    else:
        print("issue   :", close_issue())
    return 0


if __name__ == "__main__":
    sys.exit(main())
