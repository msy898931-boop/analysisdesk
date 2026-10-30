"""Price alerts: tell a user when a level they named is actually crossed.

The desk used to be a thing you had to *look at*. A trader's real need is the
opposite: "wake me when XAU/USD touches 4000". This module is that, and it is
built on the same rule as everything else here -- say exactly what happened.

What an alert means, and what it does not:

  * It fires when the price **crosses** the level the user named, from one side
    to the other. It is not "price is above 4000" (which would fire forever) and
    it is not a recommendation. The message states the level and the two prices,
    so the crossing is checkable.

Delivery is **opt-in and off by default**:

  * The engine and the subscription store are always live. *Sending* a message
    to someone's Telegram is a real-world side effect, so it happens only when
    `ALERTS_ENABLED` is truthy **and** a bot token is present. Otherwise the
    firing is recorded and reported, and no message leaves the process. A test
    pins that default so it cannot quietly change.

No polling loop is started here. A scheduler calls `run_once()`; keeping the loop
out of the module makes the logic testable without a clock.
"""

from __future__ import annotations

import os

import httpx

from . import market, store

TELEGRAM_API = "https://api.telegram.org"


def alerts_enabled() -> bool:
    """Whether delivery is turned on. Off unless explicitly set."""
    return os.getenv("ALERTS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def crossed(direction: str, level: float, prev: float, cur: float) -> bool:
    """Did the price cross `level` in the direction the user asked about?

    A BUY alert fires when the price rises through the level (it was below and is
    now at or above). A SELL alert fires when it falls through. Equality counts
    as a touch, so a level that is hit exactly still fires -- but only on the
    bar where it happens, because the store disarms the alert when it fires.
    """
    d = direction.upper()
    if d == "BUY":
        return prev < level <= cur
    if d == "SELL":
        return prev > level >= cur
    return False


def _message(sub: dict, prev: float, cur: float) -> str:
    d = sub["direction"].upper()
    arrow = "▲" if d == "BUY" else "▼"
    return (
        f"{arrow} تنبيه AnalysisDesk\n"
        f"{sub['symbol']} ({sub['interval']}): عبر السعر المستوى الذي حدّدته\n"
        f"المستوى: {sub['price']}\n"
        f"من {prev} إلى {cur}\n"
        f"الاتجاه: {'صعود' if d == 'BUY' else 'هبوط'}\n"
        f"— هذا عبور لمستوى سمّيته أنت، وليس توصية."
    )


def _telegram_send(chat_id: str, text: str) -> bool:
    """Send one message. Returns whether it left the process successfully.

    Never raises: an alert that could not be delivered must not crash the loop.
    The token is read here and used only in the request URL, never logged.
    """
    token = _bot_token()
    if not token:
        return False
    try:
        r = httpx.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200 and bool(r.json().get("ok"))
    except (httpx.HTTPError, ValueError):
        return False


def evaluate(subs: list[dict], prices: dict[str, tuple[float, float]]) -> list[dict]:
    """Pure: which subscriptions fired, given (prev, cur) per symbol.

    `prices` maps a symbol to (previous price, current price). A symbol with no
    price is skipped, not guessed -- an unknown must not be treated as a known.
    """
    fired = []
    for sub in subs:
        key = sub["symbol"]
        if key not in prices:
            continue
        prev, cur = prices[key]
        if crossed(sub["direction"], float(sub["price"]), prev, cur):
            fired.append({"subscription": sub, "prev": prev, "cur": cur})
    return fired


async def _prices_for(symbols: set[str], interval: str) -> dict[str, tuple[float, float]]:
    """Fetch (prev_close, last_close) for each symbol. Skips what it cannot read."""
    out: dict[str, tuple[float, float]] = {}
    for sym in symbols:
        try:
            series = await market.fetch_series(sym, interval)
        except market.MarketDataError:
            continue
        cs = series.candles
        if len(cs) < 2:
            continue
        out[sym] = (cs[-2].c, cs[-1].c)
    return out


async def run_once(*, transport=None) -> dict:
    """One pass: read subscriptions, fetch prices, fire and disarm what crossed.

    `transport` is injectable so tests exercise the real matching logic without a
    network call. The default is the Telegram sender, and it is used only when
    `alerts_enabled()` is true.
    """
    subs = store.list_subscriptions()
    if not subs:
        return {"checked": 0, "fired": 0, "delivered": 0, "delivery_on": alerts_enabled()}

    by_interval: dict[str, set[str]] = {}
    for s in subs:
        by_interval.setdefault(s["interval"], set()).add(s["symbol"])

    prices: dict[str, tuple[float, float]] = {}
    for interval, syms in by_interval.items():
        prices.update(await _prices_for(syms, interval))

    fired = evaluate(subs, prices)
    send = transport or _telegram_send
    delivered = 0
    for f in fired:
        sub = f["subscription"]
        store.disarm(sub["id"])  # disarm first: a re-send is worse than a miss
        if alerts_enabled():
            if send(sub["chat_id"], _message(sub, f["prev"], f["cur"])):
                delivered += 1
    return {
        "checked": len(subs),
        "fired": len(fired),
        "delivered": delivered,
        "delivery_on": alerts_enabled(),
    }
