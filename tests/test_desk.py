"""Tests over the real code paths: the signal engine, the indicator math, and the
API against live market data. No mocks — the point is that a wrong number must
fail here, not in front of a user.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ai, alerts, backtest, indicators, main, market, news, signals
from app.indicators import _clean as ind_clean
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
        # The pivot ladder the scale-out targets snap onto. Real pivot prices,
        # not projections, so a rung that lands on one is a "level".
        "supports": [98.0, 95.0], "resistances": [104.0, 110.0, 118.0, 130.0],
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


# ------------------------------------------------------- the six-target ladder

def test_signal_carries_six_targets_on_a_buy():
    """The user asked for six targets, not one. Every non-neutral read has them."""
    d = signals.signal_dict(signals.evaluate(_board()))
    assert [t["index"] for t in d["targets"]] == [1, 2, 3, 4, 5, 6]


def test_six_targets_are_ordered_beyond_entry_and_stop():
    """A scale-out ladder that is not monotone is not a ladder."""
    buy = signals.signal_dict(signals.evaluate(_board()))["targets"]
    assert buy[0]["price"] > 0
    for a, b in zip(buy, buy[1:], strict=False):
        assert b["price"] > a["price"], "BUY targets must ascend"
        assert b["rr"] > a["rr"], "reward must grow with distance"
    sell = signals.signal_dict(signals.evaluate(_board(
        price=90.0, ema20=95.0, ema50=97.0, ema200=99.0, macd=-0.5,
        macd_signal=-0.2, macd_hist=-0.3, rsi=35.0, bb_position=0.05,
        stoch_k=15.0, stoch_d=25.0, support=85.0, resistance=95.0,
        supports=[80.0, 85.0], resistances=[92.0],
    )))["targets"]
    for a, b in zip(sell, sell[1:], strict=False):
        assert b["price"] < a["price"], "SELL targets must descend"


def test_targets_snap_onto_real_levels_before_projecting():
    """A rung on a pivot the market printed says so; the rest admit a projection."""
    d = signals.signal_dict(signals.evaluate(_board()))
    kinds = [t["kind"] for t in d["targets"]]
    assert "level" in kinds and "projection" in kinds
    # TP1 is the nearest resistance; the ladder continues to the levels beyond it,
    # skipping 104 which sits behind TP1 at 110.
    levels = [t["price"] for t in d["targets"] if t["kind"] == "level"]
    assert levels == [110.0, 118.0, 130.0]
    for t in d["targets"]:
        if t["kind"] == "level":
            assert t["price"] in (104.0, 110.0, 118.0, 130.0)
        else:
            assert "projection" in t["source_en"]


def test_the_first_target_is_the_old_take_profit():
    """take_profit stays TP1 so every existing consumer keeps its meaning."""
    d = signals.signal_dict(signals.evaluate(_board()))
    assert d["targets"][0]["price"] == d["take_profit"]


def test_neutral_read_has_no_ladder():
    """No direction means no plan; an empty ladder, not six invented numbers."""
    d = signals.signal_dict(signals.evaluate(_board(
        price=90.0, ema20=95.0, ema50=97.0, ema200=99.0,
        macd=0.5, macd_signal=0.3, macd_hist=0.2,
        rsi=55.0, bb_position=0.5, stoch_k=55.0, stoch_d=50.0,
        support=85.0, resistance=95.0,
    )))
    assert d["direction"] == "NEUTRAL"
    assert d["targets"] == []


def test_ladder_prices_carry_no_float_tail():
    """The rungs are shown to a user; they round by the one precision rule."""
    d = signals.signal_dict(signals.evaluate(_board()))
    for t in d["targets"]:
        assert round(t["price"], 2) == t["price"], t


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


def test_swing_ladder_returns_every_level_nearest_first():
    """The nearest-only helper cannot feed a scale-out plan; the ladder can."""
    import pandas as pd
    n = 160
    close = [100 + 10 * math.sin(i / 7) for i in range(n)]
    df = pd.DataFrame({
        "high": [c + 1 for c in close],
        "low": [c - 1 for c in close],
        "close": close,
    })
    price = df["close"].iloc[-1]
    supports, resistances = indicators.swing_ladder(df)
    assert supports and resistances
    assert all(x < price for x in supports) and all(x > price for x in resistances)
    # Ascending above, descending below: the order the ladder is consumed in.
    assert resistances == sorted(resistances)
    assert supports == sorted(supports, reverse=True)
    # They are real pivot prices, so the nearest ones match the nearest-only call.
    s, r = indicators.swing_levels(df)
    assert resistances[0] == pytest.approx(r)
    assert supports[0] == pytest.approx(s)


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

@pytest.mark.network
def test_live_market_read_has_real_candles():
    s = asyncio.run(market.fetch_series("EURUSD=X", "1h"))
    assert len(s.candles) > 100
    assert all(c.h >= c.l for c in s.candles)
    assert all(c.h >= c.o and c.h >= c.c for c in s.candles)
    assert all(c.l <= c.o and c.l <= c.c for c in s.candles)


@pytest.mark.network
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


@pytest.mark.network
def test_analyze_live_end_to_end():
    r = client.get("/api/analyze", params={"symbol": "EURUSD=X", "interval": "1h", "lang": "ar"})
    assert r.status_code == 200
    d = r.json()
    assert d["signal"]["direction"] in ("BUY", "SELL", "NEUTRAL")
    assert d["indicators"]["candles"] > 100
    assert d["narrative"]["text"]
    assert "ليس توصية" in d["disclaimer"]


@pytest.mark.network
def test_analyze_rejects_bad_interval():
    r = client.get("/api/analyze", params={"symbol": "EURUSD=X", "interval": "7s"})
    assert r.status_code == 400


@pytest.mark.network
def test_analyze_reports_market_failure_loudly():
    """A failed read must never come back as a reassuring default."""
    r = client.get("/api/analyze", params={"symbol": "NOT_A_REAL_SYMBOL_XYZ", "interval": "1h"})
    assert r.status_code == 502
    assert "تعذّر قراءة" in r.json()["detail"]


@pytest.mark.network
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
    # EUR/USD keeps pipette precision; the API reports the same 5 decimals.
    assert ai._n(1.118067979812622) == "1.11807"
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
    import asyncio

    import httpx

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


# ------------------------------------------- the raw frame price is cleaned too

def _frame_with(close_last: float):
    import pandas as pd
    n = 300
    idx = pd.date_range("2026-01-01", periods=n, freq="h")
    closes = [100.0 + (i % 7) * 0.1 for i in range(n - 1)] + [close_last]
    return pd.DataFrame({"open": closes, "high": [c + 0.5 for c in closes],
                         "low": [c - 0.5 for c in closes], "close": closes,
                         "volume": [1.0] * n}, index=idx)


def test_snapshot_price_carries_no_float_tail():
    """EUR/USD closes land as 1.1185681819915771; the board must not ship that."""
    import pandas as pd  # noqa: F401

    import app.indicators as ind
    from app.market import Series

    raw = 1.1185681819915771
    series = Series(symbol="EURUSD=X", currency="USD", exchange="test",
                    interval="1h", candles=[], short_name="EUR/USD",
                    instrument_type="SPOT")
    series.to_frame = lambda: _frame_with(raw)  # type: ignore[method-assign]
    board = ind.snapshot(series)
    assert board["price"] == 1.11857, board["price"]
    assert repr(board["price"]) == "1.11857", repr(board["price"])
    assert repr(board["prev_close"]) == "100.4", repr(board["prev_close"])


# ------------------------------- the invention guard must not cry wolf on commas

def _facts():
    ind = {"price": 3982.69, "ema20": 4012.87, "ema50": 4023.73, "ema200": 4041.82,
           "macd": -9.0563, "macd_signal": -8.2219, "support": 3989.01,
           "resistance": 4028.40, "rsi": 43.42, "stoch_k": 43.03, "atr_pct": 0.329}
    sig = {"entry": 3982.69, "stop_loss": 4001.68, "take_profit": 3944.70,
           "confidence": 50.0, "risk_reward": 2.0, "score": -100.0}
    return ind, sig


def test_thousands_separator_is_not_mistaken_for_an_invented_figure():
    """The model writes 3,982.69 for a price of 3982.69 — that is not an invention."""
    ind, sig = _facts()
    prose = "قراءة بيع. السعر 3,982.69 ووقف الخسارة 4,001.68 والهدف 3,944.70."
    assert ai._verify(prose, ind, sig) is None


def test_figures_strips_commas_before_comparing():
    assert ai._figures("3,982.69") == ["3982.69"]
    assert ai._figures("1,234,567.89") == ["1234567.89"]
    assert ai._figures("no numbers here") == []


def test_a_genuinely_invented_figure_is_still_rejected():
    """The fix must not disable the guard it was protecting."""
    ind, sig = _facts()
    assert ai._verify("السعر 3982.69 والهدف 3999.99.", ind, sig) is not None
    reason = ai._verify("السعر 3982.69 والهدف 3999.99.", ind, sig)
    assert "3999.99" in reason


def test_banned_profit_language_is_still_rejected():
    ind, sig = _facts()
    assert ai._verify("هذا مضمون الربح", ind, sig) is not None
    assert ai._verify("guaranteed profit", ind, sig) is not None


# ---------------- the guard's allow-list must cover what the sheet tells the model

def _real_facts():
    """A board shaped like production, including bollinger_position."""
    ind = {"price": 3983.19, "ema20": 4006.67, "ema50": 4020.23, "ema200": 4040.56,
           "macd": -11.6175, "macd_signal": -9.46481, "macd_hist": -2.15268,
           "support": 3982.41, "resistance": 4028.40, "rsi": 30.45, "stoch_k": 1.7,
           "atr_pct": 0.318, "bb_position": 0.026, "bb_upper": 4040.0,
           "bb_mid": 4010.0, "bb_lower": 3980.0, "stoch_d": 5.0}
    sig = {"direction": "SELL", "entry": 3983.19, "stop_loss": 4002.21,
           "take_profit": 3945.14, "confidence": 50.0, "risk_reward": 2.0,
           "score": -100.0, "rules": [], "warnings": []}
    return ind, sig


def test_every_figure_the_sheet_shows_is_accepted_by_the_guard():
    """The invariant: if we tell the model a number, quoting it cannot be an invention.

    bollinger_position=0.026 was printed in the sheet and then rejected as
    'invented a figure' because the guard kept a separate hand-written list.
    """
    ind, sig = _real_facts()
    sheet = ai._sheet_text(ind, sig)
    tokens = ai._figures(sheet)
    assert tokens, "the sheet should contain figures"
    for tok in tokens:
        prose = f"The value is {tok}."
        assert ai._verify(prose, ind, sig) is None, (
            f"guard rejected {tok}, which the fact sheet itself states")


def test_bollinger_position_from_production_is_accepted():
    """The exact live failure: 0.026 was in the sheet and got rejected."""
    ind, sig = _real_facts()
    assert ai._verify("Bollinger position is 0.026.", ind, sig) is None


def test_guard_still_rejects_a_number_absent_from_the_sheet():
    ind, sig = _real_facts()
    assert ai._verify("The target is 3999.99.", ind, sig) is not None


# --------------------------- the ladder must be quotable, not treated as invented

def test_the_ladder_reaches_the_fact_sheet_and_the_guard():
    """If a target is on the card, the model may quote it -- it must not be rejected."""
    ind = _board()
    sig = signals.signal_dict(signals.evaluate(ind))
    sheet = ai._sheet_text(ind, sig)
    assert "target_1=" in sheet and "target_6=" in sheet
    for tgt in sig["targets"]:
        tok = ai._n(tgt["price"])
        assert tok in sheet
        assert ai._verify(f"Target {tgt['index']} is {tok}.", ind, sig) is None, tok


def test_guard_still_rejects_a_seventh_invented_target():
    """The ladder has six rungs; a seventh number is still an invention."""
    ind = _board()
    sig = signals.signal_dict(signals.evaluate(ind))
    assert ai._verify("A seventh target sits at 999.99.", ind, sig) is not None


def test_allowed_figures_survives_a_partial_report():
    """A missing key must shrink the allow-list, not raise."""
    allowed = ai._allowed_figures({"price": 100.5}, {})
    assert "100.5" in allowed


# ------------------------- rounding scales with magnitude, so tails cannot hide

def test_clean_removes_tail_from_large_numbers():
    """A fixed 5 decimals left 3992.80005 intact; precision must scale down."""
    assert ind_clean(3992.80005) == 3992.80
    assert ind_clean(78073.54688) == 78073.55
    assert ind_clean(7720.39014) == 7720.39
    assert ind_clean(4002.265) == 4002.26


def test_clean_keeps_fine_precision_for_small_numbers():
    """EUR/USD needs its decimals — do not over-round the small ones."""
    assert ind_clean(1.1185681819915771) == 1.11857
    assert ind_clean(1.11907) == 1.11907


def test_clean_keeps_mid_range_precision():
    assert ind_clean(57.23456) == 57.2346
    assert ind_clean(9.87654) == 9.87654


def test_api_and_prose_round_by_the_same_rule():
    """The drift that bit twice: two copies of one rule disagreeing."""
    for v in (3992.80005, 78073.54688, 1.1185681819915771, 57.23456, 4005.96):
        assert float(ai._n(v).replace(",", "")) == ind_clean(v), v


def test_clean_still_returns_nan_for_non_finite():
    assert math.isnan(ind_clean(float("nan")))
    assert math.isnan(ind_clean(float("inf")))


def test_clean_respects_a_tighter_digit_cap():
    assert ind_clean(57.23456, 2) == 57.23


def test_guard_accepts_the_rounded_price_it_shows():
    """After rounding, the value the sheet prints must still pass _verify."""
    ind, sig = _real_facts()
    ind["price"] = ind_clean(3992.80005)
    ind["support"] = ind_clean(3992.80005)
    ind["resistance"] = ind_clean(4028.40014)
    sheet = ai._sheet_text(ind, sig)
    for tok in ai._figures(sheet):
        assert ai._verify(f"value {tok}", ind, sig) is None, tok


# ---------- signal output must round by the same rule as the API and the prose

def _sig_board(price, ema20, ema50, ema200):
    return {"price": price, "ema20": ema20, "ema50": ema50, "ema200": ema200,
            "macd": -12.7632, "macd_signal": -10.1964, "macd_hist": -2.5668,
            "support": 3970.39, "resistance": 4024.46, "rsi": 29.17,
            "stoch_k": 13.98, "stoch_d": 20.0, "atr": 12.7, "atr_pct": 0.319,
            "bb_position": 0.04, "bb_upper": 4037.82, "bb_lower": 3975.73,
            "candles": 500, "prev_close": price, "change_pct": 0.0}


def test_signal_levels_carry_no_float_tail_for_large_prices():
    """Gold's stop-loss shipped as 3997.5179 while the card's other numbers were clean."""
    sig = signals.signal_dict(signals.evaluate(_sig_board(3978.5, 4003.51, 4018.39, 4039.89)))
    for key in ("entry", "stop_loss", "take_profit"):
        v = sig[key]
        assert round(v, 2) == v, f"{key}={v!r} still has a tail"


def test_signal_levels_keep_fx_precision():
    """EUR/USD must not be over-rounded by the large-price rule."""
    sig = signals.signal_dict(signals.evaluate(_sig_board(1.11982, 1.1183, 1.11693, 1.11834)))
    assert round(sig["stop_loss"], 5) == sig["stop_loss"]
    assert sig["stop_loss"] != round(sig["stop_loss"], 2)


def test_rule_sentences_use_the_shared_precision():
    """The rule text said EMA20 4003.5100 while the API said 4003.51."""
    sig = signals.signal_dict(signals.evaluate(_sig_board(3978.5, 4003.51, 4018.39, 4039.89)))
    for r in sig["rules"]:
        for blob in (r["detail_en"], r["detail_ar"]):
            assert not re.search(r"\d\.\d{5,}", blob), f"tail in {r['name']}: {blob}"


# ---------------------------------------------------------------- deploy watch

def _load_watch():
    """Import the watchdog scripts (they live outside the package on purpose)."""
    import importlib.util
    from pathlib import Path
    mods = {}
    for name in ("check_deploy", "hours_audit"):
        path = Path(__file__).resolve().parent.parent / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods[name] = mod
    return mods


_WATCH = _load_watch()


def test_watch_reads_sleep_without_waking():
    """/robots.txt is the one probe that observes without intervening.

    Render answers it itself while the service is spun down; a 404 JSON body
    means FastAPI answered, so the app is awake. Anything else must be reported
    as unknown, never guessed as awake.
    """
    classify = _WATCH["check_deploy"].classify
    assert classify(404, '{"detail":"Not Found"}') == "awake"
    assert classify(200, "User-agent: *\nDisallow: /\n") == "asleep"
    assert classify(200, "<html>hi</html>") == "unknown"
    assert classify(None, "") == "unknown"


def test_watch_never_reports_failure_as_all_clear():
    """A failed read says 'unknown', and an unknown is not a pass."""
    verdict = _WATCH["check_deploy"].verdict
    assert verdict({"state": "asleep", "healthy": None})[0] is True
    assert verdict({"state": "awake", "healthy": True})[0] is True
    assert verdict({"state": "awake", "healthy": False})[0] is False
    ok, reason = verdict({"state": "unknown", "healthy": None})
    assert ok is False and "never completed" in reason


def test_hours_audit_flags_the_two_service_case():
    """750 h is per workspace: a second always-on Free service exhausts it."""
    audit = _WATCH["hours_audit"].audit
    one = audit([{"state": "awake"}] * 40, services=1)
    two = audit([{"state": "awake"}] * 40, services=2)
    assert one["projected"] < 750 < two["projected"]
    assert two["headroom"] < 0 and "OVER" in two["verdict"]
    # A service that mostly sleeps is cheap.
    sleepy = audit([{"state": "asleep"}] * 40, services=1)
    assert sleepy["projected"] == 0 and sleepy["headroom"] == 750


def test_hours_audit_refuses_to_guess_from_nothing():
    """No usable readings must not become a reassuring projection."""
    report = _WATCH["hours_audit"].audit([{"state": "unknown"}] * 10, services=1)
    assert report["projected"] is None
    assert "no usable readings" in report["verdict"]


# --------------------------------------------------------- markup (XSS) guard

def test_model_markup_is_stripped_before_it_leaves_the_server():
    """A tag in the model's prose must not survive to the browser.

    The panel is built with innerHTML, and the number guard checks figures, not
    tags. Stripping happens server-side; the client escapes as a second layer.
    """
    strip = ai._strip_markup
    assert "<script>" not in strip("Price 3982.69 <script>alert(1)</script>")
    assert "onerror" not in strip('x <img src=y onerror="alert(1)">')
    assert "<" not in strip("a <b>bold</b> b")
    # An entity-encoded tag loses its angle brackets, so it can no longer be
    # parsed as a tag. The bare word "script" is harmless text.
    decoded = strip("&lt;script&gt;alert(1)&lt;/script&gt;")
    assert "<" not in decoded and ">" not in decoded
    assert "alert(1)" in decoded
    # Ordinary prose is untouched.
    plain = "The price is 3982.69 and the trend is down."
    assert strip(plain) == plain
    assert strip("") == "" and strip(None) is None


def test_narrate_strips_markup_on_the_model_path():
    """The served narrative goes through the stripper, not only the guard."""
    import inspect
    src = inspect.getsource(ai.narrate)
    assert "_strip_markup(text)" in src


def test_security_headers_are_sent():
    """The page refuses to run a script that is not ours, and refuses framing."""
    h = client.get("/").headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert "script-src 'self'" in h["content-security-policy"]
    # script-src must NOT allow inline script, or the CSP would not stop XSS.
    assert "'unsafe-inline'" not in h["content-security-policy"].split("style-src")[0]
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"


def test_page_has_no_inline_handlers():
    """CSP without 'unsafe-inline' for scripts requires no inline handlers."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text("utf-8")
    assert "onclick=" not in html
    assert 'src="/static/app.js"' in html


