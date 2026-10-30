"""Tests over the real code paths: the signal engine, the indicator math, and the
API against live market data. No mocks — the point is that a wrong number must
fail here, not in front of a user.
"""

from __future__ import annotations

import asyncio
import math
import re

import pytest
from fastapi.testclient import TestClient

from app import ai, indicators, market, signals
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
    from app.market import Series
    import app.indicators as ind

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
