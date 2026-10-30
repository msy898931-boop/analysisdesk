"""AI narrative layer.

The model's only job is to explain, in prose, the facts the engine already
computed. It is explicitly forbidden from introducing any new number, target,
or probability. If it cannot be reached, the desk still works: the narrative
degrades to a deterministic sentence built from the same facts.
"""

from __future__ import annotations

import json
import os
import re

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Primary first; the rest are fallbacks so one exhausted quota does not blind the desk.
MODELS = [m.strip() for m in os.getenv(
    "ANALYSIS_MODELS",
    "openai/gpt-oss-120b,qwen/qwen3.8-27b,openai/gpt-oss-20b,allam-2-7b",
).split(",") if m.strip()]
MODEL = MODELS[0]

SYSTEM_AR = """أنت محلل أسواق تكتب تفسيراً مختصراً لتقرير محسوب مسبقاً.

قواعد صارمة:
- اشرح فقط الأرقام والحقائق المذكورة في التقرير. لا تخترع أي رقم أو هدف أو نسبة.
- الحقائق مكتوبة بصيغة جاهزة مثل price_vs_ema20=ABOVE. انقلها كما هي ولا تعكسها.
  ABOVE تعني أن السعر أعلى، BELOW تعني أن السعر أدنى.
- لا تعد بأرباح ولا تذكر "ضمان" أو "نسبة نجاح" أو "احتمال ربح". الثقة هنا تعني فقط توافق المؤشرات.
- لا تعطِ نصيحة استثمارية شخصية. اذكر بوضوح أن هذا تحليل تقني آلي وليس توصية.
- اكتب 3 إلى 5 أسطر، بجمل قصيرة، بالعربية الفصحى المبسطة.
"""

SYSTEM_EN = """You are a market analyst writing a short explanation of a pre-computed report.

Strict rules:
- Explain only the numbers and facts given. Never invent a figure, target or probability.
- Facts arrive pre-resolved, e.g. price_vs_ema20=ABOVE. Quote them as-is; never invert them.
  ABOVE means price is higher, BELOW means price is lower.
- Never promise profit, never mention "guarantee", "win rate" or "probability of profit".
  Confidence here means indicator agreement only.
- Give no personal investment advice. State plainly this is automated technical analysis, not a recommendation.
- Write 3 to 5 sentences in plain English.
"""


def _fact_sheet(symbol: str, interval: str, ind: dict, sig: dict, lang: str) -> str:
    # Pre-digest every comparison into an unambiguous token. A weaker model was
    # inverting "price > EMA20" into "EMA20 is above price", so the sheet now
    # states relations as explicit facts the model only has to quote.
    def side(a: float, b: float) -> str:
        return "ABOVE" if a > b else "BELOW" if a < b else "EQUAL"

    rows = [
        f"symbol={symbol}", f"timeframe={interval}",
        f"price={_n(ind['price'])}",
        f"price_vs_ema20={side(ind['price'], ind['ema20'])} (ema20={_n(ind['ema20'])})",
        f"price_vs_ema50={side(ind['price'], ind['ema50'])} (ema50={_n(ind['ema50'])})",
        f"price_vs_ema200={side(ind['price'], ind['ema200'])} (ema200={_n(ind['ema200'])})",
        f"macd_vs_signal={side(ind['macd'], ind['macd_signal'])}",
        f"rsi={ind['rsi']}", f"stochastic_k={ind['stoch_k']}",
        f"bollinger_position={ind['bb_position']} (0=lower band, 1=upper band)",
        f"support={_n(ind['support'])}", f"resistance={_n(ind['resistance'])}",
        f"atr_pct_of_price={ind['atr_pct']}",
        f"FINAL_DIRECTION={sig['direction']}",
        f"indicator_agreement_pct={sig['confidence']}",
        f"entry={_n(sig['entry'])}", f"stop_loss={_n(sig['stop_loss'])}",
        f"take_profit={_n(sig['take_profit'])}", f"risk_reward={sig['risk_reward']}",
    ]
    for r in sig["rules"]:
        if r["weight"]:
            rows.append(f"rule {r['name']}: {r['vote'].upper()} weight={r['weight']}")
    if sig["warnings"]:
        rows.append("warnings: " + "; ".join(w["en"] for w in sig["warnings"]))
    header = ("بيانات التقرير المحسوب. استخدم هذه الحقائق حرفياً ولا تُعِد صياغة العلاقات بنفسك:"
              if lang == "ar" else
              "Computed report facts. Quote these literally; do not re-derive the relations yourself:")
    return header + "\n" + "\n".join(rows)