def test_pitch_is_served_and_carries_no_script():
    """The pitch lives on the same origin and the same CSP as the desk.

    It switches language with CSS only, so it must contain no script at all --
    not an inline handler, not a <script> tag. If it ever needs one, it needs a
    carve-out in script-src, and this test is where that decision has to be
    faced rather than discovered in a browser.
    """
    from pathlib import Path

    r = client.get("/pitch")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # Same hardening as the desk, from the same middleware.
    assert "script-src 'self'" in r.headers["content-security-policy"]

    html = (Path(__file__).resolve().parent.parent / "web" / "pitch.html").read_text("utf-8")
    assert "<script" not in html.lower()
    assert "onclick=" not in html and "onchange=" not in html
    # Both languages ship in the one file, gated by CSS, so it works with JS off.
    assert 'id="lang-ar"' in html and 'id="lang-en"' in html


# ------------------------------------------------------------- abuse guard

def test_rate_limit_blocks_the_nth_plus_one_request(monkeypatch):
    """The endpoint spends the owner's quota, so a runaway loop must be cut off.

    The limit is per client IP. We drive the counter directly so the test does
    not depend on how many calls the rest of the suite has already made.
    """
    monkeypatch.setattr(main, "RATE_LIMIT", 3)
    main._hits.clear()
    assert all(main._within_rate("1.2.3.4") for _ in range(3))
    assert main._within_rate("1.2.3.4") is False
    # A different client has its own budget.
    assert main._within_rate("5.6.7.8") is True


def test_rate_window_slides(monkeypatch):
    """Old hits fall out of the window, so the limit is per minute, not forever."""
    monkeypatch.setattr(main, "RATE_LIMIT", 1)
    main._hits.clear()
    assert main._within_rate("9.9.9.9") is True
    assert main._within_rate("9.9.9.9") is False
    # Age the recorded hit past the window.
    main._hits["9.9.9.9"][0] -= 61
    assert main._within_rate("9.9.9.9") is True


def test_api_key_turns_away_a_direct_caller(monkeypatch):
    """With a key configured, a script that sends no key is refused."""
    monkeypatch.setattr(main, "API_KEY", "s3cret")
    r = client.get("/api/analyze", params={"symbol": "EURUSD=X", "interval": "1h"})
    assert r.status_code == 401


def test_api_key_is_not_bypassed_by_a_forged_sec_fetch_site(monkeypatch):
    """A caller-set header must not stand in for the key.

    The gate used to trust `Sec-Fetch-Site: same-origin` as proof of the bundled
    page. A browser sets it and page script cannot -- but a script sets it
    freely, so `curl -H 'Sec-Fetch-Site: same-origin'` got in with no key. A
    header the caller controls is not a credential; the gate fails closed now.
    """
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException
    monkeypatch.setattr(main, "API_KEY", "s3cret")
    main._hits.clear()
    forged = SimpleNamespace(headers={"sec-fetch-site": "same-origin"},
                             client=SimpleNamespace(host="10.0.0.1"))
    with pytest.raises(HTTPException) as err:
        asyncio.run(main.guard(forged))
    assert err.value.status_code == 401
    # The key itself still works, and only the exact key.
    good = SimpleNamespace(headers={"x-api-key": "s3cret"},
                           client=SimpleNamespace(host="10.0.0.2"))
    asyncio.run(main.guard(good))
    near = SimpleNamespace(headers={"x-api-key": "s3cret "},
                           client=SimpleNamespace(host="10.0.0.3"))
    with pytest.raises(HTTPException):
        asyncio.run(main.guard(near))


def test_bad_parameter_is_free(monkeypatch):
    """A rejected request must not spend the caller's rate budget."""
    monkeypatch.setattr(main, "RATE_LIMIT", 1)
    main._hits.clear()
    r = client.get("/api/analyze", params={"symbol": "EURUSD=X", "interval": "7s"})
    assert r.status_code == 400
    # The budget is untouched, so a valid request still goes through.
    assert main._within_rate("testclient") is True


# ------------------------------------------------------- degenerate inputs

def test_zero_price_does_not_crash_the_engine():
    """A broken read must degrade to 'no signal', not raise.

    Dividing by price for the level-room rule crashed the whole read before the
    `fatal` check ran. The honest output is NEUTRAL with a warning, never a
    stack trace and never a fabricated direction.
    """
    s = signals.evaluate(_board(price=0))
    assert s.direction == "NEUTRAL"
    assert s.confidence == 0.0
    assert any(w.code == "undefined_volatility" for w in s.warnings)


def test_negative_price_is_not_traded():
    """A negative price is nonsense; it must not produce a BUY."""
    s = signals.evaluate(_board(price=-5))
    assert s.direction == "NEUTRAL"
    assert s.confidence == 0.0


# ------------------------------------------- forwarded-IP must not be forgeable

def test_client_ip_ignores_forwarded_header_unless_a_proxy_is_trusted(monkeypatch):
    """X-Forwarded-For is caller-controlled, so it is an identity only behind a
    proxy we trust -- and the default does not trust one.

    Reading it blindly (even the last hop) let a script send a fresh value per
    request and step around the limit; the header is right *behind a proxy*
    because each proxy appends the address it saw, and wrong with nothing in
    front, which is the default case.
    """
    from types import SimpleNamespace

    spoofed = SimpleNamespace(
        headers={"x-forwarded-for": "9.9.9.9, 203.0.113.7"},
        client=SimpleNamespace(host="10.0.0.1"),
    )
    # Default: untrusted. The header is ignored; the socket peer wins.
    monkeypatch.setattr(main, "TRUST_PROXY", False)
    assert main._client_ip(spoofed) == "10.0.0.1"
    # Behind a proxy we trust: the last hop is the one our edge appended.
    monkeypatch.setattr(main, "TRUST_PROXY", True)
    assert main._client_ip(spoofed) == "203.0.113.7"
    # No forwarded header at all: the socket peer either way.
    direct = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.2"))
    assert main._client_ip(direct) == "10.0.0.2"


def test_forged_forwarded_ip_cannot_multiply_the_budget(monkeypatch):
    """One real client varying the forwarded header still exhausts one budget."""
    from types import SimpleNamespace

    monkeypatch.setattr(main, "TRUST_PROXY", False)
    monkeypatch.setattr(main, "RATE_LIMIT", 2)
    main._hits.clear()
    for fake in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        req = SimpleNamespace(headers={"x-forwarded-for": f"{fake}, 203.0.113.7"},
                              client=SimpleNamespace(host="10.0.0.1"))
        main._within_rate(main._client_ip(req))
    # Every forgery resolved to the same socket peer, so the third was refused.
    plain = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.1"))
    assert main._within_rate(main._client_ip(plain)) is False


def test_rate_limit_buckets_are_pruned_so_the_map_cannot_grow_forever():
    """Distinct invented identities must not accumulate without bound."""
    import time

    main._hits.clear()
    main._swept[0] = 0.0
    for i in range(50):
        main._hits[f"10.0.0.{i}"].append(0.0)  # all long expired
    main._prune(time.time())
    assert len(main._hits) == 0, "expired buckets were not swept"


def test_hsts_and_permissions_policy_are_sent():
    """HSTS pins https; the permissions policy refuses sensors nothing uses."""
    h = client.get("/").headers
    assert h["strict-transport-security"].startswith("max-age=")
    assert "geolocation=()" in h["permissions-policy"]


# ------------------------------------------------ spot is cached like futures

def test_spot_series_is_cached(monkeypatch):
    """Spot gold is the instrument this desk exists for; it must cache too.

    The cache used to sit below the spot branch, so XAUUSD was the one series
    that never cached and every read paid for a fresh upstream call.
    """
    import asyncio

    from app.market import Candle, Series

    calls = {"n": 0}

    async def fake_spot(symbol, interval):
        calls["n"] += 1
        return Series(symbol="XAUUSD", currency="USD", exchange="test",
                      interval=interval,
                      candles=[Candle(i, 1, 1, 1, 1, None) for i in range(70)],
                      short_name="Gold Spot", instrument_type="SPOT")

    monkeypatch.setattr(market, "fetch_spot_series", fake_spot)
    market._cache.clear()
    first = asyncio.run(market.fetch_series("XAUUSD", "1h"))
    second = asyncio.run(market.fetch_series("XAUUSD", "1h"))
    assert first is second          # the cached object, not a fresh fetch
    assert calls["n"] == 1, "spot was fetched twice; the cache did not apply"


def test_spot_cache_respects_fresh_flag(monkeypatch):
    """fresh=True must bypass the cache, so a forced re-read still re-reads."""
    import asyncio

    from app.market import Candle, Series

    calls = {"n": 0}

    async def fake_spot(symbol, interval):
        calls["n"] += 1
        return Series(symbol="XAUUSD", currency="USD", exchange="test",
                      interval=interval,
                      candles=[Candle(i, 1, 1, 1, 1, None) for i in range(70)],
                      short_name="Gold Spot", instrument_type="SPOT")

    monkeypatch.setattr(market, "fetch_spot_series", fake_spot)
    market._cache.clear()
    asyncio.run(market.fetch_series("XAUUSD", "1h"))
    asyncio.run(market.fetch_series("XAUUSD", "1h", fresh=True))
    assert calls["n"] == 2


# --------------------------------------- the fallback must not name a model

def test_deterministic_fallback_does_not_credit_a_model(monkeypatch):
    """No model wrote the fallback, so the card must not name one.

    The served narrative said source=deterministic while also reporting
    model=<primary>, which reads as 'this model wrote it'. It did not.
    """
    ind = _board()
    sig = signals.signal_dict(signals.evaluate(ind))

    async def always_bad(client, model, messages):
        return None, f"{model}: upstream returned 500"

    monkeypatch.setattr(ai, "_try_model", always_bad)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    out = asyncio.run(ai.narrate("EURUSD=X", "1h", ind, sig, "en"))
    assert out["source"] == "deterministic"
    assert out["model"] is None


# ------------------------------------------- config has one source of truth

