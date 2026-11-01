"""Telegram command receiver: arm a level from the chat, not the page.

The desk could already be *told* a level through the HTTP API, but that API is
service-to-service (it wants a shared token), so the person who actually trades
had no way in. This is that way in: send the bot a line, it arms the alert.

What this is, and what it is not:

  * It **receives** commands over Telegram's long poll (`getUpdates`) and
    replies with what it did. It is not a webhook -- a webhook would need a
    public URL and a secret path, and the host already has one door.
  * It **arms alerts**. It never places an order, never touches money, and
    never speaks with authority: a reply states the level the *user* named.

Two rules it keeps, both from the project's spine:

  * **Fail closed.** An empty allowlist means nobody is served. A bot that
    answers anyone who finds it would let a stranger fill the store.
  * **Say when the clock is stopped.** Arming an alert is useless if nothing
    checks the price, so a reply that arms one also says whether the scheduler
    is running -- see `scheduler.py` for why it might not be.

Command parsing and dispatch are pure (`parse_command`, `handle_command`,
`drain`), so the logic is tested without a network or a bot token. The only
thing that talks to Telegram is `poll_forever`, and it never raises out of its
task: a broken poll must not take the web process down with it.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx

from . import alerts, errors, market, scheduler, store

TELEGRAM_API = "https://api.telegram.org"
# Telegram holds a long poll open for this many seconds before answering empty.
_POLL_TIMEOUT = 25
_BACKOFF_S = 5

# Heartbeat, so /api/status can show a poll that stopped. Same one-element-list
# pattern main.py uses for its counters -- no `global` statement needed.
_last_poll = [0.0]
_note = [""]

_HELP = (
    "🤖 AnalysisDesk — بوت التنبيهات\n\n"
    "/alert <الرمز> <السعر> [BUY|SELL] [الإطار]\n"
    "  مثال: /alert XAUUSD 4000 BUY 1h\n"
    "/list — تنبيهاتك المسلّحة\n"
    "/remove <الرقم> — احذف تنبيهاً\n"
    "/status — حالة المجدول والمخزن\n"
    "/help — هذه القائمة\n\n"
    "التنبيه يوصل لما السعر يعبر المستوى الذي سمّيته — وليس توصية."
)


def bot_enabled() -> bool:
    """Whether the receiver is turned on. Off unless explicitly set."""
    return os.getenv("BOT_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _bot_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def allowed_chats() -> set[str]:
    """The chats this bot will answer. Fail closed: empty means nobody.

    A token in the environment is a credential, not a permission -- it may have
    been set only so alert *delivery* works. So the allowlist is its own
    decision: `TELEGRAM_ALLOWED_CHATS` (comma-separated), falling back to the
    single `TELEGRAM_CHAT_ID`. With neither set, the set is empty and every
    message is refused, which is the only safe default for an open endpoint.
    """
    raw = os.getenv("TELEGRAM_ALLOWED_CHATS", "").strip()
    if raw:
        return {c.strip() for c in raw.split(",") if c.strip()}
    single = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return {single} if single else set()


def ready() -> bool:
    """Everything the poller needs is present. Off if any part is missing."""
    return bool(bot_enabled() and _bot_token() and allowed_chats())


def parse_command(text: str) -> tuple[str, list[str]]:
    """Split a message into (command, args). Pure.

    Telegram sends `/alert@MyBot ...` in groups, so the `@username` suffix is
    stripped -- otherwise the same command works in a DM and fails in a group,
    which is exactly the kind of silent difference this project refuses.
    """
    parts = (text or "").strip().split()
    if not parts:
        return "", []
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    return cmd, parts[1:]


def _arm_note() -> str:
    """A line for the reply that says whether the clock is actually running."""
    if not scheduler.scheduler_enabled():
        return "\n⚠ المجدول موقّف: التنبيه انحفظ، لكن لا شيء يفحص السعر دورياً."
    if scheduler.state().get("last_run_s_ago") is None:
        return "\n⏳ المجدول شغّال لكنه لم يُكمل فحصاً بعد."
    return ""


def _cmd_alert(chat_id: str, args: list[str]) -> str:
    if len(args) < 2:
        return "الصيغة: /alert <الرمز> <السعر> [BUY|SELL] [الإطار]"
    symbol = args[0].upper()
    try:
        price = float(args[1])
    except ValueError:
        return "السعر يجب أن يكون رقماً."
    direction = args[2].upper() if len(args) > 2 else "BUY"
    interval = args[3].lower() if len(args) > 3 else "1h"
    if not market.valid_symbol(symbol):
        return f"رمز غير مدعوم: {symbol}"
    if interval not in market.INTERVALS:
        return f"إطار غير مدعوم: {interval}. المتاح: {', '.join(market.INTERVALS)}"
    if direction not in ("BUY", "SELL"):
        return "الاتجاه يجب أن يكون BUY أو SELL."
    if price <= 0:
        return "السعر يجب أن يكون موجباً."
    sid = store.add_subscription(chat_id, symbol, interval, direction, price)
    if sid is None:
        return "المخزن غير متاح؛ لم يُحفظ شيء."
    arrow = "صعود ▲" if direction == "BUY" else "هبوط ▼"
    return f"تم. تنبيه #{sid}: {symbol} ({interval}) يعبر {price} — {arrow}.{_arm_note()}"


def _cmd_list(chat_id: str) -> str:
    subs = store.list_subscriptions(chat_id)
    if not subs:
        return "ما عندك تنبيهات مسلّحة."
    lines = ["تنبيهاتك:"]
    for s in subs:
        d = "صعود ▲" if s["direction"] == "BUY" else "هبوط ▼"
        lines.append(f"#{s['id']} {s['symbol']} ({s['interval']}) يعبر {s['price']} — {d}")
    return "\n".join(lines)


def _cmd_remove(chat_id: str, args: list[str]) -> str:
    if not args:
        return "الصيغة: /remove <الرقم>"
    try:
        aid = int(args[0].lstrip("#"))
    except ValueError:
        return "الرقم يجب أن يكون عدداً."
    if store.remove_subscription(chat_id, aid):
        return f"انحذف #{aid}."
    return f"ما في تنبيه #{aid} يخصّك."


def _cmd_status() -> str:
    st = scheduler.state()
    sd = store.stats()
    last = st.get("last_run_s_ago")
    if not st.get("enabled"):
        sch = "موقّف"
    elif last is None:
        sch = "شغّال، لم يُكمل فحصاً بعد"
    else:
        sch = f"آخر فحص قبل {int(last)} ثانية"
    durable = "" if sd["durable"] else " (غير دائم — يُمحى عند إعادة النشر)"
    return (
        f"المجدول: {sch}\n"
        f"المخزن: {'متاح' if sd['available'] else 'غير متاح'}{durable}\n"
        f"تنبيهات مسلّحة: {sd['armed_alerts']}"
    )


def handle_command(chat_id: str, text: str) -> str:
    """Turn one message into one reply. Pure apart from the store it reads.

    The allowlist is checked first, before the command is even parsed: an
    unauthorised chat gets one refusal and touches nothing else.
    """
    if str(chat_id) not in allowed_chats():
        return "هذا البوت مخصّص لمالكه."
    cmd, args = parse_command(text)
    if cmd in ("start", "help"):
        return _HELP
    if cmd == "alert":
        return _cmd_alert(chat_id, args)
    if cmd == "list":
        return _cmd_list(chat_id)
    if cmd == "remove":
        return _cmd_remove(chat_id, args)
    if cmd == "status":
        return _cmd_status()
    return "أمر غير معروف. أرسل /help."


def drain(payload: dict, offset: int) -> tuple[int, list[tuple[str, str]]]:
    """Pure: turn a getUpdates payload into (new_offset, [(chat, reply)]).

    `offset` is advanced past every update seen -- including ones with no text
    -- so Telegram does not hand the same update back forever.
    """
    replies: list[tuple[str, str]] = []
    for upd in payload.get("result", []) or []:
        offset = max(offset, int(upd.get("update_id", 0)) + 1)
        msg = upd.get("message") or {}
        text = msg.get("text")
        chat = (msg.get("chat") or {}).get("id")
        if text and chat is not None:
            replies.append((str(chat), handle_command(str(chat), text)))
    return offset, replies


async def poll_forever() -> None:
    """Long-poll Telegram, dispatch each message, reply. Never raises out.

    A single `getUpdates` and a single reply, in a loop, is the whole thing --
    no webhook, no public URL. When the process is suspended (see scheduler.py
    on why a free host does that) the loop simply stops; Telegram holds undelivered
    updates for 24 hours, so a command sent while asleep is answered on wake.
    """
    token = _bot_token()
    if not token:
        return
    url = f"{TELEGRAM_API}/bot{token}/getUpdates"
    offset = 0
    _note[0] = ""
    async with httpx.AsyncClient(timeout=_POLL_TIMEOUT + 10) as client:
        while True:
            try:
                r = await client.get(url, params={"offset": offset, "timeout": _POLL_TIMEOUT})
                if r.status_code == 409:
                    # Another getUpdates consumer (or a webhook) holds the bot.
                    # A deployment fault, not our bug: note it, do not spam errors.
                    _note[0] = "409: another getUpdates consumer or a webhook is set"
                    await asyncio.sleep(30)
                    continue
                data = r.json()
                if not data.get("ok"):
                    await asyncio.sleep(_BACKOFF_S)
                    continue
                _last_poll[0] = time.time()
                _note[0] = ""
                offset, replies = drain(data, offset)
                for chat, reply in replies:
                    # The sender is blocking; keep it off the event loop.
                    await asyncio.to_thread(alerts.send_message, chat, reply)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a poll must not kill the app
                errors.record(exc, where="bot.poll")
                await asyncio.sleep(_BACKOFF_S)


def state() -> dict:
    """Honest heartbeat for /api/status. No chat ids, only their count."""
    now = time.time()
    last = _last_poll[0]
    return {
        "enabled": bot_enabled(),
        "ready": ready(),
        "allowed_chats": len(allowed_chats()),
        "last_poll_s_ago": round(now - last, 1) if last else None,
        "note": _note[0],
    }
