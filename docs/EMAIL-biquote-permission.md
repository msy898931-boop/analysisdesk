# Email to request written permission — biquote (option أ)

The owner sends this. It is kept in the repo so the outcome is on record: if
permission is granted, the reply settles the licence question in
`docs/SOURCES.md`; if refused, the biquote entry is removed.

**To:** (see https://biquote.io — contact address on the site / terms page)
**Subject:** Permission to display biquote spot quotes inside a personal analysis app

---

Hello,

I run a small, personal, non-commercial market-analysis app (open source, MIT).
I would like to use the biquote REST API for a single purpose: to show one live
broker quote for **XAU/USD** (and XAG/USD) beside a technical chart, so a user
can compare the charted instrument to the broker's own price.

What I do **not** do, and will not do:

- No bulk download, no OHLC redistribution, no data product.
- No pass-through endpoint that returns biquote data as a feed.
- No competing market-data service.
- I credit the source on the card as "biquote (MT5)".

In short: I use the data *inside* my product for display, which your terms
describe as permitted. The only quote I surface is a single symbol's `mid`
with its `marketState` / `stale` / `quoteAgeSeconds` fields.

Could you confirm in writing that this use is acceptable under your terms?
If there is a preferred attribution string, please tell me and I will use it.

Thank you,
[owner name]
[repo URL]

---

## Status

- [ ] Sent
- [ ] Reply received
- Outcome: _pending_