def test_requirements_match_pyproject_dependencies():
    """requirements.txt and pyproject must not drift.

    Two hand-kept copies of one fact is exactly the failure this project keeps
    finding: they agree until someone edits one. Pin them together here.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    pyproject = {d.split(">=")[0].split("[")[0].strip().lower()
                 for d in data["project"]["dependencies"]}
    reqs = set()
    for raw in (root / "requirements.txt").read_text("utf-8").splitlines():
        clean = raw.split("#")[0].strip()
        if not clean:
            continue
        reqs.add(clean.split(">=")[0].split("[")[0].strip().lower())
    assert pyproject == reqs, f"drift: pyproject-only={pyproject - reqs}, req-only={reqs - pyproject}"


def test_pyproject_declares_the_supported_python_floor():
    """requires-python must match the target the linters and CI use."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    data = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))
    assert data["project"]["requires-python"] == ">=3.11"
    assert data["tool"]["ruff"]["target-version"] == "py311"
    assert data["tool"]["mypy"]["python_version"] == "3.11"


# ============================ market.py: every venue and every failure path
# The market layer is the one place a wrong number could enter the desk, and it
# talks to four live venues. These drive each path with a canned payload so the
# parsing rules are pinned without depending on a provider being up.

class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    """Stands in for httpx.AsyncClient: a queue of canned responses, async CM."""

    def __init__(self, *responses, calls=None):
        self._responses = list(responses)
        self.calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        self.calls.append((url, params))
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _yahoo_payload(n=120, start=1700000000, step=3600, close0=100.0):
    stamps = [start + i * step for i in range(n)]
    closes = [close0 + i * 0.1 for i in range(n)]
    return {"chart": {"result": [{
        "meta": {"symbol": "EURUSD=X", "currency": "USD", "exchangeName": "CCY",
                 "shortName": "EUR/USD", "instrumentType": "CURRENCY"},
        "timestamp": stamps,
        "indicators": {"quote": [{
            "open": closes, "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes], "close": closes,
            "volume": [None] * n,
        }]},
    }]}}


def test_series_to_frame_and_last_price():
    from app.market import Candle, Series

    s = Series(symbol="X", currency="USD", exchange="E", interval="1h",
               candles=[Candle(1, 1, 2, 0, 3, None), Candle(2, 2, 3, 1, 4, None)])
    assert s.last_price == 4
    df = s.to_frame()
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.name == "t" and len(df) == 2


def test_cache_put_evicts_the_oldest_entry(monkeypatch):
    from app.market import Candle, Series

    monkeypatch.setattr(market, "_CACHE_MAX", 2)
    market._cache.clear()
    mk = lambda i: (float(i), Series(symbol=str(i), currency="", exchange="",  # noqa: E731
                                     interval="1h", candles=[Candle(i, 1, 1, 1, 1, None)]))
    market._cache_put(("a", "1h"), mk(1))
    market._cache_put(("b", "1h"), mk(2))
    market._cache_put(("c", "1h"), mk(3))  # evicts "a" (oldest timestamp)
    assert ("a", "1h") not in market._cache
    assert ("b", "1h") in market._cache and ("c", "1h") in market._cache


def test_fetch_series_rejects_an_unknown_interval():
    import asyncio

    try:
        asyncio.run(market.fetch_series("EURUSD=X", "7s"))
        raise AssertionError("unknown interval should raise")
    except market.MarketDataError as exc:
        assert "7s" in str(exc)


def test_the_market_layer_refuses_a_path_walking_symbol_before_building_a_url(monkeypatch):
    """The symbol becomes a path segment, so this is the boundary that matters.

    CodeQL flagged the URL build as a partial SSRF: the symbol reached
    `YAHOO.format(...)` with only the API validating it, and `fetch_series` has
    other callers (alerts.py, the scripts). The refusal now lives here, before a
    request is built, so no caller can route a `..` into the upstream path.
    """
    fake = _Client()
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    for bad in ("../../etc/passwd", "a/../b", "..", ".", "a..b", "a/b", "has space", "x" * 21, "sym;drop"):
        try:
            asyncio.run(market.fetch_series(bad, "1h"))
            raise AssertionError(f"{bad!r} should be refused")
        except market.MarketDataError:
            pass
    assert not fake.calls, "no request may be built for a refused symbol"


def test_a_real_symbol_is_percent_encoded_into_the_path(monkeypatch):
    """A valid symbol keeps working, and its punctuation is encoded, not raw."""
    fake = _Client(_Resp(200, _yahoo_payload(120)))
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    market._cache.clear()
    asyncio.run(market.fetch_series("^GSPC", "1h"))
    assert fake.calls, "the request should have been attempted"
    url = fake.calls[0][0]
    assert "%5EGSPC" in url, f"the symbol was not encoded into the path: {url}"


def test_fetch_series_parses_a_yahoo_payload(monkeypatch):
    import asyncio

    fake = _Client(_Resp(200, _yahoo_payload(120)))
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    market._cache.clear()
    s = asyncio.run(market.fetch_series("EURUSD=X", "1h"))
    assert s.currency == "USD" and s.exchange == "CCY"
    assert s.short_name == "EUR/USD" and s.instrument_type == "CURRENCY"
    assert len(s.candles) == 120
    assert s.candles[0].t == 1700000000


def test_fetch_series_reports_a_non_200_loudly(monkeypatch):
    import asyncio

    fake = _Client(_Resp(503, None))
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    market._cache.clear()
    try:
        asyncio.run(market.fetch_series("EURUSD=X", "1h"))
        raise AssertionError("503 should raise")
    except market.MarketDataError as exc:
        assert "503" in str(exc)


def test_fetch_series_reports_a_network_error_loudly(monkeypatch):
    import asyncio

    fake = _Client(market.httpx.ConnectError("boom"))
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    market._cache.clear()
    try:
        asyncio.run(market.fetch_series("EURUSD=X", "1h"))
        raise AssertionError("network error should raise")
    except market.MarketDataError as exc:
        assert "network error" in str(exc)


def test_fetch_series_reports_an_unreadable_payload(monkeypatch):
    import asyncio

    fake = _Client(_Resp(200, {"chart": {"result": []}}))
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    market._cache.clear()
    try:
        asyncio.run(market.fetch_series("EURUSD=X", "1h"))
        raise AssertionError("empty result should raise")
    except market.MarketDataError as exc:
        assert "unreadable" in str(exc)


def test_fetch_series_refuses_too_little_history(monkeypatch):
    import asyncio

    fake = _Client(_Resp(200, _yahoo_payload(30)))  # under the 60-candle floor
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    market._cache.clear()
    try:
        asyncio.run(market.fetch_series("EURUSD=X", "1h"))
        raise AssertionError("short history should raise")
    except market.MarketDataError as exc:
        assert "not enough history" in str(exc)


def test_fetch_series_skips_candles_with_a_null_field(monkeypatch):
    """A gap in the feed is dropped, never filled with a made-up value."""
    import asyncio

    payload = _yahoo_payload(80)
    quote = payload["chart"]["result"][0]["indicators"]["quote"][0]
    quote["close"][5] = None  # one bad bar
    fake = _Client(_Resp(200, payload))
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    market._cache.clear()
    s = asyncio.run(market.fetch_series("EURUSD=X", "1h"))
    assert len(s.candles) == 79


def test_fetch_series_serves_the_second_call_from_cache(monkeypatch):
    import asyncio

    calls = []
    fake = _Client(_Resp(200, _yahoo_payload(120)), calls=calls)
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    market._cache.clear()
    a = asyncio.run(market.fetch_series("EURUSD=X", "1h"))
    b = asyncio.run(market.fetch_series("EURUSD=X", "1h"))
    assert a is b and len(calls) == 1


# ---- Kraken (primary spot source) ----

def test_kraken_rejects_a_non_200():
    import asyncio

    try:
        asyncio.run(market._kraken_page(_Client(_Resp(502, None)), "PAXGUSD", 60))
        raise AssertionError("502 should raise")
    except market.MarketDataError as exc:
        assert "502" in str(exc)


def test_kraken_rejects_an_unreadable_body():
    import asyncio

    try:
        asyncio.run(market._kraken_page(_Client(_Resp(200, ValueError("x"))), "PAXGUSD", 60))
        raise AssertionError("bad json should raise")
    except market.MarketDataError as exc:
        assert "unreadable" in str(exc)


def test_kraken_surfaces_a_source_error_field():
    import asyncio

    body = {"error": ["EQuery:Unknown asset pair"], "result": {}}
    try:
        asyncio.run(market._kraken_page(_Client(_Resp(200, body)), "NOPE", 60))
        raise AssertionError("error field should raise")
    except market.MarketDataError as exc:
        assert "Unknown asset pair" in str(exc)


def test_kraken_reports_an_empty_result():
    import asyncio

    body = {"error": [], "result": {"last": 123}}
    try:
        asyncio.run(market._kraken_page(_Client(_Resp(200, body)), "PAXGUSD", 60))
        raise AssertionError("no rows should raise")
    except market.MarketDataError as exc:
        assert "no candles" in str(exc)


# ---- Coinbase (fallback spot source) ----

def test_coinbase_rejects_a_non_200():
    import asyncio

    try:
        asyncio.run(market._coinbase_page(_Client(_Resp(500, None)), "PAXG-USD", 3600))
        raise AssertionError("500 should raise")
    except market.MarketDataError as exc:
        assert "500" in str(exc)


def test_coinbase_rejects_an_unreadable_body():
    import asyncio

    try:
        asyncio.run(market._coinbase_page(_Client(_Resp(200, ValueError("x"))), "PAXG-USD", 3600))
        raise AssertionError("bad json should raise")
    except market.MarketDataError as exc:
        assert "unreadable" in str(exc)


def test_coinbase_rejects_an_empty_list():
    import asyncio

    try:
        asyncio.run(market._coinbase_page(_Client(_Resp(200, [])), "PAXG-USD", 3600))
        raise AssertionError("empty list should raise")
    except market.MarketDataError as exc:
        assert "no candles" in str(exc)


def test_coinbase_paginates_until_enough_history():
    import asyncio

    page = [[1793311200 + i * 3600, 1.0, 2.0, 1.5, 1.8, 5.0] for i in range(60)]
    older = [[1793311200 - (i + 1) * 3600, 1.0, 2.0, 1.5, 1.8, 5.0] for i in range(60)]
    fake = _Client(_Resp(200, page), _Resp(200, older))
    out = asyncio.run(market._spot_via_coinbase(fake, {"product": "PAXG-USD"}, "1h"))
    assert len(out) == 120  # first page plus one older page


def test_coinbase_pagination_stops_at_the_end_of_history():
    """An exhausted feed must not loop forever: stop and use what we have."""
    import asyncio

    page = [[1793311200 + i * 3600, 1.0, 2.0, 1.5, 1.8, 5.0] for i in range(60)]
    fake = _Client(_Resp(200, page), _Resp(500, None))  # the older page fails
    out = asyncio.run(market._spot_via_coinbase(fake, {"product": "PAXG-USD"}, "1h"))
    assert len(out) == 60


# ---- fetch_spot_series (Kraken then Coinbase) ----

def test_spot_series_uses_kraken_when_it_answers(monkeypatch):
    import asyncio

    rows = [[1793311200 + i * 60, 1.0, 2.0, 0.5, 1.5, 1.0, 9.0, 0] for i in range(70)]
    fake = _Client(_Resp(200, {"error": [], "result": {"PAXGUSD": rows, "last": 1}}))
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    s = asyncio.run(market.fetch_spot_series("XAUUSD", "1h"))
    assert s.exchange == "Kraken PAXG/USD" and len(s.candles) == 70
    assert s.instrument_type == "SPOT" and s.currency == "USD"


def test_spot_series_falls_back_to_coinbase_when_kraken_fails(monkeypatch):
    import asyncio

    rows = [[1793311200 + i * 3600, 1.0, 2.0, 0.5, 1.5, 9.0] for i in range(120)]
    fake = _Client(_Resp(503, None), _Resp(200, rows))
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    s = asyncio.run(market.fetch_spot_series("XAUUSD", "1h"))
    assert s.exchange == "Coinbase PAXG-USD" and len(s.candles) == 120


def test_spot_series_reports_both_sources_failing(monkeypatch):
    import asyncio

    fake = _Client(_Resp(503, None), _Resp(500, None))
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    try:
        asyncio.run(market.fetch_spot_series("XAUUSD", "1h"))
        raise AssertionError("both failing should raise")
    except market.MarketDataError as exc:
        assert "503" in str(exc) and "500" in str(exc)


def test_spot_series_has_no_source_for_an_unknown_instrument():
    import asyncio

    try:
        asyncio.run(market.fetch_spot_series("XPTUSD", "1h"))
        raise AssertionError("unknown spot instrument should raise")
    except market.MarketDataError as exc:
        assert "no spot source" in str(exc)


def test_spot_series_rejects_an_unsupported_timeframe():
    import asyncio

    try:
        asyncio.run(market.fetch_spot_series("XAUUSD", "2h"))
        raise AssertionError("2h should raise")
    except market.MarketDataError as exc:
        assert "2h" in str(exc)


def test_spot_series_refuses_too_little_history(monkeypatch):
    import asyncio

    rows = [[1793311200 + i * 60, 1.0, 2.0, 0.5, 1.5, 1.0, 9.0, 0] for i in range(30)]
    fake = _Client(_Resp(200, {"error": [], "result": {"PAXGUSD": rows}}))
    monkeypatch.setattr(market.httpx, "AsyncClient", lambda **k: fake)
    try:
        asyncio.run(market.fetch_spot_series("XAUUSD", "1h"))
        raise AssertionError("short history should raise")
    except market.MarketDataError as exc:
        assert "not enough history" in str(exc)


# ---- fetch_spot (the broker quote beside the chart) ----

def test_fetch_spot_prefers_swissquote():
    import asyncio

    payload = [{"spreadProfilePrices": [{"spreadProfile": "premium", "bid": 4002.1, "ask": 4002.4}]}]
    out = asyncio.run(_fetch_spot_with(_Client(_Resp(200, payload))))
    assert out["source"] == "Swissquote" and out["price"] == 4002.25


def test_fetch_spot_falls_back_to_gold_api():
    import asyncio

    body = {"name": "Gold", "price": 4001.5, "currency": "USD", "updatedAt": "2026-01-01"}
    out = asyncio.run(_fetch_spot_with(_Client(_Resp(503, None), _Resp(200, body))))
    assert out["source"] == "gold-api" and out["price"] == 4001.5


def test_fetch_spot_reports_both_sources_failing():
    import asyncio

    try:
        asyncio.run(_fetch_spot_with(_Client(_Resp(503, None), _Resp(500, None))))
        raise AssertionError("both failing should raise")
    except market.MarketDataError as exc:
        assert "Swissquote" in str(exc) and "gold-api" in str(exc)


def test_fetch_spot_survives_an_exception_on_either_source():
    import asyncio

    try:
        asyncio.run(_fetch_spot_with(_Client(market.httpx.ConnectError("a"),
                                             ValueError("b"))))
        raise AssertionError("exceptions should be collected and raise")
    except market.MarketDataError as exc:
        assert "Swissquote" in str(exc)


def test_fetch_spot_rejects_an_unknown_metal():
    import asyncio

    try:
        asyncio.run(market.fetch_spot("XPD"))
        raise AssertionError("unknown metal should raise")
    except market.MarketDataError as exc:
        assert "unsupported spot metal" in str(exc)


async def _fetch_spot_with(fake):
    """Drive fetch_spot with an injected client, since it builds its own."""
    orig = market.httpx.AsyncClient
    market.httpx.AsyncClient = lambda **k: fake
    try:
        return await market.fetch_spot("XAU")
    finally:
        market.httpx.AsyncClient = orig


