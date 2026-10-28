# Real-result paper portfolio projection

The existing forward observer records agent preferences and actual reported
results, but it does not quantify a portfolio of the preferences an agent would
have selected. Build a separate, reproducible PAPER-only projection using the
existing `PaperBook`, `TicketLeg` and `PaperRiskPolicy`. This addresses the
owner's profitability objective without changing the active research campaign,
the production book, risk limits, or the agent's recorded decisions.

Frozen assumptions: virtual starting bankroll 100 units; stake 1 unit for each
captured candidate whose *frozen at capture* preference is PRICE_GAP and passes
the canonical default paper risk policy. WAIT creates no ticket. Observed price
is assumed filled for this simulation only: no bookmaker has accepted it.
Inputs are the immutable observer state, shadow memory and their exact retained
pre-match forecasts and later source result snapshots. Recompute the original
candidate against the frozen observer plan and compare identity, selection,
price and decision time. Reconstruct the preference using only feedback ingested
before the decision; same-cycle labels are excluded. Recompute each outcome;
verify both source snapshots by SHA-256. Conflicting/corrected/missing evidence
halts the whole projection; do not silently delete losing records. No result
known after a decision affects that decision.

Replay decision and result events in chronological order; when timestamps
are equal, open decisions before settling other matches so that no unproven
cash release funds the same instant. At a recorded result, settle the canonical
virtual ticket from the actual recorded 1/X/2 result, not price movement.
Unresolved matches remain committed. Replaying the same state must give the
same totals with no duplicate positions; the book is a derived projection,
not a claim that a ticket existed at the time in a prior real account.

Expose virtual cash, committed stake, gross realized net, wins/losses and a
separate 1%-of-stake fee stress charged per opened ticket. The canonical risk
evaluation and PaperBook exclude this hypothetical fee; the stressed equity is
an informational estimate, not a fully fee-aware risk/portfolio ledger. A
separate conservative reserve check subtracts all assumed fees before opening
another ticket and can only tighten the existing risk decision. Open
stakes are valued at stake for reporting, not as profits. Quote acceptance,
timestamp independence, all costs and account restrictions remain unverified.
No model promotion or real execution is enabled. All outcomes, including loss,
are retained by the observer and memory even when the agent chose WAIT.
The research runner refreshes this derived projection after each capture. If
projection evidence is missing or conflicting, it publishes HELD and continues
public data collection; it never displays stale totals as an updated portfolio.

Local outcome-bound run: the retained public observer resumed to cycle 52 with
11 candidates, one recorded loss, no conflicts, and 346 captured forecasts.
The reconstructed portfolio had 0 tickets because all frozen preferences were
WAIT; stressed balance remained 100 units. This is a valid abstention, not an
earned profit or a real-money balance. Seventy-one focused tests passed on Linux
including actual-result wins/losses, assumed fill, fee stress and reserve,
preference causality, conflict hold and continued capture after a held projection.
An additional end-to-end fixture feeds 30 declined winning outcomes into shadow
memory, confirms the next frozen choice opens exactly one virtual ticket, then
settles that ticket as a loss and records the 1% fee. The fixture proves the
learning and accounting path works; its constructed wins are not evidence of
real-world edge or earnings.
