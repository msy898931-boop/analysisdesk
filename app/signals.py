"""Deterministic signal engine.

This is the honest core of the platform, and the opposite of what the site we
inspected does. Every point of the score is attached to the rule that produced
it, so a user can audit the verdict instead of trusting a black box.

Design rules:
  * No probability of profit is ever emitted. `confidence` measures agreement
    between independent indicators, nothing else.
  * A rule that cannot be evaluated (missing history) contributes nothing and
    says so, rather than silently defaulting to a friendly value.
  * Rules and warnings are bilingual, so no surface has to render a foreign
    string just because the engine only spoke one language.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .indicators import price_decimals


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(float(v))


def _r(v: float, d: int = 5) -> float:
    """Round a computed price the same way the API and the prose do.

    This used a fixed 5 decimals, so gold's stop-loss shipped as 3997.5179 while
    every other figure on the card was clean. price_decimals is the one rule.
    """
    if not _finite(v):
        return 0.0
    f = float(v)
    return round(f, min(price_decimals(f), d))


def _f(v, extra: int = 0) -> str:
    """Format a value inside a rule sentence, matching the API's precision.

    The sentences used a hardcoded .4f, so gold read 4003.5100 in the rule text
    while the API reported 4003.51. Both now ask indicators.price_decimals.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(f):
        return "0"
    return f"{f:.{min(price_decimals(f) + extra, 6)}f}"


@dataclass
class Rule:
    name: str
    vote: str  # "bullish" | "bearish" | "neutral"
    weight: int
    detail_en: str
    detail_ar: str

    def as_dict(self) -> dict:
        return {"name": self.name, "vote": self.vote, "weight": self.weight,
                "detail_en": self.detail_en, "detail_ar": self.detail_ar}


@dataclass
class Warning:
    code: str
    en: str
    ar: str

    def as_dict(self) -> dict:
        return {"code": self.code, "en": self.en, "ar": self.ar}


@dataclass
class Signal:
    direction: str
    score: float
    confidence: float
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    rules: list[Rule]
    warnings: list[Warning]


def _rule(name: str, vote: str, weight: int, en: str, ar: str) -> Rule:
    return Rule(name=name, vote=vote, weight=weight, detail_en=en, detail_ar=ar)


