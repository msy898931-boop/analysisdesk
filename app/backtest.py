"""Walk-forward backtest over the desk's own signal engine.

The question this answers is the one the desk could not: *does the strategy have
a record?* Until now every figure was computed and every rule was auditable, but
nothing said whether the rules would have made or lost money. This module walks
the real candles forward and simulates each signal the engine would have emitted.

Honesty rules, same as everywhere else here:

  * **No look-ahead.** At bar `i` the engine sees `candles[:i+1]` only, exactly
    what it would have seen live. The trade is then resolved on the bars *after*
    `i`, which the signal never saw.
  * **The resolution is pessimistic on purpose.** When one bar's range covers
    both the stop and the target, the stop is counted. A backtest that assumes
    the best-case touch is a sales pitch, not a measurement.
  * **A small sample is reported as a small sample.** Below `MIN_TRADES` the
    result carries `sample_adequate=False` and says so; it is not dressed up as
    a track record.
  * **Costs are stated, not hidden.** Slippage and commission are not modelled
    (the venue and size are unknown), so the numbers are an upper bound on
    performance. That caveat travels with the result.
  * **No probability of profit is emitted.** A realised hit rate over a past
    sample is a fact about that sample, not a forecast; the wording says so.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import indicators, signals
from .market import Candle, Series

# Below this many closed trades the sample cannot support any conclusion. Thirty
# is a floor, not a good sample -- a real study wants hundreds.
MIN_TRADES_TO_CONCLUDE = 30

# A trade whose stop and target are never touched is abandoned after this many
# bars rather than held forever. The exit is the close of that bar.
TIMEOUT_BARS = 60


@dataclass
class Trade:
    index: int
    direction: str
    entry_t: int
    entry: float
    stop: float
    target: float
    exit_t: int
    exit: float
    outcome: str  # "target" | "stop" | "timeout"
    r: float  # realised R multiple (reward in units of the risk taken)
    bars: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
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
    caveats: list[str] = field(default_factory=list)
    trades_detail: list[Trade] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["trades_detail"] = [t.as_dict() for t in self.trades_detail]
        return d


CAVEATS = [
    "Historical sample only — a realised record, not a forecast. No probability of profit is implied.",
    "Slippage and commission are not modelled, so these figures are an upper bound.",
    "Resolution is pessimistic: when a bar spans both the stop and the target, the stop is counted.",
    "Only the first target is simulated; the scale-out ladder is not scored here.",
    "Walk-forward over one symbol and timeframe; a different sample can differ.",
]

CAVEATS_AR = [
    "عيّنة تاريخية فقط — سجلّ محقّق، وليس توقّعاً، ولا يُبنى عليه وعد بمكسب.",
    "الانزلاق والعمولة غير محسوبين، فهذه الأرقام حدّ أعلى.",
    "التحرير متشائم: إذا غطّت الشمعة الوقف والهدف معاً، يُحتسب الوقف.",
    "يُحاكى الهدف الأول فقط؛ سلّم الخروج التدريجي غير مُقيَّم هنا.",
    "مشي أمامي على رمز وفريم واحدين؛ عيّنة مختلفة قد تختلف.",
]


def _slice(series: Series, end: int) -> Series:
    return Series(
        symbol=series.symbol,
        currency=series.currency,
        exchange=series.exchange,
        interval=series.interval,
        candles=series.candles[:end],
        short_name=series.short_name,
        instrument_type=series.instrument_type,
    )


def _resolve(  # noqa: PLR0913, PLR0917 - a trade is defined by its own numbers
    direction: str,
    candles: list[Candle],
    start: int,
    entry: float,
    stop: float,
    target: float,
    timeout_bars: int,
) -> tuple[str, int, float]:
    """Walk bars forward from `start` until the stop or the target is touched.

    Returns (outcome, exit_index, exit_price). The stop is checked first in every
    bar, so an ambiguous bar resolves against the trade.
    """
    last = min(len(candles) - 1, start + timeout_bars - 1)
    for j in range(start, last + 1):
        c = candles[j]
        if direction == "BUY":
            if c.l <= stop:
                return "stop", j, stop
            if c.h >= target:
                return "target", j, target
        else:
            if c.h >= stop:
                return "stop", j, stop
            if c.l <= target:
                return "target", j, target
    return "timeout", last, candles[last].c


def _r_multiple(direction: str, entry: float, stop: float, exit_: float) -> float:
    risk = abs(entry - stop)
    if not risk:
        return 0.0
    if direction == "BUY":
        return (exit_ - entry) / risk
    return (entry - exit_) / risk


def _max_drawdown(rs: list[float]) -> float:
    """Largest peak-to-trough of the cumulative R curve, as a positive number."""
    peak = 0.0
    cum = 0.0
    worst = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return round(worst, 2)


def run(series: Series, *, step: int = 1, timeout_bars: int = TIMEOUT_BARS) -> BacktestResult:
    """Walk the series forward and score every signal the engine would have given.

    `step` samples a signal every `step` bars. It exists to bound the cost on a
    long series; the default scores every bar. A larger step means fewer trades,
    which the result reports rather than hides.
    """
    candles = series.candles
    trades: list[Trade] = []
    signals_seen = 0

    # Bar 0 has no history to reason about; start where the engine can speak.
    for i in range(1, len(candles) - 1, step):
        board = indicators.snapshot(_slice(series, i + 1))
        sig = signals.evaluate(board)
        if sig.direction == "NEUTRAL":
            continue
        signals_seen += 1
        entry, stop, target = sig.entry, sig.stop_loss, sig.take_profit
        outcome, exit_i, exit_p = _resolve(sig.direction, candles, i + 1, entry, stop, target, timeout_bars)
        trades.append(
            Trade(
                index=len(trades) + 1,
                direction=sig.direction,
                entry_t=candles[i].t,
                entry=entry,
                stop=stop,
                target=target,
                exit_t=candles[exit_i].t,
                exit=exit_p,
                outcome=outcome,
                r=round(_r_multiple(sig.direction, entry, stop, exit_p), 2),
                bars=exit_i - i,
            )
        )

    rs = [t.r for t in trades]
    hits = sum(1 for t in trades if t.outcome == "target")
    stops = sum(1 for t in trades if t.outcome == "stop")
    outs = sum(1 for t in trades if t.outcome == "timeout")
    n = len(trades)

    return BacktestResult(
        symbol=series.symbol,
        interval=series.interval,
        bars=len(candles),
        signals=signals_seen,
        trades=n,
        target_hits=hits,
        stop_hits=stops,
        timeouts=outs,
        hit_rate=round(hits / n, 4) if n else 0.0,
        expectancy_r=round(sum(rs) / n, 4) if n else 0.0,
        total_r=round(sum(rs), 2),
        max_drawdown_r=_max_drawdown(rs),
        avg_bars=round(sum(t.bars for t in trades) / n, 1) if n else 0.0,
        best_r=round(max(rs), 2) if rs else 0.0,
        worst_r=round(min(rs), 2) if rs else 0.0,
        sample_adequate=n >= MIN_TRADES_TO_CONCLUDE,
        caveats=list(CAVEATS),
        trades_detail=trades,
    )
