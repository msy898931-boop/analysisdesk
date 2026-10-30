"""Real market data layer.

Every number downstream comes from here. No synthetic prices, ever: if the
upstream read fails we raise, and the API reports "could not read" instead of
inventing a value.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass

import httpx
import pandas as pd

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
UA = "Mozilla/5.0 (compatible; AnalysisDesk/1.0)"

# interval -> how far back to request, and how long the payload stays warm
INTERVALS: dict[str, tuple[str, int]] = {
    "5m": ("5d", 60),
    "15m": ("1mo", 120),
    "1h": ("3mo", 300),
    "1d": ("2y", 600),
    "1wk": ("5y", 3600),
}


@dataclass
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float | None


@dataclass
class Series:
    symbol: str
    currency: str
    exchange: str
    interval: str
    candles: list[Candle]
    short_name: str = ""
    instrument_type: str = ""

    @property
    def last_price(self) -> float:
        return self.candles[-1].c

    def to_frame(self) -> pd.DataFrame:
        df = pd.DataFrame(
            [
                {"t": c.t, "open": c.o, "high": c.h, "low": c.l, "close": c.c, "volume": c.v}
                for c in self.candles
            ]
        )
        return df.set_index("t")


class MarketDataError(RuntimeError):
    pass


_cache: dict[tuple[str, str], tuple[float, Series]] = {}
# A symbol the watchlist does not name can still be requested, so the key space
# is unbounded. TTL alone does not cap it: entries are only dropped when they
# are asked for again, so a caller who walks distinct symbols grows this
# forever. The cap evicts the oldest entry first, which is the one least likely
# to be asked for again.
_CACHE_MAX = 256


def _cache_put(key: tuple[str, str], value: tuple[float, Series]) -> None:
    if key not in _cache and len(_cache) >= _CACHE_MAX:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = value


def cache_stats() -> dict:
    """Report the cache's size for the diagnostics surface.

    How full the cache is tells an operator whether the desk is serving a
    handful of watched symbols or being walked through invented ones -- the
    latter is the shape of the abuse the cap exists to bound.
    """
    return {"entries": len(_cache), "max": _CACHE_MAX}


async def fetch_series(symbol: str, interval: str = "1h", *, fresh: bool = False) -> Series:
    if interval not in INTERVALS:
        raise MarketDataError(f"unsupported interval: {interval}")

    # The cache wraps both venues. It used to sit below the spot branch, so spot
    # gold -- the instrument this desk exists for -- was the one series that
    # never cached, and every read paid for a fresh Kraken plus Coinbase call.
    key = (symbol.upper(), interval)
    now = time.time()
    hit = _cache.get(key)
    if hit and not fresh and now - hit[0] < INTERVALS[interval][1]:
        return hit[1]

    # Spot instruments (e.g. XAUUSD) come from a different venue entirely.
    if symbol.upper() in SPOT_SYMBOLS:
        series = await fetch_spot_series(symbol, interval)
        _cache_put(key, (now, series))
        return series

    rng = INTERVALS[interval][0]
    url = YAHOO.format(symbol=symbol)
    params = {"range": rng, "interval": interval, "includePrePost": "false"}

    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as client:
        try:
            r = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise MarketDataError(f"network error reading {symbol}: {exc}") from exc

    if r.status_code != 200:
        raise MarketDataError(f"upstream returned {r.status_code} for {symbol}")

    try:
        result = r.json()["chart"]["result"][0]
    except (KeyError, IndexError, ValueError) as exc:
        raise MarketDataError(f"unreadable payload for {symbol}") from exc

    meta = result.get("meta", {})
    stamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens, highs, lows, closes = (quote.get(k) or [] for k in ("open", "high", "low", "close"))
    vols = quote.get("volume") or [None] * len(stamps)

    candles: list[Candle] = []
    for i, ts in enumerate(stamps):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if None in (o, h, l, c):
            continue
        v = vols[i] if i < len(vols) else None
        candles.append(Candle(int(ts), float(o), float(h), float(l), float(c),
                              float(v) if v is not None else None))

    if len(candles) < 60:
        raise MarketDataError(f"not enough history for {symbol} ({len(candles)} candles)")

    series = Series(
        symbol=meta.get("symbol", symbol.upper()),
        currency=meta.get("currency", ""),
        exchange=meta.get("exchangeName", ""),
        interval=interval,
        candles=candles,
        short_name=meta.get("shortName") or meta.get("longName") or "",
        instrument_type=meta.get("instrumentType") or "",
    )
    _cache_put(key, (now, series))
    return series


# --- Spot gold (what a broker screen shows) --------------------------------
# There is no keyless XAU/USD candle feed: Yahoo has no spot gold symbol,
# LBMA/FRED are Cloudflare-walled, Dukascopy is unreachable from here, and
# Binance blocks this region. Gold-backed tokens trade as a close proxy, so we
# chart those instead and show the live broker quote beside them.
#
# Kraken is primary: it serves every timeframe natively (including weekly) with
# ~7 years of history. Coinbase is the fallback; it has no weekly granularity,
# so its daily candles are folded into ISO weeks when it is used.
KRAKEN = "https://api.kraken.com/0/public/OHLC"
COINBASE = "https://api.exchange.coinbase.com/products/{product}/candles"

SPOT_SYMBOLS = {
    "XAUUSD": {"pair": "PAXGUSD", "product": "PAXG-USD",
               "name": "Gold Spot (XAU/USD)", "metal": "XAU"},
}
# Kraken intervals are minutes and it returns [t, open, high, low, close, ...];
# Coinbase's order differs — it is [t, low, high, open, close, volume].
KRAKEN_GRAN = {"5m": 5, "15m": 15, "1h": 60, "1d": 1440, "1wk": 10080}
COINBASE_GRAN = {"5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
MIN_CANDLES = 60


async def _kraken_page(client: httpx.AsyncClient, pair: str, gran: int) -> list[Candle]:
    r = await client.get(KRAKEN, params={"pair": pair, "interval": gran})
    if r.status_code != 200:
        raise MarketDataError(f"spot source returned {r.status_code} for {pair}")
    try:
        body = r.json()
    except ValueError as exc:
        raise MarketDataError(f"unreadable spot payload for {pair}") from exc
    if body.get("error"):
        raise MarketDataError(f"spot source error for {pair}: {body['error']}")
    result = body.get("result") or {}
    rows = next((v for k, v in result.items() if k != "last"), None)
    if not rows:
        raise MarketDataError(f"spot source returned no candles for {pair}")
    return [Candle(int(t), float(o), float(h), float(l), float(c),
                   float(vol) if vol else None)
            for t, o, h, l, c, _vwap, vol, _n in rows]


async def _coinbase_page(client: httpx.AsyncClient, product: str, gran: int,
                         end: int | None = None) -> list[Candle]:
    params = {"granularity": gran}
    if end is not None:
        params["end"] = end
    r = await client.get(COINBASE.format(product=product), params=params)
    if r.status_code != 200:
        raise MarketDataError(f"spot source returned {r.status_code} for {product}")
    try:
        rows = r.json()
    except ValueError as exc:
        raise MarketDataError(f"unreadable spot payload for {product}") from exc
    if not isinstance(rows, list) or not rows:
        raise MarketDataError(f"spot source returned no candles for {product}")
    # Documented order: [time, low, high, open, close, volume].
    return [Candle(int(t), float(o), float(hi), float(lo), float(c),
                   float(vol) if vol else None)
            for t, lo, hi, o, c, vol in rows]


def _to_weekly(daily: list[Candle]) -> list[Candle]:
    """Fold daily candles into ISO weeks, for sources without a weekly step."""
    weeks: dict[tuple[int, int], list[Candle]] = {}
    for c in daily:
        iso = datetime.datetime.fromtimestamp(c.t, datetime.UTC).isocalendar()
        key = (iso[0], iso[1])
        weeks.setdefault(key, []).append(c)
    out = []
    for _, group in sorted(weeks.items(), key=lambda kv: kv[1][0].t):
        out.append(Candle(
            t=group[0].t, o=group[0].o,
            h=max(c.h for c in group), l=min(c.l for c in group),
            c=group[-1].c, v=sum(c.v or 0 for c in group) or None,
        ))
    return out


async def _spot_via_coinbase(client: httpx.AsyncClient, spec: dict,
                             interval: str) -> list[Candle]:
    weekly = interval == "1wk"
    gran = COINBASE_GRAN["1d"] if weekly else COINBASE_GRAN[interval]
    wanted = 260 if weekly else 120
    candles = await _coinbase_page(client, spec["product"], gran)
    while len(candles) < wanted:
        oldest = min(c.t for c in candles)
        try:
            older = await _coinbase_page(client, spec["product"], gran, end=oldest - gran)
        except MarketDataError:
            break  # out of history; use what we have
        if not older:
            break
        candles.extend(older)
    return _to_weekly(candles) if weekly else candles


async def fetch_spot_series(instrument: str, interval: str) -> Series:
    """Chart a spot instrument, preferring the source with the deepest history."""
    spec = SPOT_SYMBOLS.get(instrument.upper())
    if not spec:
        raise MarketDataError(f"no spot source for {instrument}")
    if interval not in KRAKEN_GRAN:
        raise MarketDataError(f"spot does not support the {interval} timeframe")

    errors: list[str] = []
    async with httpx.AsyncClient(timeout=25, headers={"User-Agent": UA}) as client:
        try:
            candles = await _kraken_page(client, spec["pair"], KRAKEN_GRAN[interval])
            source = "Kraken PAXG/USD"
        except MarketDataError as exc:
            errors.append(str(exc))
            candles = []
            source = "Coinbase PAXG-USD"
            try:
                candles = await _spot_via_coinbase(client, spec, interval)
            except MarketDataError as exc2:
                errors.append(str(exc2))

    if not candles:
        raise MarketDataError("; ".join(errors) or f"no spot data for {instrument}")

    candles.sort(key=lambda c: c.t)
    if len(candles) < MIN_CANDLES:
        raise MarketDataError(f"not enough history for {instrument} ({len(candles)} candles)")

    return Series(
        symbol=instrument.upper(),
        currency="USD",
        exchange=source,
        interval=interval,
        candles=candles,
        short_name=spec["name"],
        instrument_type="SPOT",
    )


# --- Spot metals -----------------------------------------------------------
# GC=F is the COMEX futures contract, which trades at a premium to spot. A user
# watching a broker's XAU/USD will see a different number, so we fetch the real
# spot quote and show both rather than letting one stand in for the other.
#
# Primary source is Swissquote's public BBO feed: it is a real broker book and
# matched the user's own screen to the dollar. The gold-api aggregate is the
# fallback, so a single upstream outage degrades instead of failing.
SWISSQUOTE = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{metal}/USD"
SPOT_URL = "https://api.gold-api.com/price/{metal}"
SPOT_METALS = {"XAU", "XAG"}


def _round_price(value: float) -> float:
    """Strip binary-float noise from a computed price.

    (bid + ask) / 2 yields values like 4002.2650000000003 — the true midpoint is
    4002.265, the tail is float representation. Rounding here keeps the API
    honest, since the JSON is read by more than the formatter in the UI.
    """
    return round(value, 4)


def _swissquote_mid(payload) -> float | None:
    """Pick a bid/ask pair from the book and return its midpoint."""
    if not isinstance(payload, list) or not payload:
        return None
    profiles = payload[0].get("spreadProfilePrices") or []
    for want in ("premium", "prime", "elite"):
        for p in profiles:
            if p.get("spreadProfile") == want and p.get("bid") and p.get("ask"):
                return _round_price((float(p["bid"]) + float(p["ask"])) / 2)
    for p in profiles:
        if p.get("bid") and p.get("ask"):
            return _round_price((float(p["bid"]) + float(p["ask"])) / 2)
    return None


async def fetch_spot(metal: str) -> dict:
    metal = metal.upper()
    if metal not in SPOT_METALS:
        raise MarketDataError(f"unsupported spot metal: {metal}")

    errors: list[str] = []
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as client:
        try:
            r = await client.get(SWISSQUOTE.format(metal=metal))
            if r.status_code == 200:
                mid = _swissquote_mid(r.json())
                if mid:
                    return {"metal": metal, "name": metal, "price": mid,
                            "currency": "USD", "source": "Swissquote", "bid_ask": True}
            errors.append(f"Swissquote: HTTP {r.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"Swissquote: {exc.__class__.__name__}")

        try:
            r = await client.get(SPOT_URL.format(metal=metal))
            if r.status_code == 200:
                body = r.json()
                return {"metal": metal, "name": body.get("name", metal),
                        "price": _round_price(float(body["price"])),
                        "currency": body.get("currency", "USD"),
                        "updated_at": body.get("updatedAt"), "source": "gold-api"}
            errors.append(f"gold-api: HTTP {r.status_code}")
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"gold-api: {exc.__class__.__name__}")

    raise MarketDataError("; ".join(errors))


# Which spot metal, if any, a futures symbol should be compared against.
FUTURES_TO_SPOT = {"GC=F": "XAU", "SI=F": "XAG"}