def _n(v, digits: int = 4) -> str:
    """Format a price for prose: enough precision, no float noise."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    a = abs(f)
    if a >= 1000:
        decimals = 2
    elif a >= 10:
        decimals = 3
    elif a >= 1:
        decimals = 4
    else:
        decimals = 5
    decimals = min(decimals, digits) if a >= 1 else decimals
    return f"{f:,.{decimals}f}"


def _summarise_failures(reasons: list[str], lang: str) -> str:
    """Turn per-model failures into one short, translated sentence.

    Four identical 'upstream returned 401' lines tell the reader nothing and read
    as noise on an otherwise clean card; the cause is what matters.
    """
    if not reasons:
        return ""
    if all(r.startswith("config:") for r in reasons):
        return ("لم يُقبل مفتاح الذكاء الاصطناعي — التفسير محسوب محلياً."
                if lang == "ar" else
                "The AI key was rejected — showing the computed explanation.")
    if all("rate limit" in r for r in reasons):
        return ("الموديلات مشغولة الآن — التفسير محسوب محلياً."
                if lang == "ar" else
                "Models are rate-limited right now — showing the computed explanation.")
    seen = list(dict.fromkeys(reasons))
    return "; ".join(seen)


def _offline_narrative(sig: dict, ind: dict, lang: str) -> str:
    if lang == "ar":
        if sig["direction"] == "NEUTRAL":
            return ("لا يوجد اتجاه واضح: المؤشرات متعارضة، والقراءة الأمينة هنا هي عدم الدخول. "
                    f"السعر {_n(ind['price'])} بين دعم {_n(ind['support'])} ومقاومة {_n(ind['resistance'])}. "
                    "هذا تحليل تقني آلي وليس توصية استثمارية.")
        verb = "شراء" if sig["direction"] == "BUY" else "بيع"
        tail = ""
        for w in sig["warnings"]:
            if w["code"] == "low_rr":
                tail = " نسبة المخاطرة إلى العائد منخفضة."
                break
        return (f"قراءة {verb} مع توافق مؤشرات {sig['confidence']}%. "
                f"السعر {_n(ind['price'])}، وقف الخسارة {_n(sig['stop_loss'])}، الهدف {_n(sig['take_profit'])} "
                f"(نسبة مخاطرة/عائد {sig['risk_reward']}).{tail} "
                "هذا تحليل تقني آلي وليس توصية استثمارية.")
    if sig["direction"] == "NEUTRAL":
        return ("No clear direction: the indicators conflict, and the honest read is to stand aside. "
                f"Price {_n(ind['price'])} sits between support {_n(ind['support'])} "
                f"and resistance {_n(ind['resistance'])}. "
                "Automated technical analysis, not investment advice.")
    verb = "long" if sig["direction"] == "BUY" else "short"
    tail = ""
    for w in sig["warnings"]:
        if w["code"] == "low_rr":
            tail = " Risk/reward is on the low side."
            break
    return (f"{verb.upper()} read with {sig['confidence']}% indicator agreement. "
            f"Price {_n(ind['price'])}, stop {_n(sig['stop_loss'])}, target {_n(sig['take_profit'])} "
            f"(risk/reward {sig['risk_reward']}).{tail} "
            "Automated technical analysis, not investment advice.")


_BANNED = ("guarantee", "guaranteed", "win rate", "probability of profit",
           "ضمان", "نسبة نجاح", "احتمال ربح", "مضمون")


def _figures(text: str) -> list[str]:
    """Pull decimal figures out of prose, treating commas as thousand separators.

    The model writes 3,982.69 for a price of 3982.69. A plain \\d+\\.\\d+ scan
    would read that as 982.69 — a number that is not in the fact set — and reject
    perfectly correct prose as an invention. Strip the separators first.
    """
    return [raw.replace(",", "") for raw in re.findall(r"\d[\d,]*\.\d+", text)]


def _sheet_text(ind: dict, sig: dict) -> str:
    """The fact sheet as the model receives it, tolerant of a partial report.

    _fact_sheet indexes several sig keys directly. The guard runs on real reports
    where they are always present, but a caller assembling a partial dict should
    get a smaller allow-list rather than a KeyError.
    """
    filled = {"rules": [], "warnings": [], "direction": "NEUTRAL", **sig}
    base = {"price": 0.0, "ema20": 0.0, "ema50": 0.0, "ema200": 0.0, "macd": 0.0,
            "macd_signal": 0.0, "support": 0.0, "resistance": 0.0, "rsi": 0.0,
            "stoch_k": 0.0, "atr_pct": 0.0, "bb_position": 0.5, **ind}
    try:
        return _fact_sheet("", "", base, filled, "en")
    except (KeyError, TypeError, ValueError):
        return ""


def _allowed_figures(ind: dict, sig: dict) -> set[str]:
    """Every decimal figure the model may legitimately quote.

    Two sources, unioned: the figures the fact sheet actually states, and the
    underlying report values. The sheet half matters — the sheet is what the
    model is told to quote, so when the guard kept its own hand-written list,
    bollinger_position was offered to the model in the sheet and then rejected
    as an invention. Deriving from the sheet means the two cannot drift apart.
    """
    allowed: set[str] = set()

    def add(v) -> None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        for s in (str(v), f"{f:.1f}", f"{f:.2f}", f"{f:.3f}", f"{f:.4f}", f"{f:.5f}"):
            allowed.add(s)

    for raw in re.findall(r"\d[\d,]*\.\d+", _sheet_text(ind, sig)):
        add(raw.replace(",", ""))

    for key in ("price", "ema20", "ema50", "ema200", "macd", "macd_signal",
                "macd_hist", "support", "resistance", "rsi", "stoch_k", "stoch_d",
                "atr_pct", "bb_position"):
        add(ind.get(key))
    for key in ("entry", "stop_loss", "take_profit", "confidence",
                "risk_reward", "score"):
        add(sig.get(key))
    return allowed


def _verify(text: str, ind: dict, sig: dict) -> str | None:
    """Return a rejection reason if the model's prose contradicts the facts.

    The engine is the source of truth; the model only narrates. If the text
    invents a number, promises profit, or states a relation backwards, we throw
    the prose away and use the computed sentence instead.
    """
    low = text.lower()
    for bad in _BANNED:
        if bad in low:
            return f"used banned profit language: {bad}"

    allowed = _allowed_figures(ind, sig)
    for tok in _figures(text):
        if tok not in allowed:
            return f"invented a figure not in the report: {tok}"

    # A relation stated backwards is the failure we actually observed.
    # Compare in the lowercased text so the Arabic wording matches too.
    if ind["price"] > ind["ema20"] and "ema20" in low:
        if "below ema20" in low or "تحت ema20" in low:
            return "said price is below EMA20 when it is above"
    if ind["price"] < ind["ema20"] and "ema20" in low:
        if "above ema20" in low or "فوق ema20" in low:
            return "said price is above EMA20 when it is below"
    return None


async def _try_model(client: httpx.AsyncClient, model: str, messages: list[dict]) -> tuple[str | None, str | None]:
    """Return (text, failure_reason). Reason is a human sentence, not a class name."""
    base = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        # Reasoning models spend tokens before emitting content, so the budget
        # must cover both; a small cap yields an empty completion.
        "max_tokens": 2500,
    }

    async def _post(body: dict):
        return await client.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY','').strip()}",
                     "Content-Type": "application/json"},
            content=json.dumps(body),
        )

    try:
        r = await _post({**base, "reasoning_effort": "low"})
        if r.status_code == 400:
            # Not every model accepts reasoning_effort; retry without it.
            r = await _post(base)
    except httpx.HTTPError as exc:
        return None, f"{model}: network error ({exc.__class__.__name__})"

    if r.status_code == 429:
        retry = ""
        try:
            detail = r.json().get("error", {})
            msg = detail.get("message", "")
            if "in " in msg and "Please try again" in msg:
                retry = " — " + msg.split("Please try again")[1].split(".")[0].strip()
        except Exception:
            pass
        return None, f"{model}: rate limit reached{retry}"
    if r.status_code == 401:
        # Not a transient failure: the key itself is rejected, so retrying the
        # other models is pointless and the reader must be told it is config.
        return None, "config: the AI key was rejected by the provider (401)"
    if r.status_code == 403:
        return None, "config: the AI key is not permitted to use this model (403)"
    if r.status_code != 200:
        return None, f"{model}: upstream returned {r.status_code}"

    try:
        msg = r.json()["choices"][0]["message"]
    except (KeyError, IndexError, ValueError):
        return None, f"{model}: unreadable response"
    text = (msg.get("content") or "").strip()
    if not text:
        return None, f"{model}: returned only hidden reasoning, no answer"
    return text, None


async def narrate(symbol: str, interval: str, ind: dict, sig: dict, lang: str = "ar") -> dict:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return {"text": _offline_narrative(sig, ind, lang), "model": None,
                "source": "deterministic", "note": "no model key configured"}

    messages = [
        {"role": "system", "content": SYSTEM_AR if lang == "ar" else SYSTEM_EN},
        {"role": "user", "content": _fact_sheet(symbol, interval, ind, sig, lang)},
    ]

    reasons: list[str] = []
    async with httpx.AsyncClient(timeout=60) as client:
        for model in MODELS:
            text, why = await _try_model(client, model, messages)
            if not text:
                reasons.append(why or f"{model}: unknown failure")
                continue
            reject = _verify(text, ind, sig)
            if reject:
                # The model contradicted the engine. Keep the engine.
                reasons.append(f"{model}: rejected — {reject}")
                continue
            return {"text": text, "model": model, "source": "model", "note": None}

    # Every model failed or contradicted the facts. Fall back to the computed sentence.
    note = _summarise_failures(reasons, lang)
    return {"text": _offline_narrative(sig, ind, lang), "model": MODELS[0],
            "source": "deterministic", "note": note}