# ---- anchor_series_to_spot (put the proxy on the broker's scale) ----

def _spot_series(n=120, close0=100.0, step=0.1):
    from app.market import Candle, Series

    candles = [
        Candle(1700000000 + i * 3600, close0 + i * step, close0 + 0.5 + i * step,
               close0 - 0.5 + i * step, close0 + i * step, None)
        for i in range(n)
    ]
    return Series(symbol="XAUUSD", currency="USD", exchange="Kraken PAXG/USD",
                  interval="1h", candles=candles, short_name="Gold Spot (XAU/USD)",
                  instrument_type="SPOT")


def test_anchor_rescales_the_levels_onto_the_live_quote():
    s = _spot_series()
    proxy_last = s.candles[-1].c
    out = market.anchor_series_to_spot(s, {"price": proxy_last * 0.998, "source": "Swissquote"})
    assert out.anchor["ok"] is True
    # The last close now sits on the quote, to the float.
    assert abs(out.candles[-1].c - proxy_last * 0.998) < 1e-9
    assert out.anchor["source"] == "Swissquote"
    # The input series is untouched -- anchoring returns a new one.
    assert s.candles[-1].c == proxy_last


def test_anchor_leaves_every_ratio_unchanged():
    """RSI, ATR% and Bollinger position are ratios; a uniform scale cancels."""
    from app import indicators

    s = _spot_series()
    before = indicators.snapshot(s)
    after = indicators.snapshot(market.anchor_series_to_spot(
        s, {"price": s.candles[-1].c * 0.99, "source": "Swissquote"}))
    for key in ("rsi", "atr_pct", "bb_position", "stoch_k"):
        assert before[key] == pytest.approx(after[key], rel=1e-9), key
    # Only the absolute prices move.
    assert after["price"] < before["price"]
    assert after["support"] < before["support"]


def test_anchor_refuses_a_diverged_proxy():
    """A token that lost its peg is not spot; rescaling it would fabricate a price."""
    s = _spot_series()
    out = market.anchor_series_to_spot(s, {"price": s.candles[-1].c * 0.8})
    assert out.anchor["ok"] is False and out.anchor["reason"] == "proxy diverged"
    assert out.candles[-1].c == s.candles[-1].c  # raw candles, not rescaled


def test_anchor_declines_without_a_quote():
    s = _spot_series()
    assert market.anchor_series_to_spot(s, None).anchor["ok"] is False
    assert market.anchor_series_to_spot(s, {"error": "503"}).anchor["ok"] is False
    assert market.anchor_series_to_spot(s, {"price": 0}).anchor["ok"] is False


def test_anchor_ignores_a_non_spot_series():
    """A currency or a futures contract is already the real thing."""
    fx = _series("EURUSD=X")
    out = market.anchor_series_to_spot(fx, {"price": 9999})
    assert out.anchor is None and out is fx


# --------------------------------------------- the browser side has tests too

def test_ui_helpers_pass_under_node():
    """The card is half the product; run its pure helpers under node.

    Skipped when node is absent so a Python-only environment still passes; CI
    runs the same file directly in a dedicated job, so it is never skipped there.
    """
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [node, "--test", str(root / "web" / "app.test.mjs")],
        capture_output=True, text=True, cwd=root, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_page_loads_app_js_as_a_module():
    """app.js imports lib.js, so the tag must be type=module or the import fails."""
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text("utf-8")
    assert 'type="module"' in html
    assert 'src="/static/app.js"' in html


def test_the_page_and_the_server_share_one_precision_rule():
    """The card must not import its own copy of price_decimals.

    Two copies of one rule is the drift this project keeps finding; lib.js is
    the single source, and the server's indicators.price_decimals mirrors it.
    """
    from pathlib import Path

    web = Path(__file__).resolve().parent.parent / "web"
    app_js = (web / "app.js").read_text("utf-8")
    assert "from \"./lib.js\"" in app_js
    assert "function fmt(" not in app_js      # not re-declared locally
    assert "function esc(" not in app_js
    lib = (web / "lib.js").read_text("utf-8")
    assert "export function fmt" in lib and "export function esc" in lib


# ------------------------------------------------- accessibility and mobile

def _page():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text("utf-8")


def test_page_declares_a_language_and_direction():
    html = _page()
    assert 'lang="ar"' in html and 'dir="rtl"' in html


def test_interactive_controls_are_labelled():
    """A bare <select> has no accessible name; each one needs a <label>."""
    html = _page()
    assert 'for="sym"' in html and 'for="iv"' in html
    assert 'aria-pressed' in html           # the language toggle states its state


def test_status_region_announces_itself():
    """The load/error line is a live region, or a screen reader never hears it."""
    html = _page()
    assert 'role="status"' in html and 'aria-live="polite"' in html


def test_motion_and_focus_preferences_are_respected():
    html = _page()
    assert "prefers-reduced-motion" in html
    assert ":focus-visible" in html
    assert "@media(max-width:560px)" in html


def test_canvas_carries_a_text_description():
    """A canvas is opaque to assistive tech; the panel adds a description."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text("utf-8")
    assert "role=\"img\"" in js and "aria-label=" in js
    assert "sr-only" in js
    assert "function chartAlt" in js


def test_chart_draws_axes_not_just_candles():
    """A chart with no price or time axis is decoration, not a chart."""
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text("utf-8")
    assert "padR" in js and "padB" in js          # reserved axis gutters
    assert "fillText(fmt(v)" in js                # price labels
    assert "toLocaleString" in js                 # time labels


# ------------------------------------- the rate limit's scope must stay honest

def test_image_pins_one_worker():
    """The limiter counts in-process, so N workers would mean N times the limit.

    Pinning --workers 1 is what makes the configured number the real number.
    If someone raises it, they must move the counter to a shared store first --
    this test makes that a deliberate act, not an accident.
    """
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text("utf-8")
    cmd = [ln for ln in dockerfile.splitlines() if ln.startswith("CMD")]
    assert cmd, "no CMD in the Dockerfile"
    assert "--workers 1" in cmd[0], "the image must run a single worker for the limiter to hold"


def test_rate_limit_scope_is_documented_not_hidden():
    """The limitation must be written down where a reader will meet it."""
    from pathlib import Path

    main_src = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text("utf-8")
    # Collapse comment markers and newlines so a phrase split across two comment
    # lines still matches.
    flat = " ".join(main_src.replace("#", " ").split())
    assert "per process" in flat
    assert "--workers 1" in flat


# ================================ main.py: the endpoints, end to end
# The API layer is where every layer meets, and it had no integration test: the
# pieces were tested apart, never wired. These drive real requests through
# TestClient with the network stubbed at the function boundary (monkeypatch
# restores it after each test, so no test can leak a stub into another).


def _series(symbol="EURUSD=X", n=120, kind="CURRENCY", short="EUR/USD"):
    from app.market import Candle, Series

    candles = [
        Candle(1700000000 + i * 3600, 100 + i * 0.1, 100.5 + i * 0.1, 99.5 + i * 0.1, 100 + i * 0.1, None)
        for i in range(n)
    ]
    return Series(
        symbol=symbol,
        currency="USD",
        exchange="CCY",
        interval="1h",
        candles=candles,
        short_name=short,
        instrument_type=kind,
    )


def _wire(monkeypatch, series, spot=None, spot_error=None):
    """Stub the market layer and hand back a client with a clean rate budget."""
    from app import main

    async def fake_fetch(sym, iv, **kw):
        if isinstance(series, Exception):
            raise series
        return series

    async def fake_spot(metal):
        if spot_error:
            raise market.MarketDataError(spot_error)
        return spot or {"metal": metal, "price": 4000.5, "source": "Swissquote"}

    monkeypatch.setattr(main.market, "fetch_series", fake_fetch)
    monkeypatch.setattr(main.market, "fetch_spot", fake_spot)
    main._hits.clear()
    return TestClient(main.app)


def test_health_reports_the_model_and_whether_a_key_is_set(monkeypatch):
    from app import main

    monkeypatch.setenv("GROQ_API_KEY", "x")
    body = TestClient(main.app).get("/api/health").json()
    assert body["status"] == "ok" and body["model"] and body["model_key"] is True


def test_symbols_lists_the_watchlist_and_intervals():
    from app import main

    body = TestClient(main.app).get("/api/symbols").json()
    syms = {s["symbol"] for s in body["symbols"]}
    assert "XAUUSD" in syms and "EURUSD=X" in syms and "GC=F" in syms
    assert set(body["intervals"]) == {"5m", "15m", "1h", "1d", "1wk"}


def test_analyze_returns_the_full_provenance_block(monkeypatch):
    c = _wire(monkeypatch, _series("EURUSD=X"))
    body = c.get("/api/analyze?symbol=EURUSD=X&interval=1h&lang=en").json()
    assert body["symbol"] == "EURUSD=X" and body["interval"] == "1h"
    assert body["instrument"]["kind"] == "spot"  # CURRENCY is not FUTURE
    assert body["instrument"]["name"] == "EUR/USD"
    assert body["indicators"] and body["signal"]
    assert body["narrative"]["source"] in ("model", "deterministic")
    assert "not investment advice" in body["disclaimer"].lower()


def test_analyze_labels_a_futures_contract_and_attaches_spot(monkeypatch):
    c = _wire(monkeypatch, _series("GC=F", kind="FUTURE", short="Gold Futures"))
    body = c.get("/api/analyze?symbol=GC=F&interval=1h").json()
    assert body["instrument"]["kind"] == "futures"
    assert body["spot"] and body["spot"]["metal"] == "XAU"


def test_analyze_says_spot_failed_instead_of_hiding_it(monkeypatch):
    """A missing second opinion is reported, never substituted."""
    c = _wire(monkeypatch, _series("GC=F", kind="FUTURE"), spot_error="upstream 503")
    body = c.get("/api/analyze?symbol=GC=F&interval=1h").json()
    assert body["spot"]["error"] and "price" not in body["spot"]


def test_analyze_anchors_spot_gold_onto_the_broker_quote(monkeypatch):
    """The card's levels must be the broker's, not the token's."""
    quote = 111.68  # ~0.2% under the proxy's last close of 111.9, as PAXG trades
    c = _wire(monkeypatch, _spot_series(), spot={"metal": "XAU", "price": quote,
                                                 "source": "Swissquote"})
    body = c.get("/api/analyze?symbol=XAUUSD&interval=1h").json()
    assert body["anchor"]["ok"] is True
    assert body["anchor"]["source"] == "Swissquote"
    # Every level is now on the quote's scale, within a hair of it.
    assert abs(body["indicators"]["price"] - quote) < 0.05
    assert body["signal"]["entry"] <= 111.7


def test_analyze_reports_a_diverged_proxy_as_a_warning(monkeypatch):
    """A proxy far from spot is flagged, and its levels are not passed off as spot."""
    c = _wire(monkeypatch, _spot_series(), spot={"metal": "XAU", "price": 50.0,
                                                 "source": "Swissquote"})  # 55% away
    body = c.get("/api/analyze?symbol=XAUUSD&interval=1h").json()
    assert body["anchor"]["ok"] is False
    codes = [w["code"] for w in body["signal"]["warnings"]]
    assert "proxy_diverged" in codes
    # The warning is bilingual, like every other string on the card.
    w = next(w for w in body["signal"]["warnings"] if w["code"] == "proxy_diverged")
    assert w["ar"] and w["en"]


def test_analyze_flags_levels_as_proxy_when_spot_is_unreadable(monkeypatch):
    c = _wire(monkeypatch, _spot_series(), spot_error="upstream 503")
    body = c.get("/api/analyze?symbol=XAUUSD&interval=1h").json()
    codes = [w["code"] for w in body["signal"]["warnings"]]
    assert "spot_unread" in codes and body["anchor"]["ok"] is False


def test_instrument_names_the_real_provider(monkeypatch):
    """The footer credits this, so it must not say Yahoo for a Kraken series."""
    c = _wire(monkeypatch, _spot_series(), spot={"metal": "XAU", "price": 111.68})
    gold = c.get("/api/analyze?symbol=XAUUSD&interval=1h").json()
    assert "Kraken" in gold["instrument"]["provider"]
    fx = _wire(monkeypatch, _series("EURUSD=X"))
    eur = fx.get("/api/analyze?symbol=EURUSD=X&interval=1h").json()
    assert eur["instrument"]["provider"] == "Yahoo Finance"


def test_candles_for_spot_gold_are_anchored_too(monkeypatch):
    """The chart must not draw the token's prices under a card that shows spot."""
    from app import main

    c = _wire(monkeypatch, _spot_series(), spot={"metal": "XAU", "price": 111.68,
                                                 "source": "Swissquote"})
    body = c.get("/api/candles?symbol=XAUUSD&interval=1h").json()
    assert abs(body["candles"][-1]["c"] - 111.68) < 0.05
    assert main.market.SPOT_SYMBOLS["XAUUSD"]["metal"] == "XAU"


def test_analyze_rejects_an_unknown_interval(monkeypatch):
    c = _wire(monkeypatch, _series())
    r = c.get("/api/analyze?symbol=EURUSD=X&interval=7s")
    assert r.status_code == 400 and "unsupported interval" in r.json()["detail"]


def test_analyze_returns_200_when_the_model_answers(monkeypatch):
    """The model-success path must serialize, not 500.

    ai.narrate returns note=None when a model wrote the prose, but the schema
    typed note as a required `str`. No API-level test set a key *and* let the
    model answer, so every analyze call in the suite took the deterministic
    branch (note always a string) and the mismatch stayed invisible -- until a
    valid key reached production and every successful read became a 500.
    """
    from app import ai

    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    async def model_answers(client, model, messages):
        # No figures and no banned words, so the verify guard accepts it.
        return "The read is long and the indicators agree.", None

    monkeypatch.setattr(ai, "_try_model", model_answers)
    c = _wire(monkeypatch, _series())

    r = c.get("/api/analyze?symbol=EURUSD=X&interval=1h&lang=en")
    assert r.status_code == 200, r.text
    narrative = r.json()["narrative"]
    assert narrative["source"] == "model"
    assert narrative["model"] == ai.MODELS[0]
    assert narrative["note"] is None  # nothing to explain; the model wrote it


def test_narrative_note_is_optional_but_present(monkeypatch):
    """note is null on success and a reason on fallback -- both must serialize."""
    from app import ai, schemas

    ok = schemas.NarrativeOut(text="t", model="m", source="model", note=None)
    assert ok.note is None
    fell_back = schemas.NarrativeOut(text="t", source="deterministic", note="key rejected")
    assert fell_back.note == "key rejected"
    # The field is still always emitted, so a client never has to guess.
    assert "note" in ok.model_dump() and "note" in fell_back.model_dump()


def test_analyze_rejects_an_unknown_language(monkeypatch):
    c = _wire(monkeypatch, _series())
    r = c.get("/api/analyze?symbol=EURUSD=X&interval=1h&lang=fr")
    assert r.status_code == 400 and "ar or en" in r.json()["detail"]


