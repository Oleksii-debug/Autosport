# Prospective table-tennis PAPER comparison — 2026-11-13

This is a bounded continuation of research PR #2592 / issue #2591, outside
closed production Sections. Primary comparison: unchanged H2H versus market;
Elo, the fixed combination, both November 10 residual models and their existing
sensitivity gates are controls. No parameter, threshold or league tuning.

Capture one current read-only table-tennis results archive, then one current
winner-market board. History uses only structurally valid source results with
finish and update strictly before the actual local archive receipt. Exact
league/name strings are research proxies. Exclude every result ID in the archive
and both models' training IDs from testing. No results fetched after the board
may enter features. This refreshes source history, not model weights.

Take up to 32 fixtures in scheduled kickoff/ID order, 4–22 minutes after the
board receipt and within its UTC date. Use the same five leagues. Require a
unique prematch fixture, no live score, a fresh group-920000 poll within 120
seconds of source clock, local clock within 30 seconds of source clock, and a
single unsuspended selection per exact side in one winner market. No selection
by probability, history support, quote level, or hypothetical ticket. Keep all
abstentions and exclusions. Stop before results if no fixture qualifies.

Publish the hash of the full private forecast artifact before its earliest
scheduled kickoff. Keep the exact forecast, source bodies, receipt clocks and
model bytes. A failure to publish in time must be disclosed and does not qualify
as an externally precommitted test. One-unit stakes, 0.01 per-ticket cost, 2%
payout haircut and 3% minimum estimated net edge; 5% haircut stress on the same
actions. Preserve all eight separate decision rules and six probability models.

After the last scheduled kickoff plus 20 minutes, read one date-filtered results
response. If any rows remain unresolved, allow one further response after the
last scheduled kickoff plus 35 minutes. A valid completed score, chronological
source timestamps, exact ID and exact oriented labels are required. Do not infer
aliases from spelling, price or score. Retain pending and conflicting rows, and
do not count them as losses, wins or returned stakes. Reproduce all forecasts
from saved inputs and reconcile frozen decisions before settlement. Compare
Brier/log loss on the same resolved events and publish every rule's net, stress,
drawdown and unresolved actions, including zero-ticket policies. One sample and
possible shared-player dependence cannot demonstrate durable profitability.

No account, real bet, bookmaker fill claim, scheduled task or production-memory
change. Captured odds and source-reported scores are research observations only.
