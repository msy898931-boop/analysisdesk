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
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from . import ai, indicators, market, signals

load_dotenv()

app = FastAPI(title="AnalysisDesk", version="1.0.0")
WEB = Path(__file__).resolve().parent.parent / "web"

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
    symbol: str = Query(..., description="Yahoo Finance symbol, e.g. EURUSD=X"),
    interval: str = Query("1h"),
    lang: str = Query("ar"),
):
    if interval not in market.INTERVALS:
        raise HTTPException(400, f"unsupported interval; use one of {list(market.INTERVALS)}")
    if lang not in ("ar", "en"):
        raise HTTPException(400, "lang must be ar or en")

    try:
        series = await market.fetch_series(symbol, interval)
    except market.MarketDataError as exc:
        # Fail loudly. A silent all-clear is the one thing we never emit.
        raise HTTPException(502, f"تعذّر قراءة بيانات السوق: {exc}")

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
async def candles(symbol: str, interval: str = "1h"):
    try:
        series = await market.fetch_series(symbol, interval)
    except market.MarketDataError as exc:
        raise HTTPException(502, f"تعذّر قراءة بيانات السوق: {exc}")
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
