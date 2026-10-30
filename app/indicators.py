"""Technical indicators, computed from the real OHLC frame.

Nothing here is estimated. Each function returns plain numbers derived from the
candles passed in, so every figure shown to a user can be traced back to a
formula over real prices.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .market import Series


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def rsi(s: pd.Series, period: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    # A flat stretch gives zero loss; RSI is then 100 by definition, not undefined.
    rs = gain / loss.where(loss > 0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss > 0, 100.0).astype(float)


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(s, fast) - ema(s, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger(s: pd.Series, period: int = 20, mult: float = 2.0):
    mid = s.rolling(period).mean()
    sd = s.rolling(period).std()
    return mid - mult * sd, mid, mid + mult * sd


def stochastic(df: pd.DataFrame, period: int = 14, smooth: int = 3):
    low = df["low"].rolling(period).min()
    high = df["high"].rolling(period).max()
    rng = (high - low).where((high - low) > 0, np.nan)
    k = (100 * (df["close"] - low) / rng).fillna(50.0)
    return k, k.rolling(smooth).mean()


def swing_levels(df: pd.DataFrame, lookback: int = 40, wing: int = 3) -> tuple[float, float]:
    """Nearest support below and resistance above, from actual pivot highs/lows."""
    window = df.tail(lookback)
    highs, lows = [], []
    h, l = window["high"].to_numpy(), window["low"].to_numpy()
    for i in range(wing, len(window) - wing):
        if h[i] == max(h[i - wing : i + wing + 1]):
            highs.append(float(h[i]))
        if l[i] == min(l[i - wing : i + wing + 1]):
            lows.append(float(l[i]))
    price = float(window["close"].iloc[-1])
    support = max([x for x in lows if x < price], default=float(window["low"].min()))
    resistance = min([x for x in highs if x > price], default=float(window["high"].max()))
    return support, resistance


def _clean(v: float, digits: int = 5) -> float:
    """Round, but never let a NaN reach a client dressed up as a number."""
    f = float(v)
    return round(f, digits) if np.isfinite(f) else float("nan")


def snapshot(series: Series) -> dict:
    """Compute the full indicator board for a series."""
    df = series.to_frame()
    close = df["close"]

    r = rsi(close)
    macd_line, macd_sig, macd_hist = macd(close)
    bb_low, bb_mid, bb_up = bollinger(close)
    stoch_k, stoch_d = stochastic(df)
    a = atr(df)
    ema20, ema50, ema200 = ema(close, 20), ema(close, 50), ema(close, 200)
    support, resistance = swing_levels(df)

    price = float(close.iloc[-1])
    band = float(bb_up.iloc[-1] - bb_low.iloc[-1])
    bb_pos = float((price - bb_low.iloc[-1]) / band) if band else 0.5
    # Warm-up windows leave leading NaNs; take the newest finite value of each line.
    warm = {"rsi": r, "macd": macd_line, "macd_signal": macd_sig, "macd_hist": macd_hist,
            "ema20": ema20, "ema50": ema50, "ema200": ema200,
            "bb_upper": bb_up, "bb_mid": bb_mid, "bb_lower": bb_low,
            "stoch_k": stoch_k, "stoch_d": stoch_d, "atr": a}

    def tail(name: str, digits: int = 5) -> float:
        s = warm[name].dropna()
        return _clean(s.iloc[-1], digits) if len(s) else float("nan")

    return {
        # price/prev_close come straight off the frame, so they carry the raw
        # float tail (1.1185681819915771). Every other figure here already goes
        # through _clean; these must too, or the API ships noise the UI only
        # hides by formatting it.
        "price": _clean(price),
        "prev_close": _clean(float(close.iloc[-2])),
        "change_pct": _clean((price / close.iloc[-2] - 1) * 100, 4),
        "rsi": tail("rsi", 2),
        "macd": tail("macd"),
        "macd_signal": tail("macd_signal"),
        "macd_hist": tail("macd_hist"),
        "ema20": tail("ema20"),
        "ema50": tail("ema50"),
        "ema200": tail("ema200"),
        "bb_upper": tail("bb_upper"),
        "bb_mid": tail("bb_mid"),
        "bb_lower": tail("bb_lower"),
        "bb_position": round(bb_pos, 3),
        "stoch_k": tail("stoch_k", 2),
        "stoch_d": tail("stoch_d", 2),
        "atr": tail("atr"),
        "atr_pct": _clean(float(a.iloc[-1]) / price * 100, 3) if price else float("nan"),
        "support": round(support, 5),
        "resistance": round(resistance, 5),
        "candles": len(df),
    }
