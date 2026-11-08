# Recorded live flag economics, frozen comparison

Owner objective: improve agent net return. Evaluate the provider's recorded
live flags on the already retained TipsAudit track-record CSV, SHA-256
`dd733c32609cd5f5bc4c2d6975a7ab2dca89559e965780e3fea90a0c24567d6e`.
This source has been inspected previously, so results are exploratory and
multiple strategy comparisons cannot be treated as untouched holdout proof.

Freeze two cohorts before running: `provider_flags` includes all qualifying live
flags from the v2 rule beginning 2026-09-30, with no daily cap. `current_rule`
applies the agent's existing p>=0.30, 5–12% price-gap, odds<=3.5 and five per
kickoff-day cap. The latter cap is applied in source-recorded-time order, then
event ID; this remains an approximation of the current live runner's batch
order. Neither cohort uses result, closing price, or later re-quote to select.

Both cohorts require unique ID, live origin, source-sealed-before-kickoff flag,
record time strictly before kickoff and at/after 2026-09-30, no exchange or
Matchbook, a valid 1/X/2 pick, valid odds and reference fair odds, and a matching
SHA-256 commitment to the seven-part source preimage. Confirm preimage selection,
bookmaker, price and recorded time match CSV fields. The source's claim of a
pre-kickoff seal is not independent timestamp or bookmaker-acceptance evidence.
Known source record corrections must not be silently rewritten.

After selection, settle using the recorded result only if the provider marks it
settled and the result is 1/X/2. Empty/ambiguous outcomes remain unresolved.
Recompute wins and unit return using the recorded odds; count provider won/profit
disagreements separately and exclude those conflicted outcomes from scoring.
No assumed cancellation or fabricated loss. Report zero, 0.5%, 1%, and 2%
stake-cost stresses, win count, ROI, and kickoff-ordered drawdown for both.

In a separate descriptive subset, compare the same eligible provider flags at
their original odds and at the provider's later same-book re-quote within 60
minutes. The later price and its existence were not known at initial decision;
this subset is conditioned on a future observation and is not a selectable
pre-decision strategy. A re-quote is not proof of an accepted order. Report
coverage and both return scenarios, with 1% stress; no invented fill or timestamp.

Fixed-unit results do not model capital overlap, tax, account limits or actual
execution; timestamps and outcome publication are not independently verified.
No model promotion, real-money execution, or profitability claim follows from
these comparisons. Attribution: TipsAudit (2026), https://tipsaudit.com/data,
CC BY 4.0; methodology https://tipsaudit.com/methodology and ledger
https://tipsaudit.com/track-record.
