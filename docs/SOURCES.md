# Data sources and their terms / المصادر وشروطها

Every number on the card comes from a named source. This file records **which
sources, what they cost, and what their licence allows** — so a licence
question is answered here rather than guessed at in code.

كل رقم على البطاقة له مصدر مسمّى. هذا الملف يسجّل المصادر وشروطها، ليكون
سؤال الترخيص مُجاباً هنا لا مُخمَّناً في الكود.

## Candle sources (keyless)

| Order | Source | What it serves | Licence |
|---|---|---|---|
| 1 | **Kraken** (`api.kraken.com`) | OHLC for all timeframes, ~7y history | Public API, free |
| 2 | **Coinbase** (`api.exchange.coinbase.com`) | Fallback; daily aggregated to weekly | Public API, free |

Both are public exchange endpoints. No key, no signup. Used for the *chart*.

## Spot quote chain (keyless)

Fetched for the *live* broker quote shown beside the chart, in this order:

| Order | Source | Why | Licence |
|---|---|---|---|
| 1 | **biquote** (`biquote.io`) | States its own `marketState` / `stale` / `quoteAgeSeconds` — a closed market reads as closed | Free, no key. **See below.** |
| 2 | **Swissquote** (`forex-data-feed.swissquote.com`) | Real broker book | Public feed; terms silent |
| 3 | **gold-api** (`api.gold-api.com`) | Aggregate, last resort | Free, no key |

Any single upstream outage degrades down the chain; if all fail, the card says
so and shows no substitute number.

## biquote licence — the constraint that matters

biquote's [terms](https://biquote.io/terms.html) allow commercial and
non-commercial use, free, with no key. The one restriction, verbatim:

> "Don't resell or redistribute the raw feed. Do not expose biquote quotes or
> OHLC data through your own public API, offer bulk downloads of it as a data
> product, sublicense it, or operate a competing market-data service on top of
> it. Using the data *inside* your product is fine; acting as a *data vendor* of
> our feed is not."

**Our use.** The `spot` field surfaces **one broker quote for display**, not the
feed: no bulk download, no OHLC redistribution, no pass-through endpoint. This
is the "inside your product" case. We do **not** use biquote for the candles.

Two fields are traps and are never used: `last` and `volume` are **always 0**
on this feed (broker CFD pricing has no consolidated tape). The price is `mid`.

**Action taken.** Because the boundary is a judgement call, written permission
was requested from the biquote team — the ready-to-send email is
`docs/EMAIL-biquote-permission.md` (to `support@biquote.io`, cc the author's
address on their terms page). If they decline, the chain falls back to
Swissquote and biquote is removed — the code path is one entry in
`BIQUOTE_SYMBOLS`.
