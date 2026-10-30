# Overlapping recorded-session PAPER replay

This is research-only code for the open overlap limitation in the preceding
[shared-book replay](JOINT_FORWARD_PORTFOLIO_20261030.md). The [analysis plan](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6518158516)
was recorded before the comparison. Existing source sessions and their
outcomes were already inspected. There is no new live observation, independent
holdout, trained model, accepted order, or profitability claim.

## Scope and chronology

`interleaved_forward_portfolio.replay` reuses the frozen-action and dated
revision-history validators, as well as the preceding adapter's PAPER book,
risk, opening and settlement machinery. The original decisions were frozen in
separate books; rechecking them against one shared book per alternative is a
retrospective counterfactual, not evidence that the common-book decisions were
actually made. Each alternative starts at 100 assumed units, with one-unit
stakes and 0.01 assumed cost per opening. The alternatives are not additive.

All validated session action-freeze times, generic result-receipt times and
dated result-receipt times are merged. Only receipts already processed can
affect a decision; at an identical time, actions precede receipt processing.
If more than one session has an action at that instant, frozen rejections are
kept and attempted openings are blocked instead of inventing an order in which
to spend the shared bankroll. A known prior exact result ID blocks opening; a
selected exact ID reused across sessions is blocked rather than merged. No
other leg is substituted. Different source IDs can still be aliases for the
same real match, and the same string can be ambiguous. The exposure counter
therefore measures identical source strings only, not physical-match overlap
or independence.

Only a session's own validated dated history can settle its ticket. Results
from another session provide adverse evidence for an exact matching ID, not
independent proof of an identity or sufficient authority to settle it. A
later duplicate, invalid or conflicting positive record quarantines the ID.
An absence from an unrelated session is not assumed to be a correction; the
existing dated-history rules do quarantine a previously eligible record that
disappears from its own tracked history. Same-time receipt batches are checked
for conflicts before any ticket is settled. A quarantined pending leg remains
pending; if an already settled ticket is affected, the whole alternative book
is held and current monetary and win/loss/pending figures are withheld. Prior
as-of records are retained, and later actions are refused until manual review.

The underlying source describes `finished_at` as its last live observation,
not independently verified match completion. Existing conservative checks
still require the source's time to be no earlier than the subsequent result
request. The new code additionally inspects adverse source evidence from the
actual frozen opening onward, not merely from that later request.

## Constructed overlap and regression

In one synthetic test, session A opens at 43 and 83 seconds and session B at
103 and 143 seconds; the first dated result arrives at 160 seconds. Four
assumed tickets are therefore open concurrently on one shared book. Peak
open tickets is four; peak exact-ID sharing is two because each session has
two tickets on its own pair. The fixture also checks a win/loss mixture,
fees and the ending balance. A separate tightened **test-only** committed
stake cap rejects later openings based on existing shared exposure; the real
recorded replay uses the default risk policy. Constructed values are not
observed matches or evidence of return.

A synthetic [falsifier](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6518299075)
also exposed an initial prototype error: a conflicting row arrived at 43.5,
after an action frozen at 43 but before that session's next result request at
44. The reused per-session check began at 44 and alone could miss this row.
The revised interleaving audit keeps that 44-second settlement bound while
also checking adverse evidence from 43; the ticket remains pending and the
conflicting exact ID is quarantined. A matching earlier unfinished row is not
automatically treated as a sporting correction.

## Existing data and verification

The previous two externally pinned histories were replayed as a regression
check. Their session windows remain non-overlapping, and the interleaved
runner's final PAPER policy states matched the earlier bounded replay. No
recorded cross-session concurrent ticket was thereby observed. The two earlier
source ID sets did not have exact overlap; no source-to-person, alias or
physical-match identity was independently verified. The source data, times,
prices, rights and output remain subject to the earlier report's limitations.

Nineteen new synthetic tests cover overlapping open stake and shared-ID
exposure, shared risk, future-result isolation, action/receipt ties,
simultaneous action ambiguity, reused source IDs, prior results, the
freeze-to-request interval, later duplicate/invalid/conflicting records,
missing own-history results, same-time correction batches, source absence,
and pinned-history tampering. The Linux/Python 3.12 research/identity suite
passed **303 tests**, and syntax compilation passed. GitHub CI must be reported
separately for the exact published head.

The new replay still cannot infer aliases between different IDs, detect
unrecorded responses or corrections, verify an exact external clock, confirm
official participant or fixture identity, grant data rights, establish an
accepted bookmaker price, or certify settlement. Inputs are bounded by the
recorded response histories and their supplied pins; a pin is not proof of
freshness or completeness. No data entered a production identity/learning
store, and no real bets were made.
