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

import os
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import ai, indicators, market, signals

load_dotenv()

app = FastAPI(title="AnalysisDesk", version="1.0.0")
WEB = Path(__file__).resolve().parent.parent / "web"

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
    response = await call_next(request)
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

    ind = indicators.snapshot(series)
    sig = signals.signal_dict(signals.evaluate(ind))
    narrative = await ai.narrate(series.symbol, interval, ind, sig, lang)

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
        },
        "spot": spot,
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