def evaluate(ind: dict) -> Signal:
    price = ind["price"]
    rules: list[Rule] = []
    warnings: list[Warning] = []

    required = ["price", "ema20", "ema50", "ema200", "macd", "macd_signal",
                "macd_hist", "rsi", "stoch_k", "stoch_d", "bb_position", "atr",
                "support", "resistance"]
    missing = [k for k in required if not _finite(ind.get(k))]
    if missing:
        warnings.append(Warning(
            "short_history",
            "not enough history for: " + ", ".join(missing),
            "التاريخ غير كافٍ لهذه المؤشرات: " + ", ".join(missing),
        ))

    # --- Trend: EMA stack -------------------------------------------------
    if price > ind["ema20"] > ind["ema50"]:
        rules.append(_rule("trend_ema_stack", "bullish", 2,
                           f"price {_f(price)} > EMA20 {_f(ind['ema20'])} > EMA50 {_f(ind['ema50'])}",
                           f"السعر {_f(price)} > EMA20 {_f(ind['ema20'])} > EMA50 {_f(ind['ema50'])}"))
    elif price < ind["ema20"] < ind["ema50"]:
        rules.append(_rule("trend_ema_stack", "bearish", 2,
                           f"price {_f(price)} < EMA20 {_f(ind['ema20'])} < EMA50 {_f(ind['ema50'])}",
                           f"السعر {_f(price)} < EMA20 {_f(ind['ema20'])} < EMA50 {_f(ind['ema50'])}"))
    else:
        rules.append(_rule("trend_ema_stack", "neutral", 0,
                           "EMA20/EMA50 are not stacked in a clear order",
                           "المتوسطان EMA20/EMA50 غير مرتّبين بترتيب واضح"))

    # --- Long-term filter -------------------------------------------------
    if price > ind["ema200"]:
        rules.append(_rule("long_term_filter", "bullish", 1,
                           f"above EMA200 {_f(ind['ema200'])}",
                           f"فوق EMA200 عند {_f(ind['ema200'])}"))
    else:
        rules.append(_rule("long_term_filter", "bearish", 1,
                           f"below EMA200 {_f(ind['ema200'])}",
                           f"تحت EMA200 عند {_f(ind['ema200'])}"))

    # --- Momentum: MACD ---------------------------------------------------
    if ind["macd_hist"] > 0 and ind["macd"] > ind["macd_signal"]:
        rules.append(_rule("macd", "bullish", 2,
                           f"MACD {_f(ind['macd'])} above signal {_f(ind['macd_signal'])}",
                           f"MACD عند {_f(ind['macd'])} فوق الإشارة {_f(ind['macd_signal'])}"))
    elif ind["macd_hist"] < 0 and ind["macd"] < ind["macd_signal"]:
        rules.append(_rule("macd", "bearish", 2,
                           f"MACD {_f(ind['macd'])} below signal {_f(ind['macd_signal'])}",
                           f"MACD عند {_f(ind['macd'])} تحت الإشارة {_f(ind['macd_signal'])}"))
    else:
        rules.append(_rule("macd", "neutral", 0,
                           "MACD and its signal are crossing",
                           "MACD وخط الإشارة في تقاطع"))

    # --- Momentum: RSI ----------------------------------------------------
    r = ind["rsi"]
    if r < 30:
        rules.append(_rule("rsi", "bullish", 1, f"RSI {r} — oversold", f"RSI {r} — تشبع بيعي"))
    elif r > 70:
        rules.append(_rule("rsi", "bearish", 1, f"RSI {r} — overbought", f"RSI {r} — تشبع شرائي"))
    elif 50 <= r <= 70:
        rules.append(_rule("rsi", "bullish", 1, f"RSI {r} — constructive, not stretched",
                           f"RSI {r} — بنّاء وغير مبالغ فيه"))
    elif 30 <= r < 50:
        rules.append(_rule("rsi", "bearish", 1, f"RSI {r} — soft", f"RSI {r} — ضعيف"))
    else:
        rules.append(_rule("rsi", "neutral", 0, f"RSI {r} — mid-range", f"RSI {r} — منتصف المدى"))

    # --- Stochastic -------------------------------------------------------
    if ind["stoch_k"] < 20 and ind["stoch_k"] > ind["stoch_d"]:
        rules.append(_rule("stochastic", "bullish", 1,
                           f"stoch %K {ind['stoch_k']} turning up from oversold",
                           f"ستوكاستيك %K {ind['stoch_k']} ينعطف صعوداً من تشبع بيعي"))
    elif ind["stoch_k"] > 80 and ind["stoch_k"] < ind["stoch_d"]:
        rules.append(_rule("stochastic", "bearish", 1,
                           f"stoch %K {ind['stoch_k']} turning down from overbought",
                           f"ستوكاستيك %K {ind['stoch_k']} ينعطف هبوطاً من تشبع شرائي"))
    else:
        rules.append(_rule("stochastic", "neutral", 0,
                           f"stoch %K {ind['stoch_k']} mid-range",
                           f"ستوكاستيك %K {ind['stoch_k']} في المنتصف"))

    # --- Location: Bollinger ---------------------------------------------
    pos = ind["bb_position"]
    if pos <= 0.05:
        rules.append(_rule("bollinger", "bullish", 1,
                           f"at the lower band (position {pos:.2f})",
                           f"عند الحد السفلي (الموضع {pos:.2f})"))
    elif pos >= 0.95:
        rules.append(_rule("bollinger", "bearish", 1,
                           f"at the upper band (position {pos:.2f})",
                           f"عند الحد العلوي (الموضع {pos:.2f})"))
    else:
        rules.append(_rule("bollinger", "neutral", 0,
                           f"inside the bands (position {pos:.2f})",
                           f"داخل النطاق (الموضع {pos:.2f})"))

    # --- Levels -----------------------------------------------------------
    room_up = (ind["resistance"] - price) / price
    room_down = (price - ind["support"]) / price
    if room_up > room_down * 1.5:
        rules.append(_rule("level_room", "bullish", 1,
                           f"more room up to {_f(ind['resistance'])} than down to {_f(ind['support'])}",
                           f"مساحة أكبر صعوداً حتى {_f(ind['resistance'])} من الهبوط حتى {_f(ind['support'])}"))
    elif room_down > room_up * 1.5:
        rules.append(_rule("level_room", "bearish", 1,
                           f"more room down to {_f(ind['support'])} than up to {_f(ind['resistance'])}",
                           f"مساحة أكبر هبوطاً حتى {_f(ind['support'])} من الصعود حتى {_f(ind['resistance'])}"))
    else:
        rules.append(_rule("level_room", "neutral", 0,
                           "price sits mid-range between the levels",
                           "السعر في منتصف النطاق بين المستويين"))

    if ind["candles"] < 200:
        warnings.append(Warning(
            "warmup",
            f"only {ind['candles']} candles available — EMA200 is not fully warmed up",
            f"عدد الشموع {ind['candles']} فقط — مؤشر EMA200 غير مكتمل التسخين",
        ))
    if ind["atr_pct"] > 4:
        warnings.append(Warning(
            "high_volatility",
            f"ATR is {ind['atr_pct']}% of price — unusually volatile, size down",
            f"التقلب {ind['atr_pct']}% من السعر — مرتفع بشكل غير معتاد، قلّل حجم الصفقة",
        ))

    bull = sum(r.weight for r in rules if r.vote == "bullish")
    bear = sum(r.weight for r in rules if r.vote == "bearish")
    total = sum(r.weight for r in rules)
    decided = bull + bear

    fatal = not _finite(ind.get("atr")) or ind["atr"] <= 0 or not _finite(price)
    if fatal or bull == bear or decided == 0:
        direction = "NEUTRAL"
        score = 0.0
        confidence = 0.0
        if fatal:
            warnings.append(Warning(
                "undefined_volatility",
                "volatility is undefined for this series — cannot place a stop, so no signal",
                "التقلب غير معرّف لهذه السلسلة — لا يمكن تحديد وقف خسارة، فلا إشارة",
            ))
        else:
            warnings.append(Warning(
                "no_majority",
                "indicators disagree with no majority — no trade is the honest call",
                "المؤشرات متعارضة بلا أغلبية — القراءة الأمينة هي عدم الدخول",
            ))
    else:
        direction = "BUY" if bull > bear else "SELL"
        score = round((bull - bear) / total * 100, 1)
        confidence = round(abs(bull - bear) / decided * 100, 1)

    # Stops are volatility-scaled from real ATR, not a fixed pip guess.
    atr = ind["atr"]
    min_reward = 2 * atr
    if direction == "BUY":
        stop = price - 1.5 * atr
        if ind["resistance"] - price >= min_reward:
            target = ind["resistance"]
        else:
            target = price + 3 * atr
            warnings.append(Warning(
                "thin_upside",
                f"nearest resistance {_f(ind['resistance'])} is closer than 2×ATR — "
                "target projected from volatility instead, and this trade has thin upside room",
                f"أقرب مقاومة {_f(ind['resistance'])} أقرب من ضعف ATR — "
                "حُسب الهدف من التقلب بدلاً منها، والمساحة الصاعدة ضيقة",
            ))
    elif direction == "SELL":
        stop = price + 1.5 * atr
        if price - ind["support"] >= min_reward:
            target = ind["support"]
        else:
            target = price - 3 * atr
            warnings.append(Warning(
                "thin_downside",
                f"nearest support {_f(ind['support'])} is closer than 2×ATR — "
                "target projected from volatility instead, and this trade has thin downside room",
                f"أقرب دعم {_f(ind['support'])} أقرب من ضعف ATR — "
                "حُسب الهدف من التقلب بدلاً منه، والمساحة الهابطة ضيقة",
            ))
    else:
        stop = target = price

    risk = abs(price - stop)
    reward = abs(target - price)
    rr = round(reward / risk, 2) if risk else 0.0
    if direction != "NEUTRAL" and rr < 1.5:
        warnings.append(Warning(
            "low_rr",
            f"risk/reward is only {rr} — below the 1.5 threshold most desks require",
            f"نسبة المخاطرة/العائد {rr} فقط — أقل من الحد 1.5 الذي تشترطه معظم المكاتب",
        ))

    return Signal(
        direction=direction,
        score=score,
        confidence=confidence,
        entry=_r(price),
        stop_loss=_r(stop),
        take_profit=_r(target),
        risk_reward=_r(rr, 2),
        rules=rules,
        warnings=warnings,
    )


def signal_dict(sig: Signal) -> dict:
    return {
        "direction": sig.direction,
        "score": sig.score,
        "confidence": sig.confidence,
        "entry": sig.entry,
        "stop_loss": sig.stop_loss,
        "take_profit": sig.take_profit,
        "risk_reward": sig.risk_reward,
        "rules": [r.as_dict() for r in sig.rules],
        "warnings": [w.as_dict() for w in sig.warnings],
    }
