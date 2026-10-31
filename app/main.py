"""AnalysisDesk API.

An honest trading-analysis service: real market data in, computed indicators and
a transparent signal out, with an AI layer that only explains.

Endpoints:
  GET /                     -> the web desk
  GET /api/health           -> liveness
  GET /api/symbols          -> watchlist
  GET /api/analyze          -> full analysis for one symbol/timeframe
  GET /api/backtest         -> walk-forward record of the signal engine
  GET /api/alerts           -> list armed alerts for a chat
  POST /api/alerts          -> arm a price-crossing alert
  DELETE /api/alerts/{id}   -> remove one of your alerts
  GET /api/news             -> keyless headlines beside the read
  GET /api/candles          -> raw candles for the chart
"""

from __future__ import annotations

import hmac
import itertools
import os
import time
import tomllib
from collections import defaultdict, deque
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import ai, alerts, backtest, errors, indicators, market, news, schemas, signals, store

load_dotenv()


def _version() -> str:
    """The one place the version is written is pyproject.toml.

    It used to be typed here as well, so `status.version` and the packaged
    version could disagree with nothing to catch it -- the same two-copies-of-
    one-fact failure this project keeps removing. Prefer the installed metadata;
    when the package is not installed (the container copies the code but does
    not build a wheel), read the same pyproject.toml the metadata was built
    from. Both paths lead to one file, so there is still a single source.
    """
    try:
        return _dist_version("analysisdesk")
    except PackageNotFoundError:
        pass
    try:
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        return tomllib.loads(pyproject.read_text("utf-8"))["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "0.0.0+unknown"


# The interactive docs pages are off, and that is deliberate. The default
# Swagger and ReDoc pages load their JavaScript from a CDN, which our own
# `script-src 'self'` policy blocks -- so they answered 200 as an empty shell, an
# all-clear over a page that never worked. A surface that cannot run is not
# served. The machine-readable schema at /openapi.json is kept: it is plain JSON,
# no CDN, no CSP carve-out, and it is what a client generates code from. The API
# is also described in prose in README.md.
app = FastAPI(
    title="AnalysisDesk",
    version=_version(),
    docs_url=None,
    redoc_url=None,
)

# Open the durable store once at import. Failure is not fatal: the desk serves
# reads from memory either way, and /api/status reports the store as unavailable
# rather than pretending it is there.
store.init()

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
# Optional. When set, every caller must present it -- the bundled page included.
# It used to exempt the page via `Sec-Fetch-Site: same-origin`, which a browser
# sets and page script cannot; but a *script* sets it freely, so the header was a
# bypass, not a credential. See guard() below.
API_KEY = os.getenv("DESK_API_KEY", "").strip()

# The alerts API is service-to-service: the bot arms and reads a user's alerts,
# and the user never calls it directly (the bundled page does not touch it). So
# its credential is a shared secret held by that service, not the caller's
# chat id. A chat id is an *identifier*, not a secret -- Telegram chat ids leak
# through groups and forwarded messages -- so treating it as one let anyone who
# guessed a number read, arm, and delete another user's alerts. Unset means the
# endpoints are *closed* (503), not open: an alerts feature with no credential
# is not a feature to expose, and failing open would be the bypass again.
ALERTS_TOKEN = os.getenv("ALERTS_TOKEN", "").strip()


async def _alerts_guard(request: Request) -> None:
    """Gate the alerts endpoints on the service token, fail-closed.

    Order matters: the shared key/rate gate runs first (so an unconfigured desk
    still throttles scraping), then the token. When `ALERTS_TOKEN` is unset the
    endpoint answers 503 -- "not configured" -- rather than serving the data,
    because the only safe reading of "no credential set" is "do not open".
    """
    if not ALERTS_TOKEN:
        raise HTTPException(503, "alerts are not configured on this deployment")
    if not hmac.compare_digest(request.headers.get("x-alerts-token", ""), ALERTS_TOKEN):
        raise HTTPException(401, "missing or invalid alerts token")


_hits: dict[str, deque[float]] = defaultdict(deque)


# Trust for X-Forwarded-For, off by default. A header the caller sets is not an
# identity: trusted blindly, one caller mints a fresh value per request and
# walks past the limit. Turn it on only when a proxy you control sits in front
# and appends the real client as the last hop (Render does; the blueprint sets
# this). Off, the identity is the socket peer -- and the run command passes
# --no-proxy-headers so uvicorn cannot rewrite that peer from the same header.
TRUST_PROXY = os.getenv("DESK_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}


def _client_ip(request: Request) -> str:
    """The caller's identity for the rate limit.

    Read from the last X-Forwarded-For hop only when `DESK_TRUST_PROXY` says a
    proxy we control is in front and wrote that hop. Untrusted, the header is
    whatever the caller sent, so it is ignored and the socket peer is used
    instead. Reading it untrusted was the bug: a script sent a new value per
    request, got a new bucket each time, and stepped around the limit entirely.
    The last hop is right *behind a proxy* because each proxy appends the
    address it saw; it is wrong with nothing in front, which is the case the
    default now covers.
    """
    if TRUST_PROXY:
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
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
    """Gate the data endpoints: optional key, then a per-IP rate limit.

    The key, when set, is required from *every* caller. It used to exempt any
    request carrying `Sec-Fetch-Site: same-origin`, on the theory that only the
    bundled page sends it. True of a browser, false of a script: the header is
    set by the client, so `curl -H 'Sec-Fetch-Site: same-origin'` walked past
    the key. A header the caller controls is not a credential. No exemption is
    offered now -- the gate fails closed, because the only alternative is a
    bypass dressed as a policy.
    """
    if API_KEY and not hmac.compare_digest(request.headers.get("x-api-key", ""), API_KEY):
        raise HTTPException(401, "missing or invalid API key")
    if not _within_rate(_client_ip(request)):
        raise HTTPException(429, "too many requests; try again shortly",
                            headers={"Retry-After": "60"})


# The one failure a user actually reads in the card is a market read that could
# not complete. It used to be Arabic whatever language was asked for, so an
# English reader got a foreign sentence under an English heading -- the opposite
# of the bilingual rule every other string follows. It now follows `lang`.
_MARKET_ERR = {
    "ar": "تعذّر قراءة بيانات السوق",
    "en": "could not read market data",
}


def _market_error(lang: str, exc: object) -> str:
    return f"{_MARKET_ERR.get(lang, _MARKET_ERR['en'])}: {exc}"


# A symbol reaches a URL template, so the check lives at that boundary in
# market.py, where every caller is covered -- not only this API. The API refuses
# a bad symbol early, before it costs a rate-limit token; the market layer
# refuses it again, before it can become a path. One predicate, one source.
_valid_symbol = market.valid_symbol


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


@app.get("/api/health", response_model=schemas.HealthOut)
async def health():
    return {"status": "ok", "model": ai.MODEL, "model_key": bool(os.getenv("GROQ_API_KEY"))}


@app.get("/api/status", response_model=schemas.StatusOut)
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
        # Whether the alerts endpoints will serve at all. Unset token = they
        # answer 503, so a client can tell "closed" from "wrong token" without
        # guessing -- the same honesty `api_key_required` gives for the key.
        "alerts_configured": bool(ALERTS_TOKEN),
        # The window is per-process, so this is the number of distinct callers
        # seen in the last minute -- the same set the limit is applied to.
        "tracked_clients": len(_hits),
        # Unhandled exceptions only. A market source being down is expected and
        # reported as 502 with a reason; it is not a bug and is not counted here.
        # Aggregate by type -- no messages, no paths, no frames.
        "errors": errors.summary(),
        # The durable store. `durable: false` on an ephemeral host is stated
        # rather than implied -- a restart keeping your alerts is not the same
        # promise as a redeploy keeping them.
        "store": store.stats(),
    }


@app.get("/api/symbols", response_model=schemas.SymbolsOut)
async def symbols():
    return {"symbols": WATCHLIST, "intervals": INTERVALS}


@app.get("/api/analyze", response_model=schemas.AnalyzeOut, response_model_exclude_unset=True)
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
    if not _valid_symbol(symbol):
        raise HTTPException(400, "unsupported symbol; use a Yahoo Finance symbol, e.g. EURUSD=X")
    # Charge the quota only for a request we would actually serve: a bad
    # parameter is free, so a typo cannot lock a user out for a minute.
    await guard(request)

    try:
        series = await market.fetch_series(symbol, interval)
    except market.MarketDataError as exc:
        # Fail loudly. A silent all-clear is the one thing we never emit.
        raise HTTPException(502, _market_error(lang, exc)) from exc

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


@app.get("/api/backtest", response_model=schemas.BacktestOut)
async def backtest_endpoint(
    request: Request,
    symbol: str = Query(..., description="Yahoo Finance symbol, e.g. EURUSD=X"),
    interval: str = Query("1h"),
    step: int = Query(1, ge=1, le=50, description="score a signal every `step` bars"),
    lang: str = Query("ar"),
):
    """Walk the signal engine forward over the real candles and report its record.

    This is the honest answer to "does the strategy work?" -- a realised result
    over a historical sample, with the sample size and the missing costs stated
    on the result itself, never a forecast.
    """
    if interval not in market.INTERVALS:
        raise HTTPException(400, f"unsupported interval; use one of {list(market.INTERVALS)}")
    if lang not in ("ar", "en"):
        raise HTTPException(400, "lang must be ar or en")
    if not _valid_symbol(symbol):
        raise HTTPException(400, "unsupported symbol; use a Yahoo Finance symbol, e.g. EURUSD=X")
    await guard(request)
    try:
        series = await market.fetch_series(symbol, interval)
    except market.MarketDataError as exc:
        raise HTTPException(502, _market_error(lang, exc)) from exc
    result = backtest.run(series, step=step)
    d = result.as_dict()
    d["caveats_ar"] = backtest.CAVEATS_AR
    return d


@app.get("/api/alerts", response_model=schemas.AlertsOut)
async def list_alerts(request: Request, chat_id: str = Query(...)):
    """List a chat's armed alerts, scoped to the chat and gated by the service
    token. The chat id selects the row; it is not the credential."""
    await guard(request)
    await _alerts_guard(request)
    return {"alerts": store.list_subscriptions(chat_id), "delivery_on": alerts.alerts_enabled()}


@app.post("/api/alerts")
async def create_alert(  # noqa: PLR0913, PLR0917 - query params, not a call site
    request: Request,
    chat_id: str = Query(...),
    symbol: str = Query(...),
    price: float = Query(...),
    direction: str = Query("BUY"),
    interval: str = Query("1h"),
):
    """Arm an alert. Returns 503 when the store cannot persist it -- an alert
    that is not written down is not an alert, and pretending otherwise is the
    one thing this desk does not do."""
    if interval not in market.INTERVALS:
        raise HTTPException(400, f"unsupported interval; use one of {list(market.INTERVALS)}")
    if direction not in ("BUY", "SELL"):
        raise HTTPException(400, "direction must be BUY or SELL")
    if price <= 0:
        raise HTTPException(400, "price must be positive")
    if not _valid_symbol(symbol):
        raise HTTPException(400, "unsupported symbol; use a Yahoo Finance symbol, e.g. EURUSD=X")
    await guard(request)
    await _alerts_guard(request)
    sid = store.add_subscription(chat_id, symbol, interval, direction, price)
    if sid is None:
        raise HTTPException(503, "the alert store is unavailable; nothing was saved")
    return {"id": sid, "armed": True, "delivery_on": alerts.alerts_enabled()}


@app.delete("/api/alerts/{alert_id}")
async def delete_alert(request: Request, alert_id: int, chat_id: str = Query(...)):
    """Remove one alert, but only if the caller's chat id owns it."""
    await guard(request)
    await _alerts_guard(request)
    ok = store.remove_subscription(chat_id, alert_id)
    if not ok:
        raise HTTPException(404, "no such alert for this chat")
    return {"removed": alert_id}


@app.get("/api/news", response_model=schemas.NewsOut)
async def news_endpoint(request: Request, limit: int = Query(12, ge=1, le=40)):
    """Keyless headlines beside the read. Titles only, and the feed health is
    reported per source so a silent feed is visible as a failure, not an empty
    list that looks like "no news"."""
    await guard(request)
    result = await news.fetch_news(limit=limit)
    return result.as_dict()


@app.get("/api/candles", response_model=schemas.CandlesOut)
async def candles(request: Request, symbol: str, interval: str = "1h", lang: str = Query("ar")):
    if interval not in market.INTERVALS:
        raise HTTPException(400, f"unsupported interval; use one of {list(market.INTERVALS)}")
    if not _valid_symbol(symbol):
        raise HTTPException(400, "unsupported symbol; use a Yahoo Finance symbol, e.g. EURUSD=X")
    await guard(request)
    try:
        series = await market.fetch_series(symbol, interval)
    except market.MarketDataError as exc:
        raise HTTPException(502, _market_error(lang, exc)) from exc
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
    store.bump("visits")
    return FileResponse(str(WEB / "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Browsers ask for /favicon.ico by default even when the page declares an SVG
    # icon, and a 404 there is a broken icon on every visit. Serve the SVG for
    # both paths so the tab always has a mark. Served from the same origin, so it
    # stays inside `img-src 'self'`.
    return FileResponse(str(WEB / "favicon.svg"), media_type="image/svg+xml")


@app.get("/pitch")
async def pitch():
    # The project pitch is a static file in web/, so it ships in the image and is
    # served from the same origin as the desk. It carries no script -- the
    # language switch is pure CSS -- so it lives inside script-src 'self' without
    # a policy carve-out, the same rule the desk page follows.
    return FileResponse(str(WEB / "pitch.html"))