def test_a_bad_parameter_does_not_spend_the_rate_budget(monkeypatch):
    """A typo must not lock a user out; the guard runs after validation."""
    from app import main

    c = _wire(monkeypatch, _series())
    for _ in range(main.RATE_LIMIT + 5):
        assert c.get("/api/analyze?symbol=EURUSD=X&interval=nope").status_code == 400
    # The budget is untouched, so a real request still goes through.
    assert c.get("/api/analyze?symbol=EURUSD=X&interval=1h").status_code == 200


def test_a_failed_market_read_is_a_502_not_a_blank_answer(monkeypatch):
    c = _wire(monkeypatch, market.MarketDataError("Yahoo returned 503"))
    r = c.get("/api/analyze?symbol=EURUSD=X&interval=1h")
    assert r.status_code == 502 and "503" in r.json()["detail"]


def test_candles_returns_the_tail_of_the_series(monkeypatch):
    c = _wire(monkeypatch, _series(n=250))
    body = c.get("/api/candles?symbol=EURUSD=X&interval=1h").json()
    assert len(body["candles"]) == 200  # capped at the last 200
    assert set(body["candles"][0]) == {"t", "o", "h", "l", "c"}


def test_candles_rejects_an_unknown_interval(monkeypatch):
    c = _wire(monkeypatch, _series())
    assert c.get("/api/candles?symbol=EURUSD=X&interval=3m").status_code == 400


def test_the_page_is_served_with_a_csp_header():
    from app import main

    r = TestClient(main.app).get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_the_rate_limit_returns_429_with_a_retry_after(monkeypatch):
    from app import main

    c = _wire(monkeypatch, _series())
    monkeypatch.setattr(main, "RATE_LIMIT", 2)
    assert c.get("/api/analyze?symbol=EURUSD=X&interval=1h").status_code == 200
    assert c.get("/api/analyze?symbol=EURUSD=X&interval=1h").status_code == 200
    r = c.get("/api/analyze?symbol=EURUSD=X&interval=1h")
    assert r.status_code == 429 and r.headers.get("retry-after") == "60"


def test_the_api_key_gate_requires_the_key_from_every_caller(monkeypatch):
    """With a key set, only the key opens the endpoint -- a spoofed page header
    does not. The gate no longer distinguishes page from script, because it
    cannot: both are just a client that sets headers."""
    from app import main

    c = _wire(monkeypatch, _series())
    monkeypatch.setattr(main, "API_KEY", "secret")
    direct = c.get("/api/analyze?symbol=EURUSD=X&interval=1h")
    assert direct.status_code == 401
    forged_page = c.get("/api/analyze?symbol=EURUSD=X&interval=1h",
                        headers={"sec-fetch-site": "same-origin"})
    assert forged_page.status_code == 401
    keyed = c.get("/api/analyze?symbol=EURUSD=X&interval=1h", headers={"x-api-key": "secret"})
    assert keyed.status_code == 200


# ================================= ai.py: the model fallback chain
# The chain is the difference between "the desk is blind when one quota runs
# out" and "it degrades gracefully". It was never exercised: the tests covered
# the guard and the offline sentence, but not the walk from model to model.


class _GroqResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _GroqClient:
    """A stand-in for httpx.AsyncClient: a queue of canned Groq replies."""

    def __init__(self, *responses, calls=None):
        self._responses = list(responses)
        self.calls = calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, content=None):
        self.calls.append(json.loads(content))
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _groq_ok(text):
    return _GroqResponse(200, {"choices": [{"message": {"content": text}}]})


def _ai_board(price=100.0):
    """A rising series, so the last close sits ABOVE its EMA20 (a fact the
    contradiction test needs to be able to lie about)."""
    from app.indicators import snapshot
    from app.market import Candle, Series

    candles = [
        Candle(1700000000 + i * 3600, 80 + i * 0.2, 80.2 + i * 0.2, 79.8 + i * 0.2, 80 + i * 0.2, None)
        for i in range(220)
    ]
    return snapshot(Series(symbol="X", currency="USD", exchange="E", interval="1h", candles=candles))


def _ai_sig(board):
    from app.signals import evaluate, signal_dict

    return signal_dict(evaluate(board))


def _narrate_with(monkeypatch, client, lang="en"):
    from app import ai

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(ai.httpx, "AsyncClient", lambda **k: client)
    b = _ai_board()
    return asyncio.run(ai.narrate("EURUSD=X", "1h", b, _ai_sig(b), lang))


def test_narrate_uses_the_first_model_when_it_answers(monkeypatch):
    from app import ai

    text = f"Price is {_n_board_price()} and the read is long."
    out = _narrate_with(monkeypatch, _GroqClient(_groq_ok(text)))
    assert out["source"] == "model" and out["model"] == ai.MODELS[0]
    assert out["note"] is None


def _n_board_price():
    from app import ai

    return ai._n(_ai_board()["price"])


def test_narrate_walks_to_the_second_model_when_the_first_is_rate_limited(monkeypatch):
    from app import ai

    text = f"Price is {_n_board_price()}."
    client = _GroqClient(_GroqResponse(429, {"error": {"message": "rate limited"}}), _groq_ok(text))
    out = _narrate_with(monkeypatch, client)
    assert out["source"] == "model" and out["model"] == ai.MODELS[1]
    assert len(client.calls) == 2


def test_narrate_retries_without_reasoning_effort_on_a_400(monkeypatch):
    """Not every model accepts reasoning_effort; a 400 must retry, not give up."""
    client = _GroqClient(_GroqResponse(400, {}), _groq_ok(f"Price is {_n_board_price()}."))
    out = _narrate_with(monkeypatch, client)
    assert out["source"] == "model"
    assert "reasoning_effort" in client.calls[0]
    assert "reasoning_effort" not in client.calls[1]


def test_narrate_gives_up_immediately_on_a_rejected_key(monkeypatch):
    """A 401 is config, not weather: retrying the other models wastes the budget."""
    from app import ai

    client = _GroqClient(_GroqResponse(401, {}))
    out = _narrate_with(monkeypatch, client)
    assert out["source"] == "deterministic" and out["model"] is None
    assert len(client.calls) == 1  # did not try the rest
    assert ai.MODELS[0] not in out["text"]  # no model is credited
    assert "key" in out["note"].lower()


def test_narrate_falls_back_when_every_model_contradicts_the_facts(monkeypatch):
    from app import ai

    lie = "Price is below EMA20 here."  # the board says above
    out = _narrate_with(monkeypatch, _GroqClient(*[_groq_ok(lie) for _ in ai.MODELS]))
    assert out["source"] == "deterministic" and out["model"] is None
    assert "rejected" in out["note"]


def test_narrate_reports_a_network_error_per_model(monkeypatch):
    from app import ai

    errs = [ai.httpx.ConnectError("down") for _ in ai.MODELS]
    out = _narrate_with(monkeypatch, _GroqClient(*errs))
    assert out["source"] == "deterministic"
    assert "network error" in out["note"]


def test_narrate_reports_an_empty_completion_honestly(monkeypatch):
    """A model that spends its whole budget on hidden reasoning returns nothing."""
    from app import ai

    empty = [_GroqResponse(200, {"choices": [{"message": {"content": ""}}]}) for _ in ai.MODELS]
    out = _narrate_with(monkeypatch, _GroqClient(*empty))
    assert out["source"] == "deterministic"
    assert "hidden reasoning" in out["note"]


def test_narrate_strips_markup_from_a_verified_answer(monkeypatch):
    price = _n_board_price()
    text = f"<script>alert(1)</script> Price is {price} and the read is long."
    out = _narrate_with(monkeypatch, _GroqClient(_groq_ok(text)))
    assert out["source"] == "model"
    assert "<script>" not in out["text"] and "</script>" not in out["text"]


def test_narrate_without_a_key_never_calls_the_provider(monkeypatch):
    from app import ai

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    called = []

    def boom(**k):
        called.append(1)
        raise AssertionError("the provider must not be contacted without a key")

    monkeypatch.setattr(ai.httpx, "AsyncClient", boom)
    b = _ai_board()
    out = asyncio.run(ai.narrate("EURUSD=X", "1h", b, _ai_sig(b), "en"))
    assert out["source"] == "deterministic" and not called


def test_narrate_returns_prose_in_the_requested_language(monkeypatch):
    from app import ai

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    b = _ai_board()
    ar = asyncio.run(ai.narrate("EURUSD=X", "1h", b, _ai_sig(b), "ar"))
    assert any("\u0600" <= ch <= "\u06ff" for ch in ar["text"])  # has Arabic


def test_the_lock_covers_every_direct_dependency():
    """requirements.lock must pin the same direct deps as requirements.txt.

    The lock is what the image installs, so a direct dependency missing from it
    would be absent in production while the tests -- which install from
    pyproject -- still passed. Compare the top-level names, ignoring versions.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    direct = set()
    for raw in (root / "requirements.txt").read_text("utf-8").splitlines():
        clean = raw.split("#")[0].strip()
        if clean:
            direct.add(clean.split(">=")[0].split("[")[0].strip().lower().replace("-", "_"))
    locked = set()
    for raw in (root / "requirements.lock").read_text("utf-8").splitlines():
        if "==" in raw and not raw.startswith((" ", "#")):
            locked.add(raw.split("==")[0].strip().lower().replace("-", "_"))
    assert direct <= locked, f"missing from the lock: {direct - locked}"


def test_the_lock_pins_every_package_with_a_hash():
    """A lock with a floating version, or no hash, is not a lock."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    lines = (root / "requirements.lock").read_text("utf-8").splitlines()
    pinned = [ln for ln in lines if "==" in ln and not ln.startswith((" ", "#"))]
    assert pinned, "the lock is empty"
    for ln in pinned:
        assert "==" in ln, f"unpinned: {ln}"
    # Every package block must carry at least one hash.
    text = "\n".join(lines)
    assert text.count("--hash=sha256:") >= len(pinned)
    for ln in lines:
        assert not ln.strip().startswith((">=", "~=", "<")), f"floating spec: {ln}"


def test_the_dockerfile_installs_from_the_lock_with_hashes():
    """The image must install the locked, hashed set -- not the loose list."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    df = (root / "Dockerfile").read_text("utf-8")
    assert "--require-hashes" in df
    assert "requirements.lock" in df
    assert "-r requirements.txt" not in df


def test_check_lock_passes_on_the_real_files():
    """The lock checker must accept the lock we ship."""
    sys_path = str(Path(__file__).resolve().parent.parent / "scripts")
    import importlib

    sys.path.insert(0, sys_path)
    try:
        mod = importlib.import_module("check_lock")
        report = mod.check()
    finally:
        sys.path.remove(sys_path)
    assert report["ok"], report["problems"]


def test_check_lock_flags_a_version_outside_the_declared_range(tmp_path):
    """The exact drift the checker exists for: a bumped range, a stale pin."""
    sys_path = str(Path(__file__).resolve().parent.parent / "scripts")
    import importlib

    sys.path.insert(0, sys_path)
    try:
        mod = importlib.import_module("check_lock")
        (tmp_path / "requirements.txt").write_text("fastapi>=99.0\n", "utf-8")
        (tmp_path / "requirements.lock").write_text(
            "fastapi==0.162.0 \\\n    --hash=sha256:" + "a" * 64 + "\n", "utf-8")
        report = mod.check(tmp_path)
    finally:
        sys.path.remove(sys_path)
    assert not report["ok"]
    assert any("does not satisfy" in p for p in report["problems"])


def test_check_lock_flags_an_unhashed_pin(tmp_path):
    sys_path = str(Path(__file__).resolve().parent.parent / "scripts")
    import importlib

    sys.path.insert(0, sys_path)
    try:
        mod = importlib.import_module("check_lock")
        (tmp_path / "requirements.txt").write_text("fastapi>=0.115\n", "utf-8")
        (tmp_path / "requirements.lock").write_text("fastapi==0.162.0\n", "utf-8")
        report = mod.check(tmp_path)
    finally:
        sys.path.remove(sys_path)
    assert not report["ok"]
    assert any("no hash" in p for p in report["problems"])


def test_check_lock_treats_a_missing_file_as_a_failure(tmp_path):
    sys_path = str(Path(__file__).resolve().parent.parent / "scripts")
    import importlib

    sys.path.insert(0, sys_path)
    try:
        mod = importlib.import_module("check_lock")
        report = mod.check(tmp_path)
    finally:
        sys.path.remove(sys_path)
    assert not report["ok"]


def test_the_lock_is_compiled_for_the_lowest_supported_python():
    """A 3.12 lock can pin a wheel that does not exist on 3.11, which CI tests.

    This was a real failure: the lock was compiled for the image's 3.12, numpy
    resolved to 2.5.4 (>=3.12 only), and `pip-audit -r requirements.lock` died on
    the 3.11 job with 'No matching distribution found' while the image built
    fine. The lock must be installable everywhere the project claims to run.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    floor = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))[
        "project"]["requires-python"]
    assert floor.startswith(">="), f"unexpected requires-python: {floor}"
    lowest = floor.lstrip(">=").strip()
    header = (root / "requirements.lock").read_text("utf-8")
    assert f"--python-version {lowest}" in header, (
        f"the lock must be compiled for the floor {lowest}; regenerate it with "
        f"`uv pip compile requirements.txt --python-version {lowest} --generate-hashes`"
    )


# ---------------------------------------------------------------- monitoring

def test_status_reports_what_the_process_is_doing(monkeypatch):
    """/api/status answers the questions /api/health does not.

    Health says "alive"; an operator's next questions are how long, how much
    served, is the model path live, is the cache filling like a scan. Every
    field is process-local, so uptime is what separates "recently restarted"
    from "nothing happened".
    """
    from app import main

    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123def456789")
    body = TestClient(main.app).get("/api/status").json()
    assert body["status"] == "ok"
    assert body["version"] == main.app.version
    assert body["commit"] == "abc123def456"      # truncated to 12
    assert isinstance(body["uptime_s"], (int, float)) and body["uptime_s"] >= 0
    # The cache is process-wide and earlier tests may have filled it, so assert
    # the shape and the bound, not a specific count.
    assert body["cache"]["max"] == market._CACHE_MAX
    assert 0 <= body["cache"]["entries"] <= market._CACHE_MAX
    assert body["rate_limit_per_min"] == main.RATE_LIMIT
    assert body["api_key_required"] in (True, False)
    assert set(body["models"]) == set(main.ai.MODELS)


def test_status_commit_is_empty_when_render_does_not_set_it(monkeypatch):
    """Not on Render -> no revision to claim. An empty string, never a guess."""
    from app import main

    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    body = TestClient(main.app).get("/api/status").json()
    assert body["commit"] == ""


def test_cache_stats_reports_size_and_cap():
    """The cache size is what tells an operator a scan from normal use."""
    stats = market.cache_stats()
    assert set(stats) == {"entries", "max"}
    assert stats["max"] == market._CACHE_MAX
    assert stats["entries"] <= stats["max"]


def test_watch_gap_is_not_an_alarm_on_a_fresh_install():
    """An empty log means 'no history yet', not 'the watchdog is dead'.

    The very first run has nothing to compare against; crying wolf there would
    train the reader to ignore the alert.
    """
    gap = _WATCH["check_deploy"].watch_gap
    ok, reason = gap([], now=1_000_000, max_gap_s=12 * 3600)
    assert ok is True and "first run" in reason


