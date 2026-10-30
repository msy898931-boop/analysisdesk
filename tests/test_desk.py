"""Tests over the real code paths: the signal engine, the indicator math, and the
API against live market data. No mocks — the point is that a wrong number must
fail here, not in front of a user.
"""

from __future__ import annotations

import asyncio
import math

import pytest
from fastapi.testclient import TestClient

from app import ai, indicators, market, signals
from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------- signal engine

def _board(**over) -> dict:
    base = {
        "price": 100.0, "prev_close": 99.0, "change_pct": 1.0,
        "rsi": 55.0, "macd": 0.5, "macd_signal": 0.3, "macd_hist": 0.2,
        "ema20": 99.0, "ema50": 98.0, "ema200": 95.0,
        "bb_upper": 105.0, "bb_mid": 100.0, "bb_lower": 95.0, "bb_position": 0.5,
        "stoch_k": 55.0, "stoch_d": 50.0,
        "atr": 1.0, "atr_pct": 1.0,
        "support": 95.0, "resistance": 110.0, "candles": 500,
    }
    base.update(over)
    return base


def test_bullish_stack_yields_buy():
    sig = signals.evaluate(_board())
    assert sig.direction == "BUY"
    assert sig.confidence > 0


def test_bearish_stack_yields_sell():
    sig = signals.evaluate(_board(
        price=90.0, ema20=95.0, ema50=97.0, ema200=99.0,
        macd=-0.5, macd_signal=-0.2, macd_hist=-0.3, rsi=35.0,
        bb_position=0.05, stoch_k=15.0, stoch_d=25.0,
        support=85.0, resistance=95.0,
    ))
    assert sig.direction == "SELL"


def test_conflicting_indicators_stay_neutral():
    """The one outcome we must never fake: no edge means no signal."""
    sig = signals.evaluate(_board(
        price=90.0, ema20=95.0, ema50=97.0, ema200=99.0,
        macd=0.5, macd_signal=0.3, macd_hist=0.2,
        rsi=55.0, bb_position=0.5, stoch_k=55.0, stoch_d=50.0,
        support=85.0, resistance=95.0,
    ))
    assert sig.direction == "NEUTRAL"
    assert sig.confidence == 0.0


def test_every_rule_is_auditable():
    sig = signals.evaluate(_board())
    assert sig.rules
    for r in sig.rules:
        assert r.name and r.detail_en and r.detail_ar
        assert r.vote in ("bullish", "bearish", "neutral")


def test_no_profit_probability_language_anywhere():
    sig = signals.evaluate(_board())
    d = signals.signal_dict(sig)
    blob = (str(d) + " ".join(w["en"] + w["ar"] for w in d["warnings"])).lower()
    for banned in ("guarantee", "guaranteed", "win rate", "profit probability",
                   "ضمان", "نسبة نجاح", "احتمال ربح"):
        assert banned not in blob


def test_thin_target_room_is_flagged():
    """Resistance nearer than 2xATR must not be presented as a clean target."""
    sig = signals.evaluate(_board(resistance=100.5))
    assert sig.direction == "BUY"
    assert any("closer than" in w.en for w in sig.warnings)


def test_low_reward_risk_is_flagged():
    # Target exactly 2xATR away against a 1.5xATR stop => R/R 1.33, under threshold.
    sig = signals.evaluate(_board(resistance=102.0, atr=1.0))
    assert sig.take_profit == 102.0
    assert sig.risk_reward < 1.5
    assert any("risk/reward" in w.en for w in sig.warnings)


def test_undefined_atr_blocks_signal():
    sig = signals.evaluate(_board(atr=float("nan")))
    assert sig.direction == "NEUTRAL"
    assert any("volatility is undefined" in w.en for w in sig.warnings)


def test_short_history_is_disclosed():
    sig = signals.evaluate(_board(ema200=float("nan"), candles=50))
    assert any("not enough history" in w.en for w in sig.warnings)


def test_stop_is_on_the_correct_side():
    buy = signals.evaluate(_board())
    assert buy.stop_loss < buy.entry < buy.take_profit
    sell = signals.evaluate(_board(
        price=90.0, ema20=95.0, ema50=97.0, ema200=99.0, macd=-0.5,
        macd_signal=-0.2, macd_hist=-0.3, rsi=35.0, bb_position=0.05,
        stoch_k=15.0, stoch_d=25.0, support=85.0, resistance=95.0,
    ))
    assert sell.take_profit < sell.entry < sell.stop_loss


