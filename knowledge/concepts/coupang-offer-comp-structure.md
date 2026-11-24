---
slug: coupang-offer-comp-structure
verdict: PROMOTE
reviewed: "approved by Andy in the review queue (item 018e24, 2026-11-24T15:11:44Z)"
reason: 3-vote gate (3P/0Q/0R) -> PROMOTE; majority promote, no reject
checks_triggered: []
gate_votes: [PROMOTE, PROMOTE, PROMOTE]
compiled_at: 2026-11-21T11:01:20Z
compiler_version: "1.3"
sources:
  - agent: loid
    session_id: 4e991561-1d61-4aef-8a45-cf411340e70e
    transcript_sha256: 65e7a402549e77f7c6460118d93ae0491af3a1bc1060c2364e8d40863bc8e308
    source_log: daily-logs/loid/2026-11-21.md
    session_n: 7
---

# coupang-offer-comp-structure

> Coupang's ~550k year-one offer is cash-weighted: base plus a time-limited salary supplement plus bonus carry most of it, while the 350k equity grant (the LTI) vests back-loaded, so only ~10% shows up in year one.

## Why this matters

Reading this offer by its 550k headline leads to two predictable sizing errors, and both change the advice we give Andy. The year-one total is propped up by a salary supplement that only runs in years 1-2, not by the equity — so the number is front-loaded in a way that quietly reverses later. The 350k LTI vests 10/10/40/40, meaning 280k of it lands in years 3-4, exactly as the ~241k supplement drops off. That hand-off is the whole decision: the comp doesn't degrade smoothly, it shifts from guaranteed cash to equity that requires staying.

## Key points

- Year-one breakdown: base ~241k + salary supplement ~241k (years 1-2 only) + bonus ~48k entered / ~24k at target + year-one equity ~35k (10% of the 350k grant). Sums to ~565k at 48k bonus, ~541k at 24k target.
- The LTI *is* the 350k equity grant, not a separate stock line. Only ~35k (10%) vests in year one.
- Sizing trap one: do not back the LTI out as the plug to reach 550k — that undersizes it to ~20k.
- Sizing trap two: do not add an annualized 350k/4 (~87k) on top of the 550k — the year-one tranche is already inside the headline, so this double-counts.
- Grant/supplement hand-off: 280k of the grant (40/40) vests in years 3-4, the same window the ~241k supplement ends. The guaranteed-cash cushion and the equity payoff trade places at year three.

## How we use it

When `loid` models this offer or compares it against alternatives, the year-one 550k and the steady-state comp are different numbers and both get stated. The supplement's two-year window and the 10/10/40/40 vest schedule are the inputs that separate them; collapsing them into one annual figure is where the two traps above come from.

## Open questions

- Whether the ~280k LTI forfeiture at year three changes Andy's Korea move. Flagged in-session as Yor's call, not resolved here.

## Related concepts

- [[offer-comparison-framework]]
- [[equity-vesting-schedules]]
- [[korea-relocation-decision]]
