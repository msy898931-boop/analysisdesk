"""Durable state: alert subscriptions and a small visit counter, in SQLite.

Until now the desk kept nothing. Every restart reset the rate counter, the
subscription list, and any record that a level was ever armed. That is the same
"an unknown reported as a known" shape the whole project is built against: a
restart silently erased state the user believed was being kept.

What this stores, and what it does NOT:

  * **Stores:** alert subscriptions (a user asking to be told when a symbol
    crosses a price) and a monotonic visit counter. Both must survive a process
    restart to be worth anything.
  * **Does NOT store:** candles, indicator output, or prose. Those are derived
    and re-fetched; caching them here would be a second source of truth.

Honesty about the host:

  * On Render's free plan the filesystem is **ephemeral** -- the database
    survives a process restart but **not** a redeploy or an instance move. The
    store reports `durable: false` in that case rather than implying permanence.
    Set `DESK_DATA_DIR` to a mounted disk (or a paid plan with a disk) for real
    durability; the status surface says which one you got.

The database is created lazily and a read-only or missing directory is reported,
never raised through the request path -- a desk that cannot write its notes
should still serve a read.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

# Default sits beside the code inside the container (/app is owned by the
# unprivileged user). An explicit DESK_DATA_DIR wins, which is how a mounted
# disk is wired in production.
DEFAULT_DIR = "data"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT    NOT NULL,
    symbol     TEXT    NOT NULL,
    interval   TEXT    NOT NULL,
    direction  TEXT    NOT NULL,
    price      REAL    NOT NULL,
    created_at INTEGER NOT NULL,
    armed      INTEGER NOT NULL DEFAULT 1,
    UNIQUE(chat_id, symbol, interval, direction, price)
);
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def data_dir() -> Path:
    return Path(os.getenv("DESK_DATA_DIR", DEFAULT_DIR)).expanduser()


def db_path() -> Path:
    return data_dir() / "desk.sqlite3"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def available() -> bool:
    """True when the store can be opened and written. Never raises."""
    try:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.execute("INSERT INTO counters(name, value) VALUES('probe', 0) ON CONFLICT(name) DO NOTHING")
            conn.commit()
        return True
    except (sqlite3.Error, OSError):
        return False


def init() -> bool:
    """Create the schema. Returns whether the store is usable."""
    try:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()
        return True
    except (sqlite3.Error, OSError):
        return False


def bump(name: str, by: int = 1) -> None:
    """Increment a named counter. Best-effort: a failed write is swallowed.

    A counter is diagnostics, not the product -- losing one bump must never
    break a request. But the *store* failing is itself reported via `available`.
    """
    try:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO counters(name, value) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
                (name, by),
            )
            conn.commit()
    except (sqlite3.Error, OSError):
        pass


def counter(name: str) -> int:
    try:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()
            return int(row["value"]) if row else 0
    except (sqlite3.Error, OSError):
        return 0


def add_subscription(chat_id: str, symbol: str, interval: str, direction: str, price: float) -> int | None:
    """Arm an alert. Returns the row id, or None when the store cannot write.

    The UNIQUE constraint makes re-arming the same alert idempotent, so a user
    who taps twice does not get two messages for the same crossing.
    """
    try:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
            cur = conn.execute(
                "INSERT INTO subscriptions(chat_id, symbol, interval, direction, price, "
                "created_at, armed) VALUES(?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(chat_id, symbol, interval, direction, price) "
                "DO UPDATE SET armed = 1",
                (str(chat_id), symbol.upper(), interval, direction.upper(), float(price), int(time.time())),
            )
            conn.commit()
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = conn.execute(
                "SELECT id FROM subscriptions WHERE chat_id=? AND symbol=? AND interval=? "
                "AND direction=? AND price=?",
                (str(chat_id), symbol.upper(), interval, direction.upper(), float(price)),
            ).fetchone()
            return int(row["id"]) if row else None
    except (sqlite3.Error, OSError):
        return None


def list_subscriptions(chat_id: str | None = None) -> list[dict]:
    try:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
            if chat_id is None:
                rows = conn.execute("SELECT * FROM subscriptions WHERE armed = 1 ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM subscriptions WHERE armed = 1 AND chat_id = ? ORDER BY id", (str(chat_id),)
                ).fetchall()
            return [dict(r) for r in rows]
    except (sqlite3.Error, OSError):
        return []


def disarm(subscription_id: int) -> bool:
    """Mark an alert fired so it does not fire again. Returns True when it hit."""
    try:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
            cur = conn.execute(
                "UPDATE subscriptions SET armed = 0 WHERE id = ? AND armed = 1",
                (int(subscription_id),),
            )
            conn.commit()
            return cur.rowcount > 0
    except (sqlite3.Error, OSError):
        return False


def remove_subscription(chat_id: str, subscription_id: int) -> bool:
    """Delete an alert the user owns. Ownership is part of the WHERE clause."""
    try:
        with closing(_connect()) as conn:
            conn.executescript(_SCHEMA)
            cur = conn.execute(
                "DELETE FROM subscriptions WHERE id = ? AND chat_id = ?",
                (int(subscription_id), str(chat_id)),
            )
            conn.commit()
            return cur.rowcount > 0
    except (sqlite3.Error, OSError):
        return False


def stats() -> dict:
    """A small, honest summary for the status surface. Never raises."""
    ok = available()
    subs = 0
    visits = 0
    if ok:
        try:
            with closing(_connect()) as conn:
                subs = int(
                    conn.execute("SELECT COUNT(*) c FROM subscriptions WHERE armed = 1").fetchone()["c"]
                )
                row = conn.execute("SELECT value FROM counters WHERE name = 'visits'").fetchone()
                visits = int(row["value"]) if row else 0
        except (sqlite3.Error, OSError):
            ok = False
    # A writable local file is durable across a process restart but not across a
    # redeploy on an ephemeral host; only an explicit DESK_DATA_DIR on a mounted
    # disk is claimed as durable.
    explicit = bool(os.getenv("DESK_DATA_DIR", "").strip())
    return {
        "available": ok,
        "path": str(db_path()),
        "durable": explicit,
        "note": (
            "persists across process restarts; on an ephemeral host it is "
            "lost on redeploy -- set DESK_DATA_DIR to a mounted disk for "
            "real durability"
            if not explicit
            else "backed by an explicit DESK_DATA_DIR"
        ),
        "armed_alerts": subs,
        "visits": visits,
    }