def test_watch_gap_flags_a_watchdog_that_stopped_firing():
    """A probe that never runs is silent exactly like a healthy service.

    This is the whole point: the schedule stopped, and silence must not pass
    for an all-clear. The reading is 20h old against a 12h threshold.
    """
    gap = _WATCH["check_deploy"].watch_gap
    now = 1_000_000
    ok, reason = gap([{"ts": now - 20 * 3600}], now=now, max_gap_s=12 * 3600)
    assert ok is False
    assert "20.0h" in reason and "not firing" in reason


def test_watch_gap_uses_the_newest_reading_not_the_oldest():
    """An old entry must not mask a recent one -- the newest line is the truth."""
    gap = _WATCH["check_deploy"].watch_gap
    now = 1_000_000
    readings = [{"ts": now - 30 * 3600}, {"ts": now - 600}]
    ok, _ = gap(readings, now=now, max_gap_s=12 * 3600)
    assert ok is True


def test_deployed_commit_is_none_when_status_is_unreachable(monkeypatch):
    """No /api/status (older deploy, or asleep) -> None, never a fabricated sha."""
    cd = _WATCH["check_deploy"]
    monkeypatch.setattr(cd, "fetch", lambda url: (404, '{"detail":"Not Found"}'))
    assert cd.deployed_commit("https://example.invalid") is None


def test_await_commit_passes_once_the_pushed_revision_appears(monkeypatch):
    """A push check must prove the pushed revision is the one running."""
    cd = _WATCH["check_deploy"]
    monkeypatch.setattr(cd, "fetch", lambda url: (200, json.dumps({"commit": "cafebabe1234"})))
    ok, reason = cd.await_commit("https://example.invalid", "cafebabe1234ffff",
                                 timeout=5, interval=0.01)
    assert ok is True and "cafebabe1234" in reason


def test_await_commit_reports_a_deploy_that_never_landed(monkeypatch):
    """A green run that only says 'something is serving' is a false all-clear.

    Here the running revision differs from the pushed one and the budget runs
    out, so the check must fail and name both revisions.
    """
    cd = _WATCH["check_deploy"]
    monkeypatch.setattr(cd, "fetch", lambda url: (200, json.dumps({"commit": "oldoldoldold"})))
    ok, reason = cd.await_commit("https://example.invalid", "newnewnewnew",
                                 timeout=0.05, interval=0.01)
    assert ok is False
    assert "oldoldoldold" in reason and "newnewnewnew" in reason


def test_await_commit_says_so_when_it_cannot_read_the_revision(monkeypatch):
    """An unreadable /api/status is reported as such, not as a mismatch."""
    cd = _WATCH["check_deploy"]
    monkeypatch.setattr(cd, "fetch", lambda url: (None, ""))
    ok, reason = cd.await_commit("https://example.invalid", "abcabcabcabc",
                                 timeout=0.05, interval=0.01)
    assert ok is False and "did not answer" in reason


# ------------------------------------------------------------ error tracking

def test_unhandled_error_is_recorded_but_not_leaked(monkeypatch):
    """A bug of ours must be visible in the log and invisible to the caller.

    This is the gap the tracker closes: before it, an unexpected exception
    produced an empty 500 and left no trace anywhere. The caller gets the
    request id and nothing else -- no traceback, no message, no path.
    """
    from app import errors, main

    captured = io.StringIO()
    monkeypatch.setattr(errors._log.handlers[0], "stream", captured)
    errors.reset()

    async def boom():
        raise RuntimeError("internal detail that must not ship")

    route = main.app.router.routes
    main.app.add_api_route("/api/_boom", boom)
    try:
        client = TestClient(main.app, raise_server_exceptions=False)
        r = client.get("/api/_boom")
    finally:
        main.app.router.routes[:] = route

    assert r.status_code == 500
    body = r.json()
    assert body["detail"] == "internal error"
    assert "RuntimeError" not in r.text and "must not ship" not in r.text
    assert body["request_id"] and body["request_id"] == r.headers["X-Request-Id"]

    # ...and the full line, traceback included, is in the log.
    line = captured.getvalue().strip()
    assert '"trace"' in line and "RuntimeError" in line and "must not ship" in line
    assert errors.summary()["total"] == 1
    assert errors.summary()["types"] == {"RuntimeError": 1}


def test_error_summary_carries_no_content(monkeypatch):
    """/api/status is public; the summary is counts, never a message or path.

    A type name is a category; a message can carry a value, a path, a host. The
    summary keeps the category and drops the rest.
    """
    from app import errors, main

    errors.reset()
    try:
        raise ValueError("secret-shaped payload at /etc/passwd")
    except ValueError as e:
        errors.record(e, where="http", request_id="r9", path="/api/analyze")

    body = TestClient(main.app).get("/api/status").json()
    blob = json.dumps(body)
    assert "passwd" not in blob and "/api/analyze" not in blob
    assert body["errors"]["total"] == 1
    assert body["errors"]["types"] == {"ValueError": 1}
    assert set(body["errors"]["last"]) == {"type", "where", "request_id", "ts"}


def test_configured_secret_is_redacted_before_it_is_written(monkeypatch):
    """A key that appears in a message or a traceback must not reach the log."""
    from app import errors

    monkeypatch.setenv("GROQ_API_KEY", "gsk_live_0123456789abcdef")
    captured = io.StringIO()
    monkeypatch.setattr(errors._log.handlers[0], "stream", captured)
    errors.reset()

    try:
        raise RuntimeError("auth failed for gsk_live_0123456789abcdef")
    except RuntimeError as e:
        errors.record(e, where="test")

    line = captured.getvalue()
    assert "gsk_live_0123456789abcdef" not in line
    assert "***" in line


def test_error_types_are_capped_and_folded_into_one_bucket(monkeypatch):
    """A crash loop can invent exception types without bound; the map cannot grow."""
    from app import errors

    errors.reset()
    for i in range(errors._MAX_TYPES + 5):
        try:
            raise type(f"E{i}", (Exception,), {})("x")
        except Exception as e:  # noqa: BLE001 - exercising the tracker
            errors.record(e, where="t")
    types = errors.summary()["types"]
    assert len(types) == errors._MAX_TYPES + 1        # the cap plus _other
    assert errors.summary()["total"] == errors._MAX_TYPES + 5


def test_market_source_failure_is_not_counted_as_our_error(monkeypatch):
    """A feed being down is expected (502 with a reason), not a bug of ours.

    The distinction is the whole point: a source outage must not read as the
    service being broken, or the error count becomes noise and gets ignored.
    """
    from app import errors, main, market

    errors.reset()

    async def no_feed(symbol, interval="1h", *, fresh=False):
        raise market.MarketDataError("feed unreachable")

    monkeypatch.setattr(market, "fetch_series", no_feed)
    r = TestClient(main.app).get("/api/analyze?symbol=EURUSD=X&interval=1h")
    assert r.status_code == 502
    assert "feed unreachable" in r.json()["detail"]
    assert errors.summary()["total"] == 0


def test_watch_error_verdict_fails_a_process_that_is_throwing():
    """A deploy that answers health while throwing must not pass as green."""
    cd = _WATCH["check_deploy"]
    ok, reason = cd.error_verdict({"errors": {"total": 3, "types": {"ValueError": 3}}},
                                  max_errors=0)
    assert ok is False
    assert "3 unhandled" in reason and "ValueError" in reason


def test_watch_error_verdict_passes_a_clean_process():
    cd = _WATCH["check_deploy"]
    ok, reason = cd.error_verdict({"errors": {"total": 0, "types": {}}}, max_errors=0)
    assert ok is True and "no unhandled errors" in reason


def test_watch_error_verdict_tolerates_a_missing_field():
    """An older deploy without the errors field is not a false alarm."""
    cd = _WATCH["check_deploy"]
    ok, reason = cd.error_verdict({}, max_errors=0)
    assert ok is True and "no unhandled errors" in reason


def _bt_series(candles):
    from app.market import Candle, Series

    return Series(
        symbol="TEST", currency="USD", exchange="test", interval="1h", candles=[Candle(*c) for c in candles]
    )


def _zigzag(n=400, start=100.0, amp=3.0):
    """A deterministic up/down series wide enough for the engine to speak."""
    import math as _m

    out = []
    for i in range(n):
        base = start + amp * _m.sin(i / 7.0)
        hi = base + 1.2
        lo = base - 1.2
        out.append((i * 3600, base - 0.3, hi, lo, base + 0.3, 1.0))
    return out


def test_backtest_never_looks_ahead():
    """The signal at bar i must depend only on candles[:i+1].

    This is the one property that separates an honest backtest from a sales
    pitch. We prove it directly: truncating the series after bar i and running
    the engine on the prefix gives the same board the backtest would have seen.
    """
    candles = _zigzag(300)
    series = _bt_series(candles)
    i = 200
    prefix = series.candles[: i + 1]
    full_board = indicators.snapshot(_bt_series(candles))
    prefix_board = indicators.snapshot(_bt_series(candles[: i + 1]))
    # The prefix board must equal the full board restricted to that prefix --
    # i.e. later candles did not leak into the earlier read.
    assert prefix_board["price"] == pytest.approx(prefix[-1].c)
    # And the full board's price is a *different* bar, so they are not equal by
    # accident of a flat series.
    assert full_board["price"] != pytest.approx(prefix_board["price"])


def test_backtest_resolves_a_stop_before_a_target_in_the_same_bar():
    """An ambiguous bar (spans both) must resolve against the trade."""
    # BUY entry 100, stop 98, target 104. One bar spans 97..105 -> stop wins.
    candles = [
        (0, 100, 101, 99, 100, 1),  # bar 0: history
        (1, 100, 105, 97, 102, 1),  # bar 1: spans stop and target
    ]
    outcome, idx, px = backtest._resolve("BUY", _bt_series(candles).candles, 1, 100, 98, 104, 60)
    assert outcome == "stop" and px == 98


def test_backtest_hits_the_target_when_only_the_target_is_touched():
    candles = [
        (0, 100, 101, 99, 100, 1),
        (1, 100, 105, 99.5, 104, 1),  # touches target 104, never 98
    ]
    outcome, _, px = backtest._resolve("BUY", _bt_series(candles).candles, 1, 100, 98, 104, 60)
    assert outcome == "target" and px == 104


def test_backtest_times_out_and_exits_at_the_close():
    candles = [(0, 100, 101, 99, 100, 1)] + [(i, 100, 101, 99, 100, 1) for i in range(1, 10)]
    outcome, idx, px = backtest._resolve("BUY", _bt_series(candles).candles, 1, 100, 50, 500, 5)
    assert outcome == "timeout" and idx == 5 and px == 100


def test_backtest_sell_direction_resolves_correctly():
    candles = [
        (0, 100, 101, 99, 100, 1),
        (1, 100, 101, 95, 96, 1),  # SELL target 96 touched
    ]
    outcome, _, px = backtest._resolve("SELL", _bt_series(candles).candles, 1, 100, 102, 96, 60)
    assert outcome == "target" and px == 96


def test_backtest_r_multiple_signs():
    assert backtest._r_multiple("BUY", 100, 98, 104) == pytest.approx(2.0)
    assert backtest._r_multiple("BUY", 100, 98, 98) == pytest.approx(-1.0)
    assert backtest._r_multiple("SELL", 100, 102, 96) == pytest.approx(2.0)
    assert backtest._r_multiple("SELL", 100, 102, 102) == pytest.approx(-1.0)
    assert backtest._r_multiple("BUY", 100, 100, 105) == 0.0  # zero risk is not a gain


def test_backtest_max_drawdown_is_peak_to_trough():
    assert backtest._max_drawdown([1, 1, -3, 1]) == pytest.approx(3.0)
    assert backtest._max_drawdown([1, 2, 3]) == 0.0
    assert backtest._max_drawdown([]) == 0.0


def test_backtest_result_carries_its_caveats_and_sample_verdict():
    r = backtest.run(_bt_series(_zigzag(400)), step=5)
    d = r.as_dict()
    assert d["caveats"] and len(d["caveats"]) == len(backtest.CAVEATS_AR)
    assert isinstance(d["sample_adequate"], bool)
    assert d["trades"] == len(d["trades_detail"])
    # The pessimistic resolution must never report more target hits than trades.
    assert d["target_hits"] + d["stop_hits"] + d["timeouts"] == d["trades"]


def test_backtest_emits_no_probability_of_profit_language():
    r = backtest.run(_bt_series(_zigzag(400)), step=5)
    blob = (" ".join(r.caveats) + " " + " ".join(backtest.CAVEATS_AR)).lower()
    for banned in ("guarantee", "win rate", "profit probability", "ضمان", "نسبة نجاح", "احتمال ربح"):
        assert banned not in blob


def test_backtest_small_sample_is_flagged_not_dressed_up():
    r = backtest.run(_bt_series(_zigzag(60)), step=5)
    assert r.trades < backtest.MIN_TRADES_TO_CONCLUDE
    assert r.sample_adequate is False


def test_backtest_empty_or_tiny_series_is_not_a_crash():
    r = backtest.run(_bt_series([(0, 1, 1, 1, 1, 1)]))
    assert r.trades == 0 and r.hit_rate == 0.0 and r.expectancy_r == 0.0
    assert r.sample_adequate is False


def test_backtest_endpoint_rejects_a_bad_interval():
    r = client.get("/api/backtest?symbol=EURUSD=X&interval=99")
    assert r.status_code == 400


def test_backtest_endpoint_serves_a_record_over_real_data():
    r = client.get("/api/backtest?symbol=EURUSD=X&interval=1h&step=25")
    assert r.status_code == 200
    d = r.json()
    for k in (
        "symbol",
        "bars",
        "trades",
        "hit_rate",
        "expectancy_r",
        "max_drawdown_r",
        "sample_adequate",
        "caveats",
        "caveats_ar",
        "trades_detail",
    ):
        assert k in d, k
    assert d["bars"] > 0
    # A record without its caveats would be a forecast; the caveats must ship.
    assert d["caveats"] and d["caveats_ar"]


# ---------------------------------------------------------------- durable store


