"""AnalysisDesk API.

An honest trading-analysis service: real market data in, computed indicators and
a transparent signal out, with an AI layer that only explains.

Endpoints:
  GET /                     -> the web desk
  GET /api/health           -> liveness
  GET /api/symbols          -> watchlist
  GET /api/analyze          -> full analysis for one symbol/timeframe
"""

from __future__ import annotations

import itertools
import os
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import ai, errors, indicators, market, signals

load_dotenv()

app = FastAPI(title="AnalysisDesk", version="1.0.0")
WEB = Path(__file__).resolve().parent.parent / "web"

# Process-level counters for the diagnostics surface. `_served` counts responses
# the middleware has seen; `_started` anchors uptime. Both are per-process, like
# the rate counter, so a restart resets them -- /api/status says so rather than
# letting a small number read as "idle" when it means "recently restarted".
_started = time.time()
_served = [0]
_req_seq = itertools.count(1)

# The desk renders server-supplied strings, so a tag that reaches the browser is
# a tag the browser obeys. Two things stop that: the client escapes every value,
# and this header refuses to run any script that did not come from our own
# origin. 'unsafe-inline' is granted to styles only -- the page uses style
# attributes for the layout; script stays strict, which is what defeats XSS.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # One id per request, echoed on the response so a reader can match what they
    # saw to the log line the error tracker wrote for it.
    rid = f"r{next(_req_seq):x}"
    request.state.request_id = rid
    response = await call_next(request)
    _served[0] += 1
    response.headers["X-Request-Id"] = rid
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Tell the browser to stay on https for this host once it has seen us there.
    # Harmless over plain http (browsers ignore it on a non-https origin), so it
    # is safe to send unconditionally; Render terminates TLS in front of us.
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Nothing here uses a camera, microphone, or location, so refuse them all.
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Record an unexpected exception and answer without leaking it.

    A bug of ours must be visible in the log -- the point of the tracker -- and
    must not be visible to the caller: no traceback, no internal path, just the
    request id, which is enough to find the full line. The traceback stays in
    the log, where it is useful and not public.
    """
    rid = getattr(request.state, "request_id", "")
    errors.record(exc, where="http", request_id=rid, path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal error", "request_id": rid},
        headers={"X-Request-Id": rid},
    )


# --- abuse guard ------------------------------------------------------------
# The analysis endpoints spend the owner's model quota, so an open door is a
# cost risk even when it is not a data risk. The limit is per client IP and per
# process: a restart clears it and a second worker would not share it. The image
# therefore pins --workers 1 (see the Dockerfile), which makes the in-process
# counter the instance counter -- one worker, one limit. It still stops casual
# scraping and runaway loops, which is what actually threatens the quota.
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))
# Optional. When set, a caller that is not the bundled page must present it.
API_KEY = os.getenv("DESK_API_KEY", "").strip()

_hits: dict[str, deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    # Take the LAST forwarded hop, not the first. Each proxy appends the address
    # it saw, so the last entry is the one our own edge (Render) wrote and the
    # caller cannot forge it. The first entry is whatever the caller sent -- a
    # script could put a fresh value there on every request and walk straight
    # past the limit, which is the whole point of the limit.
    fwd = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in fwd.split(",") if h.strip()]
    if hops:
        return hops[-1]
    return request.client.host if request.client else "unknown"


_swept = [0.0]


def _prune(now: float) -> None:
    """Drop buckets whose last hit has left the window.

    Without this the dict keeps one entry per distinct caller forever, and a
    caller can invent identities -- so it would only ever grow. A bucket still
    inside its window is kept: it is still counting.
    """
    if now - _swept[0] < 60:
        return
    _swept[0] = now
    for ip in [k for k, q in _hits.items() if not q or now - q[-1] > 60]:
        del _hits[ip]


def _within_rate(ip: str) -> bool:
    now = time.time()
    _prune(now)
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return False
    q.append(now)
    return True


async def guard(request: Request) -> None:
    """Gate the data endpoints: optional key, then a per-IP rate limit."""
    if API_KEY and request.headers.get("x-api-key") != API_KEY:
        # The bundled page cannot carry the key without exposing it, so it is
        # trusted by a header a page's own script cannot forge: the browser sets
        # Sec-Fetch-Site and it is a forbidden header name. A direct API call
        # sends nothing, and is turned away.
        if request.headers.get("sec-fetch-site") != "same-origin":
            raise HTTPException(401, "missing or invalid API key")
    if not _within_rate(_client_ip(request)):
        raise HTTPException(429, "too many requests; try again shortly",
                            headers={"Retry-After": "60"})


WATCHLIST = [
    {"symbol": "EURUSD=X", "name_ar": "يورو/دولار", "name_en": "EUR/USD", "class": "forex"},
    {"symbol": "GBPUSD=X", "name_ar": "جنيه/دولار", "name_en": "GBP/USD", "class": "forex"},
    {"symbol": "USDJPY=X", "name_ar": "دولار/ين", "name_en": "USD/JPY", "class": "forex"},
    {"symbol": "BTC-USD", "name_ar": "بيتكوين", "name_en": "Bitcoin", "class": "crypto"},
    {"symbol": "ETH-USD", "name_ar": "إيثيريوم", "name_en": "Ethereum", "class": "crypto"},
    {"symbol": "GC=F", "name_ar": "الذهب الآجل", "name_en": "Gold Futures", "class": "commodity", "note_ar": "عقد COMEX", "note_en": "COMEX contract"},
    {"symbol": "XAUUSD", "name_ar": "الذهب (سبوت)", "name_en": "Gold (Spot)", "class": "commodity", "note_ar": "ما تراه عند وسيطك", "note_en": "What your broker shows"},
    {"symbol": "CL=F", "name_ar": "النفط", "name_en": "Crude Oil", "class": "commodity", "note_ar": "عقد الآجل", "note_en": "Futures"},
    {"symbol": "^GSPC", "name_ar": "S&P 500", "name_en": "S&P 500", "class": "index"},
    {"symbol": "AAPL", "name_ar": "أبل", "name_en": "Apple", "class": "stock"},
    {"symbol": "TSLA", "name_ar": "تسلا", "name_en": "Tesla", "class": "stock"},
]

INTERVALS = {
    "5m": {"ar": "٥ دقائق", "en": "5m"},
    "15m": {"ar": "١٥ دقيقة", "en": "15m"},
    "1h": {"ar": "ساعة", "en": "1h"},
    "1d": {"ar": "يومي", "en": "1d"},
    "1wk": {"ar": "أسبوعي", "en": "1wk"},
}


@app.get("/api/health")
async def health():
    return {"status": "ok", "model": ai.MODEL, "model_key": bool(os.getenv("GROQ_API_KEY"))}


@app.get("/api/status")
async def status():
    """Diagnostics: what this process is actually doing, not just that it is up.

    /api/health answers "is it alive". This answers the questions an operator
    asks next -- how long has it run, how much has it served, is the model path
    live, is the cache growing like a scan. Every field is process-local and
    resets on restart; `uptime_s` is the tell, so a small `served` reads as
    "recently restarted", never as "nothing happened".
    """
    return {
        "status": "ok",
        "version": app.version,
        # Render sets RENDER_GIT_COMMIT to the deployed revision. Exposing it
        # lets the watchdog answer "is the thing I pushed the thing that is
        # running" -- a green check on push that does not prove the deploy
        # landed would be the same kind of false all-clear this desk refuses.
        "commit": (os.getenv("RENDER_GIT_COMMIT", "") or "")[:12],
        "uptime_s": round(time.time() - _started, 1),
        "served": _served[0],
        "model": ai.MODEL,
        "model_key": bool(os.getenv("GROQ_API_KEY")),
        "models": ai.MODELS,
        "cache": market.cache_stats(),
        "rate_limit_per_min": RATE_LIMIT,
        "api_key_required": bool(API_KEY),
        # The window is per-process, so this is the number of distinct callers
        # seen in the last minute -- the same set the limit is applied to.
        "tracked_clients": len(_hits),
        # Unhandled exceptions only. A market source being down is expected and
        # reported as 502 with a reason; it is not a bug and is not counted here.
        # Aggregate by type -- no messages, no paths, no frames.
        "errors": errors.summary(),
    }


@app.get("/api/symbols")
async def symbols():
    return {"symbols": WATCHLIST, "intervals": INTERVALS}


@app.get("/api/analyze")
async def analyze(
    request: Request,
    symbol: str = Query(..., description="Yahoo Finance symbol, e.g. EURUSD=X"),
    interval: str = Query("1h"),
    lang: str = Query("ar"),
):
    if interval not in market.INTERVALS:
        raise HTTPException(400, f"unsupported interval; use one of {list(market.INTERVALS)}")
    if lang not in ("ar", "en"):
        raise HTTPException(400, "lang must be ar or en")
    # Charge the quota only for a request we would actually serve: a bad
    # parameter is free, so a typo cannot lock a user out for a minute.
    await guard(request)

    try:
        series = await market.fetch_series(symbol, interval)
    except market.MarketDataError as exc:
        # Fail loudly. A silent all-clear is the one thing we never emit.
        raise HTTPException(502, f"تعذّر قراءة بيانات السوق: {exc}") from exc

    # Provenance. A bare "Gold" number is ambiguous: the COMEX contract and spot
    # XAU/USD differ by a basis, so we name the instrument and, when we are on a
    # futures contract, also report the spot it tracks. On the spot instrument we
    # show the live broker quote for the same reason — so it can be checked.
    kind = "futures" if series.instrument_type == "FUTURE" else "spot"
    spot = None
    metal = market.FUTURES_TO_SPOT.get(series.symbol.upper()) or \
        market.SPOT_SYMBOLS.get(series.symbol.upper(), {}).get("metal")
    if metal:
        try:
            spot = await market.fetch_spot(metal)
        except market.MarketDataError as exc:
            spot = {"metal": metal, "error": str(exc)}

    # Put the charted candles on the broker's scale before anything is computed
    # from them. A gold-backed token stands in for spot XAU/USD (there is no
    # keyless spot feed), and it trades at a small premium, so every level the
    # card shows — entry, stop, target, support, resistance — would otherwise be
    # on the token's scale, not the number on the user's own screen. Rescaling by
    # one factor leaves every ratio (RSI, ATR%, Bollinger position) untouched.
    series = market.anchor_series_to_spot(series, spot)

    ind = indicators.snapshot(series)
    sig = signals.signal_dict(signals.evaluate(ind))
    narrative = await ai.narrate(series.symbol, interval, ind, sig, lang)

    # If the proxy stopped tracking spot, the levels are the token's, not gold's.
    # Say so on the card rather than letting a large silent gap stand.
    anchor = series.anchor or {}
    if series.instrument_type == "SPOT" and not anchor.get("ok"):
        if anchor.get("reason") == "proxy diverged":
            gap = f"{anchor.get('pct', 0):+.2f}%"
            sig["warnings"].append({
                "code": "proxy_diverged",
                "en": (f"the charted proxy is {gap} from live spot — it has stopped "
                       f"tracking gold, so these levels are the proxy's, not spot"),
                "ar": (f"البروكسي المُرسم بعيد {gap} عن السبوت الحيّ — فَقَد تتبّع الذهب، "
                       f"فهذه المستويات مستوياته هو لا مستويات السبوت"),
            })
        elif spot and spot.get("error"):
            sig["warnings"].append({
                "code": "spot_unread",
                "en": "the live spot quote could not be read — levels are the proxy's",
                "ar": "تعذّرت قراءة سعر السبوت الحيّ — المستويات مستويات البروكسي",
            })

    return {
        "symbol": series.symbol,
        "currency": series.currency,
        "exchange": series.exchange,
        "interval": interval,
        "as_of": series.candles[-1].t,
        "instrument": {
            "kind": kind,
            "name": series.short_name,
            "exchange": series.exchange,
            # Name the provider, not Yahoo's internal venue code ("CCY", "CCC").
            # The footer credits this, so it must be the thing that really served
            # the candles -- spot gold comes from Kraken, not Yahoo.
            "provider": (series.exchange if series.symbol.upper() in market.SPOT_SYMBOLS
                         else "Yahoo Finance"),
        },
        "spot": spot,
        "anchor": series.anchor,
        "indicators": ind,
        "signal": sig,
        "narrative": narrative,
        "disclaimer": (
            "تحليل تقني آلي مبني على بيانات سوق حقيقية. ليس توصية استثمارية ولا ضماناً لأي نتيجة."
            if lang == "ar" else
            "Automated technical analysis over real market data. Not investment advice, no guarantee of any outcome."
        ),
    }


@app.get("/api/candles")
async def candles(request: Request, symbol: str, interval: str = "1h"):
    if interval not in market.INTERVALS:
        raise HTTPException(400, f"unsupported interval; use one of {list(market.INTERVALS)}")
    await guard(request)
    try:
        series = await market.fetch_series(symbol, interval)
    except market.MarketDataError as exc:
        raise HTTPException(502, f"تعذّر قراءة بيانات السوق: {exc}") from exc
    # Same rescale as /api/analyze: the chart must sit on the broker's scale, or
    # it would draw the token's prices under a card that shows spot.
    if series.instrument_type == "SPOT":
        metal = market.SPOT_SYMBOLS.get(series.symbol.upper(), {}).get("metal")
        if metal:
            try:
                quote = await market.fetch_spot(metal)
            except market.MarketDataError:
                quote = None  # no quote: draw the raw candles rather than nothing
            if quote:
                series = market.anchor_series_to_spot(series, quote)
    return {
        "symbol": series.symbol,
        "interval": interval,
        "candles": [
            {"t": c.t, "o": c.o, "h": c.h, "l": c.l, "c": c.c} for c in series.candles[-200:]
        ],
    }


app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(WEB / "index.html"))