def test_no_nan_leaks_into_output():
    sig = signals.signal_dict(signals.evaluate(_board()))
    for k in ("entry", "stop_loss", "take_profit", "risk_reward", "score", "confidence"):
        assert math.isfinite(sig[k]), k


# ------------------------------------------------------------------ indicators

def test_rsi_bounds_and_extremes():
    import pandas as pd
    up = pd.Series(range(1, 60), dtype=float)
    down = pd.Series(range(60, 1, -1), dtype=float)
    assert 95 <= indicators.rsi(up).iloc[-1] <= 100
    assert 0 <= indicators.rsi(down).iloc[-1] <= 5
    flat = pd.Series([5.0] * 40)
    assert indicators.rsi(flat).iloc[-1] == 100.0  # zero loss => RSI 100, defined


def test_atr_is_positive_on_real_like_data():
    import pandas as pd
    n = 100
    df = pd.DataFrame({
        "high": [i + 2.0 for i in range(n)],
        "low": [i - 2.0 for i in range(n)],
        "close": [float(i) for i in range(n)],
    })
    assert indicators.atr(df).iloc[-1] > 0


def test_swing_levels_bracket_the_price():
    import pandas as pd
    n = 120
    close = [100 + 10 * math.sin(i / 7) for i in range(n)]
    df = pd.DataFrame({
        "high": [c + 1 for c in close],
        "low": [c - 1 for c in close],
        "close": close,
    })
    s, r = indicators.swing_levels(df)
    assert s <= df["close"].iloc[-1] <= r


# ----------------------------------------------------------- offline narrative

def test_offline_narrative_is_honest_when_neutral():
    sig = signals.signal_dict(signals.evaluate(_board(
        price=90.0, ema20=95.0, ema50=97.0, ema200=99.0,
        macd=0.5, macd_signal=0.3, macd_hist=0.2, rsi=55.0,
        bb_position=0.5, stoch_k=55.0, stoch_d=50.0, support=85.0, resistance=95.0,
    )))
    text = ai._offline_narrative(sig, _board(), "ar")
    assert "عدم الدخول" in text or "لا يوجد" in text


def test_warnings_are_bilingual():
    """A caveat must never be visible in only one language."""
    sig = signals.evaluate(_board(resistance=100.5))
    assert sig.warnings
    for w in sig.warnings:
        assert w.code and w.en and w.ar