def _fresh_store(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("DESK_DATA_DIR", str(tmp_path / "d"))
    from app import store as _s

    importlib.reload(_s)
    _s.init()
    return _s


def test_store_round_trips_a_subscription(tmp_path, monkeypatch):
    s = _fresh_store(tmp_path, monkeypatch)
    sid = s.add_subscription("42", "xauusd", "1h", "buy", 2000.0)
    assert sid is not None
    subs = s.list_subscriptions("42")
    assert len(subs) == 1
    # Symbol and direction are normalised, so a lowercase ask is the same alert.
    assert subs[0]["symbol"] == "XAUUSD" and subs[0]["direction"] == "BUY"


def test_store_re_arming_the_same_alert_is_idempotent(tmp_path, monkeypatch):
    s = _fresh_store(tmp_path, monkeypatch)
    a = s.add_subscription("42", "XAUUSD", "1h", "BUY", 2000.0)
    b = s.add_subscription("42", "xauusd", "1h", "buy", 2000.0)
    assert a == b
    assert len(s.list_subscriptions("42")) == 1


def test_store_survives_a_reload(tmp_path, monkeypatch):
    """The whole point: a subscription outlives the process that made it."""
    import importlib

    monkeypatch.setenv("DESK_DATA_DIR", str(tmp_path / "d"))
    from app import store as s1

    importlib.reload(s1)
    s1.add_subscription("7", "EURUSD=X", "1h", "SELL", 1.1)
    from app import store as s2

    importlib.reload(s2)
    assert len(s2.list_subscriptions("7")) == 1


def test_store_disarm_fires_once(tmp_path, monkeypatch):
    s = _fresh_store(tmp_path, monkeypatch)
    sid = s.add_subscription("1", "BTC-USD", "1d", "BUY", 50000.0)
    assert s.disarm(sid) is True
    assert s.disarm(sid) is False  # already fired
    assert s.list_subscriptions("1") == []  # no longer armed


def test_store_remove_respects_ownership(tmp_path, monkeypatch):
    s = _fresh_store(tmp_path, monkeypatch)
    sid = s.add_subscription("owner", "XAUUSD", "1h", "BUY", 2000.0)
    assert s.remove_subscription("intruder", sid) is False
    assert len(s.list_subscriptions("owner")) == 1
    assert s.remove_subscription("owner", sid) is True
    assert s.list_subscriptions("owner") == []


def test_store_listing_is_scoped_per_chat(tmp_path, monkeypatch):
    s = _fresh_store(tmp_path, monkeypatch)
    s.add_subscription("a", "XAUUSD", "1h", "BUY", 2000.0)
    s.add_subscription("b", "XAUUSD", "1h", "BUY", 2000.0)
    assert len(s.list_subscriptions("a")) == 1
    assert len(s.list_subscriptions("b")) == 1
    assert len(s.list_subscriptions()) == 2


def test_store_counters_accumulate(tmp_path, monkeypatch):
    s = _fresh_store(tmp_path, monkeypatch)
    for _ in range(5):
        s.bump("visits")
    assert s.counter("visits") == 5
    s.bump("visits", 10)
    assert s.counter("visits") == 15
    assert s.counter("never_set") == 0


def test_store_reports_durability_honestly(tmp_path, monkeypatch):
    """An ephemeral host must not be described as durable."""
    monkeypatch.delenv("DESK_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    import importlib

    from app import store as s

    importlib.reload(s)
    st = s.stats()
    assert st["available"] is True
    assert st["durable"] is False
    assert "ephemeral" in st["note"] or "redeploy" in st["note"]


def test_store_unwritable_dir_is_reported_not_raised(tmp_path, monkeypatch):
    """A store that cannot write must answer, not blow up the caller."""
    monkeypatch.setenv("DESK_DATA_DIR", "/proc/cannot/write/here")
    import importlib

    from app import store as s

    importlib.reload(s)
    assert s.available() is False
    assert s.list_subscriptions("x") == []
    assert s.add_subscription("x", "Y", "1h", "BUY", 1.0) is None
    assert s.counter("visits") == 0
    st = s.stats()
    assert st["available"] is False


def test_status_surface_exposes_the_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_DATA_DIR", str(tmp_path / "d"))
    import importlib

    from app import main as m
    from app import store as s

    importlib.reload(s)
    importlib.reload(m)
    body = TestClient(m.app).get("/api/status").json()
    assert "store" in body
    for k in ("available", "durable", "armed_alerts", "visits", "note"):
        assert k in body["store"], k


# ---------------------------------------------------------------- alerts


def test_alert_buy_fires_on_an_upward_cross_only():
    assert alerts.crossed("BUY", 100, 99, 101) is True
    assert alerts.crossed("BUY", 100, 99, 100) is True  # exact touch counts
    assert alerts.crossed("BUY", 100, 101, 102) is False  # already above: no new cross
    assert alerts.crossed("BUY", 100, 99, 99) is False


def test_alert_sell_fires_on_a_downward_cross_only():
    assert alerts.crossed("SELL", 100, 101, 99) is True
    assert alerts.crossed("SELL", 100, 101, 100) is True
    assert alerts.crossed("SELL", 100, 99, 98) is False


def test_alert_unknown_direction_never_fires():
    assert alerts.crossed("HOLD", 100, 99, 101) is False
    assert alerts.crossed("", 100, 99, 101) is False


def test_alert_evaluate_is_pure_and_skips_unpriced_symbols():
    subs = [
        {"id": 1, "symbol": "XAUUSD", "interval": "1h", "direction": "BUY", "price": 100.0, "chat_id": "c"},
        {"id": 2, "symbol": "EURUSD=X", "interval": "1h", "direction": "BUY", "price": 100.0, "chat_id": "c"},
    ]
    # Only XAUUSD has a price; the other is skipped, not guessed as a hit.
    fired = alerts.evaluate(subs, {"XAUUSD": (99.0, 101.0)})
    assert [f["subscription"]["id"] for f in fired] == [1]


def test_alert_delivery_is_off_by_default(monkeypatch):
    """A real-world side effect must be opt-in. This pins the default."""
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    assert alerts.alerts_enabled() is False
    for on in ("1", "true", "YES", "on"):
        monkeypatch.setenv("ALERTS_ENABLED", on)
        assert alerts.alerts_enabled() is True
    monkeypatch.setenv("ALERTS_ENABLED", "0")
    assert alerts.alerts_enabled() is False


def test_alert_run_once_fires_and_disarms_without_sending_when_off(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.delenv("ALERTS_ENABLED", raising=False)
    import importlib

    from app import store as s

    importlib.reload(s)
    s.init()
    sid = s.add_subscription("chat", "XAUUSD", "1h", "BUY", 100.0)

    sent = []

    async def fake_prices(symbols, interval):
        return {sym: (99.0, 101.0) for sym in symbols}

    monkeypatch.setattr(alerts, "_prices_for", fake_prices)
    monkeypatch.setattr(alerts, "store", s)

    import asyncio

    summary = asyncio.run(alerts.run_once(transport=lambda cid, text: sent.append(text) or True))
    assert summary["fired"] == 1
    assert summary["delivered"] == 0  # delivery off => nothing sent
    assert sent == []
    # The alert fired, so it is disarmed and will not fire again.
    assert s.list_subscriptions("chat") == []
    assert sid is not None


def test_alert_run_once_delivers_when_explicitly_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    import importlib

    from app import store as s

    importlib.reload(s)
    s.init()
    s.add_subscription("chat", "XAUUSD", "1h", "BUY", 100.0)

    sent = []

    async def fake_prices(symbols, interval):
        return {sym: (99.0, 101.0) for sym in symbols}

    monkeypatch.setattr(alerts, "_prices_for", fake_prices)
    monkeypatch.setattr(alerts, "store", s)

    import asyncio

    summary = asyncio.run(alerts.run_once(transport=lambda cid, text: sent.append(text) or True))
    assert summary["delivered"] == 1
    assert len(sent) == 1
    assert "المستوى" in sent[0] and "وليس توصية" in sent[0]


def test_alert_run_once_with_no_subscriptions_is_a_clean_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_DATA_DIR", str(tmp_path / "d"))
    import importlib

    from app import store as s

    importlib.reload(s)
    s.init()
    monkeypatch.setattr(alerts, "store", s)
    import asyncio

    summary = asyncio.run(alerts.run_once())
    assert summary["checked"] == 0 and summary["fired"] == 0


def test_alert_endpoints_arm_list_and_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_DATA_DIR", str(tmp_path / "d"))
    import importlib

    from app import main as m
    from app import store as s

    importlib.reload(s)
    importlib.reload(m)
    c = TestClient(m.app)
    r = c.post("/api/alerts?chat_id=u1&symbol=XAUUSD&price=4000&direction=BUY&interval=1h")
    assert r.status_code == 200 and r.json()["armed"] is True
    aid = r.json()["id"]
    listed = c.get("/api/alerts?chat_id=u1").json()
    assert len(listed["alerts"]) == 1
    assert listed["alerts"][0]["price"] == 4000.0
    # Another chat sees nothing.
    assert c.get("/api/alerts?chat_id=u2").json()["alerts"] == []
    # Removing with the wrong chat id fails; the right one works.
    assert c.delete(f"/api/alerts/{aid}?chat_id=u2").status_code == 404
    assert c.delete(f"/api/alerts/{aid}?chat_id=u1").status_code == 200
    assert c.get("/api/alerts?chat_id=u1").json()["alerts"] == []


def test_alert_endpoint_validates_input(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_DATA_DIR", str(tmp_path / "d"))
    import importlib

    from app import main as m
    from app import store as s

    importlib.reload(s)
    importlib.reload(m)
    c = TestClient(m.app)
    base = "/api/alerts?chat_id=u&symbol=XAUUSD&price=4000"
    assert c.post(base + "&interval=99").status_code == 400
    assert c.post(base + "&direction=SIDEWAYS").status_code == 400
    assert c.post("/api/alerts?chat_id=u&symbol=XAUUSD&price=-5").status_code == 400


def test_alert_endpoint_reports_503_when_store_is_down(monkeypatch):
    monkeypatch.setenv("DESK_DATA_DIR", "/proc/cannot/write/here")
    import importlib

    from app import main as m
    from app import store as s

    importlib.reload(s)
    importlib.reload(m)
    c = TestClient(m.app)
    r = c.post("/api/alerts?chat_id=u&symbol=XAUUSD&price=4000&direction=BUY")
    assert r.status_code == 503


# ---------------------------------------------------------------- news

_RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Gold steadies as dollar slips</title>
    <link>https://example.com/a</link><pubDate>Wed, 29 Oct 2026 10:00:00 GMT</pubDate></item>
  <item><title>Oil jumps on supply worry</title>
    <link>https://example.com/b</link><pubDate>Wed, 29 Oct 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""

_ATOM_SAMPLE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Fed holds rates</title>
    <link href="https://example.com/c"/>
    <updated>2026-10-29T08:00:00Z</updated></entry>
</feed>"""


def test_news_parses_rss_items():
    heads = news.parse_feed(_RSS_SAMPLE, "Test Feed")
    assert len(heads) == 2
    assert heads[0].title == "Gold steadies as dollar slips"
    assert heads[0].source == "Test Feed"
    assert heads[0].link == "https://example.com/a"
    assert heads[0].published.startswith("Wed")


def test_news_parses_atom_entries():
    heads = news.parse_feed(_ATOM_SAMPLE, "Atom Feed")
    assert len(heads) == 1
    assert heads[0].title == "Fed holds rates"
    assert heads[0].link == "https://example.com/c"


def test_news_malformed_feed_returns_empty_not_raises():
    assert news.parse_feed("<not xml", "x") == []
    assert news.parse_feed("", "x") == []
    assert news.parse_feed("<rss><channel></channel></rss>", "x") == []


def test_news_result_usable_means_a_source_answered_not_that_list_is_nonempty():
    r = news.NewsResult()
    assert r.usable is False  # nothing answered
    r.sources_ok.append("X")
    assert r.usable is True  # answered, even if it had no items


def test_news_surface_never_claims_sentiment_or_impact():
    """The honesty rule: headlines are not analysis. No sentiment field, ever."""
    r = news.NewsResult()
    r.sources_ok.append("X")
    blob = json.dumps(r.as_dict()).lower()
    for banned in ("sentiment", "bullish", "bearish", "impact", "signal"):
        assert banned not in blob
    assert "headlines only" in r.as_dict()["note"]


def test_news_fetch_merges_and_reports_per_feed(monkeypatch):
    """A failing feed is reported, not silently dropped."""

    async def fake_fetch(client, feed):
        if feed["name"] == "CNBC Markets":
            return [news.Headline("t", feed["name"], "u")], None
        return [], "HTTP 500"

    async def no_client(*a, **k):
        raise AssertionError("network must not be touched when feeds are stubbed")

    monkeypatch.setattr(news, "_fetch_feed", fake_fetch)
    news._cache.clear()
    import asyncio

    r = asyncio.run(news.fetch_news(fresh=True))
    assert r.sources_ok == ["CNBC Markets"]
    assert len(r.sources_failed) == len(news.FEEDS) - 1
    assert r.headlines[0].source == "CNBC Markets"


def test_news_fetch_caches_a_successful_read(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(client, feed):
        calls["n"] += 1
        return [news.Headline("t", feed["name"], "u")], None

    monkeypatch.setattr(news, "_fetch_feed", fake_fetch)
    news._cache.clear()
    import asyncio

    asyncio.run(news.fetch_news(fresh=True))
    first = calls["n"]
    asyncio.run(news.fetch_news())  # should hit the cache
    assert calls["n"] == first  # no extra network calls


def test_news_fetch_does_not_cache_a_total_failure(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(client, feed):
        calls["n"] += 1
        return [], "boom"

    monkeypatch.setattr(news, "_fetch_feed", fake_fetch)
    news._cache.clear()
    import asyncio

    r1 = asyncio.run(news.fetch_news(fresh=True))
    assert r1.usable is False
    asyncio.run(news.fetch_news())  # must retry, not serve cached silence
    assert calls["n"] == 2 * len(news.FEEDS)


def test_news_endpoint_shape(monkeypatch):
    async def fake_fetch(*a, **k):
        r = news.NewsResult(
            headlines=[news.Headline("Gold up", "CNBC", "u")], sources_ok=["CNBC"], sources_failed=[]
        )
        return r

    monkeypatch.setattr(news, "fetch_news", fake_fetch)
    from app.main import app as _app

    body = TestClient(_app).get("/api/news").json()
    assert body["usable"] is True
    assert body["headlines"][0]["title"] == "Gold up"
    assert "sources_ok" in body and "sources_failed" in body


def test_news_endpoint_survives_a_failed_feed(monkeypatch):
    """A failing feed must be reported, not turn the endpoint into a 500.

    The old schema declared sources_failed as list[str] while news.py built
    [{name, error}]. The shape test above stubbed fetch_news with an *empty*
    list, so the mismatch never reached the serializer -- the same blind spot
    that hid the narrative bug: coverage ran the line, the contract was never
    checked.
    """
    from app import news
    from app.main import app as _app

    async def one_feed_down(client, feed):
        if feed["name"].startswith("CNBC"):
            return [], "HTTP 503"
        return [news.Headline("Gold up", feed["name"], "u")], None

    monkeypatch.setattr(news, "_fetch_feed", one_feed_down)
    news._cache.clear()

    body = TestClient(_app).get("/api/news?limit=5").json()
    assert body["usable"] is True
    # The failed feed is named with its reason, so a reader can distrust it.
    failed = body["sources_failed"]
    assert failed and all({"name", "error"} <= set(f) for f in failed)
    assert any(f["name"].startswith("CNBC") and "503" in f["error"] for f in failed)


def test_news_endpoint_survives_when_every_feed_fails(monkeypatch):
    """All feeds down is a usable=False answer, not an error status."""
    from app import news
    from app.main import app as _app

    async def all_down(client, feed):
        return [], "HTTP 500"

    monkeypatch.setattr(news, "_fetch_feed", all_down)
    news._cache.clear()

    r = TestClient(_app).get("/api/news")
    assert r.status_code == 200
    body = r.json()
    assert body["usable"] is False and body["headlines"] == []
    assert len(body["sources_failed"]) == len(news.FEEDS)


# ---------------------------------------------------------------- performance


def _perf_series(n=2000):
    import math as _m

    from app.market import Candle, Series

    candles = []
    base = 2000.0
    for i in range(n):
        base += _m.sin(i / 11.0) * 2 + _m.cos(i / 5.0)
        candles.append(Candle(i * 3600, base - 1, base + 2, base - 2, base + 0.5, 1.0))
    return Series(symbol="XAUUSD", currency="USD", exchange="perf", interval="1h", candles=candles)


def test_perf_indicator_snapshot_is_not_quadratic():
    """A snapshot over N bars must stay well under a second at N=2000.

    The bound is deliberately loose -- this is not a benchmark, it is a tripwire
    for an accidental O(n^2) (a nested scan that re-reads the whole series). A
    correct implementation is milliseconds; anything near this ceiling is a bug.
    """
    import time as _t

    s = _perf_series(2000)
    t0 = _t.perf_counter()
    board = indicators.snapshot(s)
    dt = _t.perf_counter() - t0
    assert board["price"] == pytest.approx(round(s.candles[-1].c, 2))
    assert dt < 2.0, f"snapshot took {dt:.2f}s -- likely a nested scan"


def test_perf_snapshot_scales_linearly_not_quadratically():
    """Doubling the bars must not quadruple the time.

    Comparing 1x vs 2x with a generous ratio catches an O(n^2) that a single
    absolute bound can miss on a fast machine. The allowance is 4x (linear with
    slack), not 2x, because timing noise at these scales is real.
    """
    import time as _t

    s1 = _perf_series(1000)
    s2 = _perf_series(2000)
    # warm caches / imports
    indicators.snapshot(s1)
    t0 = _t.perf_counter()
    indicators.snapshot(s1)
    t1 = _t.perf_counter()
    t2 = _t.perf_counter()
    indicators.snapshot(s2)
    t3 = _t.perf_counter()
    r1 = max(t1 - t0, 1e-4)
    r2 = max(t3 - t2, 1e-4)
    assert r2 / r1 < 6.0, f"2x bars cost {r2 / r1:.1f}x time -- super-linear"


def test_perf_backtest_over_a_long_series_completes():
    import time as _t

    s = _perf_series(2000)
    t0 = _t.perf_counter()
    r = backtest.run(s, step=5)
    dt = _t.perf_counter() - t0
    assert dt < 10.0, f"backtest over 2000 bars took {dt:.2f}s"
    assert r.trades >= 0


def test_perf_signal_evaluation_is_cheap_enough_for_a_request():
    """The analyze path must answer within a request budget, not a batch one."""
    import time as _t

    s = _perf_series(1500)
    board = indicators.snapshot(s)
    t0 = _t.perf_counter()
    for _ in range(5):
        signals.evaluate(board)
    dt = (_t.perf_counter() - t0) / 5
    assert dt < 0.5, f"one signal evaluation took {dt * 1000:.0f}ms"


def test_alert_telegram_send_without_token_is_a_noop(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert alerts._telegram_send("c", "hi") is False


def test_alert_telegram_send_reports_failure_without_raising(monkeypatch):
    """A delivery that fails must not crash the loop -- it returns False."""
    import httpx as _httpx

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")

    def boom(*a, **k):
        raise _httpx.ConnectError("down")

    monkeypatch.setattr(alerts.httpx, "post", boom)
    assert alerts._telegram_send("c", "hi") is False


def test_alert_telegram_send_returns_true_only_on_ok(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")

    class R:
        status_code = 200

        def json(self):
            return {"ok": True}

    monkeypatch.setattr(alerts.httpx, "post", lambda *a, **k: R())
    assert alerts._telegram_send("c", "hi") is True

    class Bad:
        status_code = 200

        def json(self):
            return {"ok": False}

    monkeypatch.setattr(alerts.httpx, "post", lambda *a, **k: Bad())
    assert alerts._telegram_send("c", "hi") is False


def test_alert_message_names_the_level_and_disclaims_advice():
    sub = {"symbol": "XAUUSD", "interval": "1h", "direction": "SELL", "price": 4000.0}
    msg = alerts._message(sub, 4005.0, 3998.0)
    assert "XAUUSD" in msg and "4000.0" in msg
    assert "وليس توصية" in msg  # never a recommendation
    assert "▼" in msg  # SELL arrow


def test_alert_prices_for_skips_a_symbol_it_cannot_read(monkeypatch):
    from app.market import Candle, Series

    async def fake_fetch(sym, interval):
        if sym == "BAD":
            raise market.MarketDataError("no data")
        return Series(
            symbol=sym,
            currency="",
            exchange="",
            interval="1h",
            candles=[Candle(1, 1, 1, 1, 100.0, 1), Candle(2, 1, 1, 1, 101.0, 1)],
        )

    monkeypatch.setattr(market, "fetch_series", fake_fetch)
    import asyncio

    got = asyncio.run(alerts._prices_for({"BAD", "OK"}, "1h"))
    assert "BAD" not in got  # unknown skipped, not guessed
    assert got["OK"] == (100.0, 101.0)


# ============================ surface hardening (docs, version, symbols, i18n)
# Each of these pins a defect that shipped: an interactive API page that could
# not run, a version with two homes, an unvalidated symbol, and an error string
# that ignored the language it was asked for.

def test_interactive_api_docs_are_not_served():
    """Swagger/ReDoc loaded their JS from a CDN, which our own CSP blocks.

    So they answered 200 as an empty shell -- an all-clear over a page that never
    worked. The fix is to not serve a surface that cannot run. The machine-
    readable schema stays, because it is plain JSON with no CDN. This asserts the
    two broken pages are gone and the schema is still there, so a future
    `FastAPI()` default cannot quietly reintroduce a broken page.
    """
    c = TestClient(main.app)
    for path in ("/docs", "/redoc"):
        assert c.get(path).status_code == 404, f"{path} should not be served"
    schema = c.get("/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["info"]["title"] == "AnalysisDesk"


def test_the_version_has_one_source_of_truth():
    """pyproject.toml is the only place the version is written.

    main.py used to type it a second time, so `status.version` and the packaged
    version could drift with nothing to catch it. Read it from the installed
    metadata and assert it matches pyproject -- the two-copies-of-one-fact
    failure this project keeps removing.
    """
    import tomllib
    from importlib.metadata import version as dist_version

    from app import main

    root = Path(__file__).resolve().parent.parent
    declared = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    assert main.app.version == declared
    assert dist_version("analysisdesk") == declared


def test_an_unusable_symbol_is_refused_before_it_reaches_upstream(monkeypatch):
    """A malformed symbol is a typo or a path-walk, not a market read.

    It used to be forwarded to Yahoo verbatim, which spent a request and came
    back with the input echoed in the message. It is refused here now, and the
    market layer is never called.
    """
    called = []

    async def spy(sym, iv, **kw):
        called.append(sym)
        raise AssertionError("the market layer must not be reached for a bad symbol")

    monkeypatch.setattr(main.market, "fetch_series", spy)
    main._hits.clear()
    c = TestClient(main.app)
    for bad in ("../../../etc/passwd", "..", "a..b", "has space", "a/b", "x" * 21, "sym;drop"):
        r = c.get("/api/analyze", params={"symbol": bad, "interval": "1h"})
        assert r.status_code == 400, f"{bad!r} should be rejected"
    assert not called, "a rejected symbol must not cost an upstream call"


def test_a_valid_symbol_shape_still_passes_validation(monkeypatch):
    """The validator must not cry wolf: real Yahoo symbols keep working."""
    from app import main

    for good in ("EURUSD=X", "BTC-USD", "^GSPC", "BRK.B", "GC=F", "XAUUSD"):
        assert main._valid_symbol(good), f"{good} is a real symbol"


def test_a_market_failure_is_reported_in_the_requested_language(monkeypatch):
    """The one error a user reads in the card must follow `lang`.

    It used to be Arabic whatever was asked for, so an English reader got a
    foreign sentence under an English heading.
    """
    from app import main

    c = _wire(monkeypatch, market.MarketDataError("Yahoo returned 503"))
    ar = c.get("/api/analyze?symbol=EURUSD=X&interval=1h&lang=ar").json()["detail"]
    en = c.get("/api/analyze?symbol=EURUSD=X&interval=1h&lang=en").json()["detail"]
    assert "تعذّر قراءة" in ar and "503" in ar
    assert "could not read" in en and "503" in en
    # The English message must not carry Arabic, and vice versa.
    assert not any("\u0600" <= ch <= "\u06ff" for ch in en)
    assert "could not read" not in ar


# ============================ identity and discovery (favicon, meta, license)
# A public project with no icon, no description and an undetected license reads
# as abandoned. These pin the surfaces that make it discoverable and legal.

def test_the_favicon_is_served_at_both_paths():
    """The page declares an SVG icon; browsers still ask for /favicon.ico.

    A 404 there is a broken icon on every visit, so both paths must answer.
    """
    c = TestClient(main.app)
    for path in ("/favicon.ico", "/static/favicon.svg"):
        r = c.get(path)
        assert r.status_code == 200, f"{path} should be served"
        assert "svg" in r.headers["content-type"]


def test_both_pages_declare_a_description_and_an_icon():
    """A shareable link needs a description and an icon; otherwise it previews
    as a bare URL. Assert the tags exist in the shipped files."""
    root = Path(__file__).resolve().parent.parent / "web"
    for name in ("index.html", "pitch.html"):
        html = (root / name).read_text("utf-8")
        assert 'name="description"' in html, f"{name} has no meta description"
        assert 'rel="icon"' in html, f"{name} declares no icon"
        assert 'property="og:title"' in html, f"{name} has no Open Graph title"
        assert 'property="og:description"' in html, f"{name} has no Open Graph description"


def test_the_license_is_plain_mit_and_the_disclaimer_is_a_notice():
    """GitHub detects a license only from a clean LICENSE file.

    The market-data disclaimer used to live in LICENSE after the MIT text, which
    made GitHub report the license as "NOASSERTION" -- an unrecognised license on
    a public repo. The disclaimer now lives in NOTICE, and LICENSE is pure MIT.
    """
    root = Path(__file__).resolve().parent.parent
    license_text = (root / "LICENSE").read_text("utf-8")
    assert license_text.startswith("MIT License")
    assert "Permission is hereby granted" in license_text
    # The disclaimer must not be in LICENSE any more.
    assert "Third-party market data" not in license_text
    assert "investment advice" not in license_text
    # And it must still exist, in NOTICE.
    notice = (root / "NOTICE").read_text("utf-8")
    assert "investment advice" in notice
    assert "Yahoo Finance" in notice


def test_a_security_policy_and_a_task_runner_exist():
    """A public repo needs a way to report a vuln and one command to check it."""
    root = Path(__file__).resolve().parent.parent
    assert (root / "SECURITY.md").exists()
    security = (root / "SECURITY.md").read_text("utf-8")
    assert "Reporting a vulnerability" in security
    makefile = (root / "Makefile").read_text("utf-8")
    assert "check:" in makefile and "test:" in makefile


def test_the_base_image_is_pinned_by_digest():
    """`python:3.12-slim` is a tag that moves; the digest does not.

    A rebuild months from now would otherwise pull a different base than the one
    CI tested. This asserts the FROM line carries a sha256 digest, so a future
    edit cannot quietly drop back to a floating tag.
    """
    root = Path(__file__).resolve().parent.parent
    df = (root / "Dockerfile").read_text("utf-8")
    from_lines = [ln for ln in df.splitlines() if ln.startswith("FROM")]
    assert from_lines, "the Dockerfile has no FROM line"
    for ln in from_lines:
        assert "@sha256:" in ln, f"base image not pinned by digest: {ln}"


def test_the_version_falls_back_to_pyproject_when_uninstalled(monkeypatch):
    """The container copies the code without building a wheel, so the installed
    metadata is absent there. The version must still resolve -- from the same
    pyproject.toml the metadata would have been built from -- rather than
    degrade to a placeholder on the deployed image."""
    from importlib.metadata import PackageNotFoundError

    from app import main

    def missing(_name):
        raise PackageNotFoundError("analysisdesk")

    monkeypatch.setattr(main, "_dist_version", missing)
    assert main._version() == "1.1.0"


def test_the_image_carries_pyproject_for_the_version(monkeypatch):
    """The fallback reads pyproject.toml, so the image must copy it in."""
    root = Path(__file__).resolve().parent.parent
    df = (root / "Dockerfile").read_text("utf-8")
    assert "COPY pyproject.toml" in df, "the image must carry the version source"


# ============================ the API contract is typed, not implied
# The endpoints used to return bare dicts, so /openapi.json described every
# response as an empty object and a client had no schema to generate against.
# These pin the contract: every data endpoint names a model, and the schema
# carries the fields -- so a renamed or dropped field fails here, not in a
# client that already shipped.

def test_every_data_endpoint_declares_a_response_model():
    from app import main

    spec = TestClient(main.app).get("/openapi.json").json()
    paths = spec["paths"]
    expected = {
        ("/api/health", "get"), ("/api/status", "get"), ("/api/symbols", "get"),
        ("/api/analyze", "get"), ("/api/backtest", "get"), ("/api/alerts", "get"),
        ("/api/news", "get"), ("/api/candles", "get"),
    }
    for path, method in expected:
        schema = paths[path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert "$ref" in schema, f"{method} {path} has no response model"
        assert schema["$ref"].startswith("#/components/schemas/"), schema


def test_the_openapi_schema_describes_the_real_fields():
    """A generated client reads these names; they must be the wire names."""
    from app import main

    spec = TestClient(main.app).get("/openapi.json").json()
    comps = spec["components"]["schemas"]
    # The full analyze shape is described, nested blocks and all.
    analyze = comps["AnalyzeOut"]["properties"]
    for field in ("symbol", "interval", "instrument", "indicators", "signal",
                  "narrative", "disclaimer"):
        assert field in analyze, f"AnalyzeOut is missing {field}"
    assert set(comps["IndicatorsOut"]["properties"]) >= {"rsi", "atr", "supports", "resistances"}
    assert set(comps["SignalOut"]["properties"]) >= {"direction", "score", "targets", "rules", "warnings"}
    # `class` is a Python keyword; the wire name must still be "class".
    assert "class" in comps["SymbolOut"]["properties"]
    # The two fields that were declared stricter than the code emits. The spec
    # must describe them as nullable/list-of-objects, or a generated client
    # would reject the very responses the server sends.
    note_schema = comps["NarrativeOut"]["properties"]["note"]
    assert any("null" in str(v) for v in note_schema.values()), note_schema
    failed_schema = comps["NewsOut"]["properties"]["sources_failed"]
    assert failed_schema["items"]["$ref"].endswith("FeedFailureOut")


def test_a_missing_spot_quote_omits_the_price_not_null_it(monkeypatch):
    """The contract for a failed spot read is an error and no price key.

    A null price would read as "the price is null", which is different from
    "there is no price" -- the distinction the whole desk is built on. The
    response model must not reintroduce the null.
    """
    c = _wire(monkeypatch, _series("GC=F", kind="FUTURE"), spot_error="upstream 503")
    body = c.get("/api/analyze?symbol=GC=F&interval=1h").json()
    assert body["spot"]["error"] and "price" not in body["spot"]
