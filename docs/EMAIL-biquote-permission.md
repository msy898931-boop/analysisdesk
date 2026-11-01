# Email to request written permission — biquote (option أ)

The owner sends this. It is kept in the repo so the outcome is on record: if
permission is granted, the reply settles the licence question in
`docs/SOURCES.md`; if refused, the biquote entry is removed from
`BIQUOTE_SYMBOLS`.

**To:** `support@biquote.io` — **cc:** `halitcandogan@gmail.com`
(the author's address, linked from `biquote.io/terms.html`; the free tier has
"no support obligation", so a reply may take time. If nothing comes back in ~2
weeks, that silence is itself the answer — leave biquote as-is or fall back.)

---

## Copy everything below this line into your mail client

Subject: Permission to display biquote spot quotes inside a personal analysis app

Hello,

I run a small, personal, non-commercial market-analysis app (open source, MIT):
https://github.com/msy898931-boop/analysisdesk

I would like to use the biquote REST API for one purpose: to show a single live
broker quote for XAU/USD (and XAG/USD) beside a technical chart, so a user can
compare the charted instrument to the broker's own price. I credit the source on
the card as "biquote (MT5)".

What I do NOT do, and will not do:

- No bulk download, no OHLC redistribution, no data product.
- No pass-through endpoint that returns biquote data as a feed.
- No competing market-data service.
- The only field I surface is a single symbol's mid, together with your
  marketState / stale / quoteAgeSeconds fields, so a closed market reads as
  closed rather than as a live price.

In short: I use the data inside my product for display, which your terms
describe as permitted.

Could you confirm in writing that this use is acceptable under your terms? If
there is a preferred attribution string, please tell me and I will use it.

Thank you,
<your name>
https://github.com/msy898931-boop/analysisdesk

---

## Status

- [ ] Sent
- [ ] Reply received
- Outcome: _pending_