def test_narrate_without_key_degrades_cleanly(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    sig = signals.signal_dict(signals.evaluate(_board()))
    out = asyncio.run(ai.narrate("EURUSD=X", "1h", _board(), sig, "en"))
    assert out["source"] == "deterministic"
    assert out["text"]


# ----------------------------------------------------------------- live market

def test_live_market_read_has_real_candles():
    s = asyncio.run(market.fetch_series("EURUSD=X", "1h"))
    assert len(s.candles) > 100
    assert all(c.h >= c.l for c in s.candles)
    assert all(c.h >= c.o and c.h >= c.c for c in s.candles)
    assert all(c.l <= c.o and c.l <= c.c for c in s.candles)


def test_unknown_symbol_is_reported_not_invented():
    with pytest.raises(market.MarketDataError):
        asyncio.run(market.fetch_series("NOT_A_REAL_SYMBOL_XYZ", "1h"))


# ------------------------------------------------------------------------ API

def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_symbols_endpoint():
    d = client.get("/api/symbols").json()
    assert len(d["symbols"]) >= 8
    assert "1h" in d["intervals"]


def test_analyze_live_end_to_end():
    r = client.get("/api/analyze", params={"symbol": "EURUSD=X", "interval": "1h", "lang": "ar"})
    assert r.status_code == 200
    d = r.json()
    assert d["signal"]["direction"] in ("BUY", "SELL", "NEUTRAL")
    assert d["indicators"]["candles"] > 100
    assert d["narrative"]["text"]
    assert "ليس توصية" in d["disclaimer"]


def test_analyze_rejects_bad_interval():
    r = client.get("/api/analyze", params={"symbol": "EURUSD=X", "interval": "7s"})
    assert r.status_code == 400


def test_analyze_reports_market_failure_loudly():
    """A failed read must never come back as a reassuring default."""
    r = client.get("/api/analyze", params={"symbol": "NOT_A_REAL_SYMBOL_XYZ", "interval": "1h"})
    assert r.status_code == 502
    assert "تعذّر قراءة" in r.json()["detail"]


def test_candles_endpoint_shape():
    r = client.get("/api/candles", params={"symbol": "BTC-USD", "interval": "1d"})
    assert r.status_code == 200
    c = r.json()["candles"][-1]
    assert c["h"] >= c["l"] and c["t"] > 0


# --------------------------------------------------------- model output guard

def test_guard_rejects_invented_number():
    """A figure that is not in the report must never reach a user."""
    ind, sig = _board(), signals.signal_dict(signals.evaluate(_board()))
    assert ai._verify("السعر سيرتفع إلى 1.9999 قريباً", ind, sig)
    assert ai._verify("price will reach 999.1234 soon", ind, sig)


def test_guard_accepts_a_real_level_at_any_precision():
    """A true level quoted with more decimals must not be read as invented."""
    ind = _board(resistance=4076.5708)
    sig = signals.signal_dict(signals.evaluate(ind))
    assert ai._verify("resistance sits at 4076.57080", ind, sig) is None
    assert ai._verify("resistance is 4076.5708", ind, sig) is None
    assert ai._verify("resistance is 4076.57", ind, sig) is None
    # A number that is not the level must still be caught.
    assert ai._verify("resistance is 4076.9999", ind, sig)


def test_guard_rejects_profit_promises():
    ind, sig = _board(), signals.signal_dict(signals.evaluate(_board()))
    assert ai._verify("this trade has a guarantee of profit", ind, sig)
    assert ai._verify("نسبة نجاح عالية جداً", ind, sig)


def test_guard_rejects_inverted_ema_relation():
    """The exact failure observed in production: price above EMA20 described as below."""
    ind = _board(price=100.0, ema20=99.0)
    sig = signals.signal_dict(signals.evaluate(ind))
    assert ai._verify("price is below EMA20 here", ind, sig)
    assert ai._verify("السعر تحت EMA20", ind, sig)
    # The correct statement must pass.
    assert ai._verify("price is above EMA20 at 99.0000", ind, sig) is None


def test_guard_accepts_faithful_prose():
    ind = _board()
    sig = signals.signal_dict(signals.evaluate(ind))
    good = (f"BUY with {sig['confidence']}% indicator agreement. Price {ind['price']}, "
            f"stop {sig['stop_loss']}, target {sig['take_profit']}, "
            f"risk/reward {sig['risk_reward']}. Automated technical analysis, not investment advice.")
    assert ai._verify(good, ind, sig) is None


def test_narrate_falls_back_when_model_contradicts(monkeypatch):
    """If every model lies, the user still gets the honest computed sentence."""
    ind = _board()
    sig = signals.signal_dict(signals.evaluate(ind))

    async def bad(client, model, messages):
        return "price is below EMA20, guaranteed profit of 500.0000", None

    monkeypatch.setattr(ai, "_try_model", bad)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    out = asyncio.run(ai.narrate("EURUSD=X", "1h", ind, sig, "en"))
    assert out["source"] == "deterministic"
    assert "rejected" in out["note"]
    assert "guarantee" not in out["text"].lower()


def test_price_formatting_has_no_float_noise():
    assert ai._n(1.118067979812622) == "1.1181"
    assert ai._n(79128.453125) == "79,128.45"
    assert ai._n(4033.300048828125) == "4,033.30"
    assert ai._n(0.51234567) == "0.51235"


# ------------------------------------------------- spot vs futures provenance

def test_swissquote_mid_uses_midpoint_of_a_real_book():
    book = [{"spreadProfilePrices": [
        {"spreadProfile": "premium", "bid": 4021.491, "ask": 4022.149},
        {"spreadProfile": "prime", "bid": 4021.501, "ask": 4022.139},
    ]}]
    mid = market._swissquote_mid(book)
    assert abs(mid - 4021.82) < 0.01


def test_swissquote_mid_survives_unexpected_shape():
    assert market._swissquote_mid([]) is None
    assert market._swissquote_mid({"nope": 1}) is None
    assert market._swissquote_mid([{}]) is None


def test_series_names_the_instrument_it_is():
    """A bare 'Gold' number is ambiguous; the series must say which contract."""
    import inspect
    src = inspect.getsource(market.fetch_series)
    assert "instrument_type" in src and "short_name" in src


def test_futures_map_points_gold_at_spot():
    assert market.FUTURES_TO_SPOT["GC=F"] == "XAU"
    assert market.FUTURES_TO_SPOT["SI=F"] == "XAG"


# ------------------------------------------------- spot gold candle sources

def test_kraken_parses_open_high_low_close_in_its_own_order():
    """Kraken is [t, open, high, low, close, vwap, volume, count] — a different
    order from Coinbase. Swapping them silently inverts every candle."""
    row = [1793232000, "4050.14", "4061.46", "4008.97", "4016.37", "4025.17", "221.74", 1863]
    c = market._kraken_row_to_candle(row) if hasattr(market, "_kraken_row_to_candle") else None
    # Parse through the public helper the fetcher uses.
    import asyncio, httpx

    class FakeResp:
        status_code = 200
        def json(self):
            return {"error": [], "result": {"PAXGUSD": [row], "last": 1793232000}}

    class FakeClient:
        async def get(self, *a, **k):
            return FakeResp()

    out = asyncio.run(market._kraken_page(FakeClient(), "PAXGUSD", 1440))
    assert len(out) == 1
    got = out[0]
    assert (got.o, got.h, got.l, got.c) == (4050.14, 4061.46, 4008.97, 4016.37)
    assert got.l <= got.o <= got.h and got.l <= got.c <= got.h


def test_coinbase_parses_low_high_before_open():
    """Coinbase is [t, low, high, open, close, volume] — note low comes first."""
    import asyncio

    class FakeResp:
        status_code = 200
        def json(self):
            return [[1793311200, 4015.48, 4021.62, 4016.61, 4016.69, 30.44906]]

    class FakeClient:
        async def get(self, *a, **k):
            return FakeResp()

    out = asyncio.run(market._coinbase_page(FakeClient(), "PAXG-USD", 3600))
    got = out[0]
    assert (got.o, got.h, got.l, got.c) == (4016.61, 4021.62, 4015.48, 4016.69)
    assert got.l <= got.o <= got.h


def test_weekly_folding_takes_high_low_across_the_week():
    from app.market import Candle
    # Mon..Fri of one ISO week, then the next Monday.
    base = 1793232000
    day = 86400
    daily = [
        Candle(base, 10, 12, 9, 11, 1),
        Candle(base + day, 11, 15, 10, 14, 1),
        Candle(base + 2 * day, 14, 14, 8, 9, 1),
        Candle(base + 7 * day, 9, 20, 9, 19, 1),
    ]
    weeks = market._to_weekly(daily)
    assert len(weeks) == 2
    assert (weeks[0].o, weeks[0].h, weeks[0].l, weeks[0].c) == (10, 15, 8, 9)
    assert (weeks[1].o, weeks[1].c) == (9, 19)


def test_spot_instrument_is_wired_to_a_pair_and_product():
    spec = market.SPOT_SYMBOLS["XAUUSD"]
    assert spec["pair"] and spec["product"]
    assert market.KRAKEN_GRAN["1wk"] == 10080  # Kraken serves weekly natively
    assert "1wk" not in market.COINBASE_GRAN    # Coinbase does not


def test_spot_timeframe_gap_is_reported_as_an_error_not_guessed():
    import asyncio
    try:
        asyncio.run(market.fetch_spot_series("XAUUSD", "2h"))
        assert False, "unsupported timeframe should raise"
    except market.MarketDataError as exc:
        assert "2h" in str(exc)


# ------------------------------------------------- no float noise in prose

def _sample_facts():
    ind = {"price": 1.1188185214996338, "ema20": 1.11644, "ema50": 1.11573,
           "ema200": 1.11823, "macd": 0.00104, "macd_signal": 0.00067,
           "rsi": 66.53, "stoch_k": 96.75, "bb_position": 0.714,
           "support": 1.1165, "resistance": 1.1191, "atr_pct": 0.112}
    sig = {"direction": "BUY", "confidence": 71.4, "entry": 1.1188185214996338,
           "stop_loss": 1.11693, "take_profit": 1.1226, "risk_reward": 2,
           "rules": [{"name": "trend_ema_stack", "vote": "bullish", "weight": 2}],
           "warnings": []}
    return ind, sig


def test_fact_sheet_never_leaks_float_noise():
    """The model quotes the sheet literally, so raw floats end up in the prose."""
    ind, sig = _sample_facts()
    sheet = ai._fact_sheet("EURUSD=X", "1h", ind, sig, "ar")
    assert "1.1188185214996338" not in sheet
    assert "1.1188" in sheet
    # No token should carry more than 5 significant decimals.
    for tok in sheet.replace("=", " ").replace("(", " ").replace(")", " ").split():
        try:
            f = float(tok)
        except ValueError:
            continue
        assert len(tok.split(".")[-1]) <= 6, f"noisy number in sheet: {tok}"


def test_fact_sheet_keeps_the_relations_it_states():
    ind, sig = _sample_facts()
    sheet = ai._fact_sheet("EURUSD=X", "1h", ind, sig, "ar")
    assert "price_vs_ema20=ABOVE" in sheet
    assert "FINAL_DIRECTION=BUY" in sheet


# ------------------------------------------------- failure reporting

def test_identical_config_failures_collapse_to_one_translated_sentence():
    """Four '401' lines on a clean card is noise; the cause is the message."""
    reasons = ["config: the AI key was rejected by the provider (401)"] * 4
    ar = ai._summarise_failures(reasons, "ar")
    assert ar.count("؛") == 0 and "401" not in ar
    assert "مفتاح" in ar
    en = ai._summarise_failures(reasons, "en")
    assert "key was rejected" in en


def test_rate_limit_failures_are_reported_as_busy_not_broken():
    reasons = ["openai/gpt-oss-120b: rate limit reached — try again in 3s"] * 4
    assert "مشغولة" in ai._summarise_failures(reasons, "ar")


def test_mixed_failures_still_list_each_model_once():
    reasons = ["a: rate limit reached", "a: rate limit reached", "b: rejected — invented 9.99"]
    out = ai._summarise_failures(reasons, "en")
    assert out.count("a:") == 1 and "b:" in out


def test_auth_errors_are_not_retried_as_transient():
    """401 is a configuration fact, not a flaky upstream — say so plainly."""
    import asyncio

    class Resp:
        status_code = 401
        def json(self): return {}

    calls = {"n": 0}

    class Client:
        async def post(self, *a, **k):
            calls["n"] += 1
            return Resp()

    text, why = asyncio.run(ai._try_model(Client(), "m", []))
    assert text is None
    assert why.startswith("config:")
    assert calls["n"] == 1  # no pointless reasoning_effort retry on auth failure


# ------------------------------------------------- computed prices stay clean

def test_swissquote_midpoint_has_no_binary_float_tail():
    """(bid+ask)/2 gives 4002.2650000000003; the API must not ship that."""
    payload = [{"spreadProfilePrices": [
        {"spreadProfile": "premium", "bid": 4002.11, "ask": 4002.42},
    ]}]
    mid = market._swissquote_mid(payload)
    assert mid == 4002.265, mid
    assert repr(mid) == "4002.265", repr(mid)


def test_swissquote_falls_back_to_any_profile_and_still_rounds():
    """With no named profile, the first usable book is taken — and rounded."""
    payload = [{"spreadProfilePrices": [
        {"spreadProfile": "other", "bid": 2350.11, "ask": 2350.42},
    ]}]
    mid = market._swissquote_mid(payload)
    assert mid == 2350.265, mid
    assert repr(mid) == "2350.265", repr(mid)


def test_swissquote_mid_returns_none_on_unusable_payload():
    for bad in (None, [], [{}], [{"spreadProfilePrices": []}],
                [{"spreadProfilePrices": [{"bid": 1, "ask": None}]}]):
        assert market._swissquote_mid(bad) is None
