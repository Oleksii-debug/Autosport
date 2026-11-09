# Fixed comparison of old versus same-day history — 2026-11-09

This protocol precedes collection of the new cohort's prices and forecasts.
It is a small research comparison prompted by the first eight-match prospective
cohort, not a claim that either strategy already has a repeatable profit edge.

Hypothesis: adding genuinely observed completed matches from today improves
predictions compared with using only the archive received on the previous day.
No new model weights, stake sizing, edge threshold, or cost parameters are fitted.

## Inputs and policies

- Reuse research formulas from commit bb97bbfdef85e252c2e9e2eabd55e51a327f8021.
- Old history: the exact archive received 2026-11-08T17:34:30.533327+00:00,
  SHA256 8331e851bf2a209810824a35ae31c6264c74ce823af8822d7ca17fa01b3f9688.
- Make one read-only GET of today's `/results?sport=table_tennis&date=2026-11-09`
  and retain raw bytes, local receipt and hash. Only structurally valid completed
  results whose updates predate that receipt may be added. Reject overlapping
  fixture IDs between the old archive and the new valid results, rather than
  silently counting a match twice or resolving a correction by guesswork.
- Both history states use the SAME feature cutoff, after receiving that results
  response and before receiving market prices. Thus the 30-day H2H window has
  the same endpoint in both arms. The difference is the added observed matches.
  The missing late-November-8 interval in the old archive is shared by both arms;
  this experiment does not claim a complete refreshed archive.
- Seven fixed rules: market; old-archive Elo, H2H and combined anchors; and the
  corresponding three anchors with today's observed results added. Market
  probabilities and quoted prices are identical between the compared arms.
- The market/history weights, Elo support rules, 30-day H2H smoothing, 3% minimum
  estimated edge, one-unit hypothetical stakes, 0.01 ticket cost, and 2% winning
  payout haircut remain unchanged. Stress uses a 5% payout haircut on the SAME
  tickets. Each policy is a separate PAPER portfolio; their correlated profits
  must not be added together as an agent's realized income.

## Prospective selection and commitment

After the source response and feature states, make one read-only market GET.
Select at most 16 eligible fixtures in scheduled-start / fixture-ID order,
eight to twenty-five minutes after the frozen decision time. Use the same five
table-tennis leagues. Require pre-match state, exact source-side identities,
two unsuspended unambiguous prices from one winner market, a market-group poll
within 120 seconds of server time, and server/local receipt clocks within 30
seconds. Provider documentation defines absent `suspended` as false and emits
`stale` only when true. No history ID may appear in the forecast cohort.

Freeze all seven probabilities and actions before any selected match starts;
publish the file hash and acquisition-script hash before the earliest start.
Do not replace matches or change forecasts after viewing outcomes. If there are
fewer than 16 eligible matches, retain that smaller cohort and report its size.

## Follow-up and interpretation

Use bounded read-only follow-up within this active session, with exact source
fixture IDs, unchanged league/name orientation, complete valid best-of-five
results, receipt hashes and chronology. Conflicts or missing results remain
unresolved; do not score them as losses or wins. Follow-up may end after all
selected results arrive or two hours after the latest scheduled start. No new
scheduled task, continuous background service, order, or real wager is created.

Report Brier/log loss for the SAME resolved cohort, and each rule's ticket count,
wins, net P&L, stress P&L and drawdown. Show old-versus-updated forecast/action
differences, not only the better arm. The sample is small, dependent and drawn
from one source: even a positive result does not establish profitability or
authorize production promotion. Historical application of these same formulas
to the previously inspected 130-match sample is exploratory only, not a fresh
holdout or a basis for retuning this cohort.
