"""Error tracking that keeps the log where it is: on stdout.

The desk had no error tracking at all -- an unexpected exception produced an
empty 500 and left no trace anywhere. A third-party tracker (Sentry and the
like) would fix the visibility, but it ships stack traces, request context, and
the operator's data to an external account, which is a decision this project
does not make on the owner's behalf. Render already captures stdout, so the
durable log is a structured line per error; the in-memory summary exists only
to answer "is the running process healthy" over /api/status.

Two rules this module exists to keep:

1. **An error of ours is not a market source being down.** A missing candle
   feed is expected and is reported to the caller as 502 with a reason; it is
   not a bug and must not be counted here. Only unhandled exceptions are.
2. **What is recorded is bounded and carries no secrets.** The message and the
   traceback are truncated, the distinct exception types are capped, and any
   configured secret that appears in the text is redacted before it is written.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback

# Distinct exception types kept in the summary. A crash loop can invent types
# without bound, so the tail is folded into one bucket rather than growing.
_MAX_TYPES = 20
_OTHER = "_other"
_MSG_MAX = 300
_TRACE_MAX = 4000

# Values that must never reach a log line. Read at call time, not import time,
# so a secret set later in the process is still redacted.
_SECRET_ENV = (
    "GROQ_API_KEY", "DESK_API_KEY", "TELEGRAM_BOT_TOKEN",
    "OPENHANDS_API_KEY", "GITHUB_TOKEN",
)

_log = logging.getLogger("analysisdesk.errors")
if not _log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)
    # Keep our structured line as the whole message; do not let the root
    # logger wrap it in a second format or route it elsewhere.
    _log.propagate = False

_started = time.time()
_state: dict = {"total": 0, "types": {}, "last": None}


def _redact(text: str) -> str:
    """Remove any configured secret value from text before it is written."""
    for name in _SECRET_ENV:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, "***")
    return text


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def record(exc: BaseException, *, where: str, request_id: str = "",
           path: str = "") -> str:
    """Log one unhandled exception and fold it into the in-memory summary.

    Returns the request id, so the caller can put it in the response and the
    reader can find this exact line in the log. The line is one JSON object;
    the summary is aggregate only (counts by type) so /api/status never leaks a
    message, a path, or a stack frame.
    """
    typename = type(exc).__name__
    message = _redact(_truncate(str(exc), _MSG_MAX))
    trace = _redact(_truncate("".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)), _TRACE_MAX))

    line = {
        "ts": int(time.time()),
        "level": "error",
        "where": where,
        "type": typename,
        "message": message,
        "request_id": request_id,
        "path": path,
        "trace": trace,
    }
    _log.error(json.dumps(line, ensure_ascii=False))

    _state["total"] += 1
    types = _state["types"]
    if typename in types:
        types[typename] += 1
    elif len(types) < _MAX_TYPES:
        types[typename] = 1
    else:
        types[_OTHER] = types.get(_OTHER, 0) + 1
    _state["last"] = {"type": typename, "where": where,
                      "request_id": request_id, "ts": line["ts"]}
    return request_id


def summary() -> dict:
    """Aggregate view for /api/status: counts and recency, never content.

    Process-local like every other counter here, so a restart resets it. The
    uptime beside it on /api/status is what tells a reader whether a zero means
    "clean" or "only just started".
    """
    return {
        "total": _state["total"],
        "types": dict(_state["types"]),
        "last": dict(_state["last"]) if _state["last"] else None,
        "since": int(_started),
    }


def reset() -> None:
    """Clear the summary. For tests and for a deliberate operator reset."""
    _state["total"] = 0
    _state["types"] = {}
    _state["last"] = None
