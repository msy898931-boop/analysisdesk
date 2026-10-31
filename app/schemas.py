"""Typed response contracts for the API.

The endpoints returned plain dicts, so `/openapi.json` described them as bare
objects and a client had no schema to generate against -- the contract lived
only in prose in README.md, and prose cannot be checked. These models make the
shape explicit, and FastAPI then validates every response against them: a field
that is added, renamed, or dropped fails a test instead of shipping silently.

Each model mirrors what the code already returns; nothing here computes a value.
Optional fields are the ones that are genuinely absent on some path (a missing
spot quote, an anchor that was refused), never a field that merely might be
empty -- an empty list is still a list.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HealthOut(BaseModel):
    status: str
    model: str
    model_key: bool


class CacheInfo(BaseModel):
    entries: int
    max: int


class ErrorInfo(BaseModel):
    total: int
    types: dict[str, int]
    last: dict[str, Any] | None
    since: float


class StoreInfo(BaseModel):
    available: bool
    path: str
    durable: bool
    note: str
    armed_alerts: int
    visits: int


class StatusOut(BaseModel):
    status: str
    version: str
    commit: str
    uptime_s: float
    served: int
    model: str
    model_key: bool
    models: list[str]
    cache: CacheInfo
    rate_limit_per_min: int
    api_key_required: bool
    tracked_clients: int
    errors: ErrorInfo
    store: StoreInfo


class SymbolOut(BaseModel):
    # `class` is a Python keyword, so the field is named with a trailing
    # underscore and carries the wire name as an alias. populate_by_name lets the
    # model accept the aliased key on input; FastAPI serializes by alias, so the
    # response still says "class".
    model_config = ConfigDict(populate_by_name=True)

    symbol: str
    name_ar: str
    name_en: str
    class_: str = Field(alias="class")
    note_ar: str | None = None
    note_en: str | None = None


class IntervalName(BaseModel):
    ar: str
    en: str


class SymbolsOut(BaseModel):
    symbols: list[SymbolOut]
    intervals: dict[str, IntervalName]


class SpotOut(BaseModel):
    metal: str
    price: float | None = None
    source: str | None = None
    error: str | None = None


class AnchorOut(BaseModel):
    ok: bool
    reason: str | None = None
    factor: float | None = None
    proxy_price: float | None = None
    spot_price: float | None = None
    pct: float | None = None
    source: str | None = None
    max_pct: float | None = None


class InstrumentOut(BaseModel):
    kind: str
    name: str
    exchange: str
    provider: str


class IndicatorsOut(BaseModel):
    price: float
    prev_close: float
    change_pct: float
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    ema20: float
    ema50: float
    ema200: float
    bb_upper: float
    bb_mid: float
    bb_lower: float
    bb_position: float
    stoch_k: float
    stoch_d: float
    atr: float
    atr_pct: float
    support: float
    resistance: float
    supports: list[float]
    resistances: list[float]
    candles: int


class TargetOut(BaseModel):
    index: int
    price: float
    rr: float
    kind: str
    source_en: str
    source_ar: str


class RuleOut(BaseModel):
    name: str
    vote: str
    weight: int
    detail_en: str
    detail_ar: str


class WarningOut(BaseModel):
    code: str
    en: str
    ar: str


class SignalOut(BaseModel):
    direction: str
    score: float
    confidence: float
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    targets: list[TargetOut]
    rules: list[RuleOut]
    warnings: list[WarningOut]


class NarrativeOut(BaseModel):
    text: str
    # null when the AI layer did not write it (no key, a bad key, a rate limit):
    # the desk names the model only when a model wrote the prose.
    model: str | None = None
    source: str
    # null when a model wrote the prose (there is nothing to explain), a string
    # when the deterministic fallback ran and owes the reader a reason. Typed
    # `str` before, which the model-success path -- note=None in ai.narrate --
    # failed validation on, turning every successful read into a 500. The
    # success path is the one path no API-level test drove.
    note: str | None = None


class AnalyzeOut(BaseModel):
    symbol: str
    currency: str
    exchange: str
    interval: str
    as_of: int
    instrument: InstrumentOut
    spot: SpotOut | None = None
    anchor: AnchorOut | None = None
    indicators: IndicatorsOut
    signal: SignalOut
    narrative: NarrativeOut
    disclaimer: str


class TradeOut(BaseModel):
    index: int
    direction: str
    entry_t: int
    entry: float
    stop: float
    target: float
    exit_t: int
    exit: float
    outcome: str
    r: float
    bars: int


class BacktestOut(BaseModel):
    symbol: str
    interval: str
    bars: int
    signals: int
    trades: int
    target_hits: int
    stop_hits: int
    timeouts: int
    hit_rate: float
    expectancy_r: float
    total_r: float
    max_drawdown_r: float
    avg_bars: float
    best_r: float
    worst_r: float
    sample_adequate: bool
    caveats: list[str]
    trades_detail: list[TradeOut]
    caveats_ar: list[str]


class HeadlineOut(BaseModel):
    title: str
    source: str
    link: str
    published: str


class FeedFailureOut(BaseModel):
    # A failed feed is named and given a reason, not collapsed to a bare string:
    # "CNBC Markets: HTTP 503" tells the reader which source to distrust, and a
    # list of strings would throw the feed name away. The schema said list[str]
    # while news.py built these objects, so any failing feed 500'd the endpoint.
    name: str
    error: str


class NewsOut(BaseModel):
    headlines: list[HeadlineOut]
    sources_ok: list[str]
    sources_failed: list[FeedFailureOut]
    usable: bool
    note: str


class AlertOut(BaseModel):
    id: int
    chat_id: str
    symbol: str
    interval: str
    direction: str
    price: float
    created_at: int
    armed: int


class AlertsOut(BaseModel):
    alerts: list[AlertOut]
    delivery_on: bool


class CandleOut(BaseModel):
    t: int
    o: float
    h: float
    l: float
    c: float


class CandlesOut(BaseModel):
    symbol: str
    interval: str
    candles: list[CandleOut]
