# Chronological replay of the shadow learner on source-recorded signals

The 69-signal source subset was already inspected and selected retrospectively.
Test, as a diagnostic only, whether the *existing* shadow learner would have
accepted signals as information became available. This is not an untouched
holdout, and its results cannot establish or promote economic advantage.

Reconstruct the previously defined ordered-source-time `current_rule` cohort
from the immutable TipsAudit CSV. Process source-recorded decisions in timestamp
and ID order. An outcome is assumed available 48 hours after kickoff; this is
not a verified publication time. Before each decision, deliver only past labels
whose assumed availability is no later than that decision. Choose using the
unchanged `ShadowStrategyMemory` (100-event window, minimum 30 observations,
minimum mean return 2%, 1% cost stress). After each decision queue its outcome,
including when the choice was WAIT, so abstention does not prevent learning.
Pending or conflicting source outcomes never train or count as settled returns.

Compare two fixed-unit groups: all current-rule signals and the learner's selected
signals. Report all
choices and net outcomes with 0% and 1% costs. Source first-flag timing differs
from the current API, recorded prices are not accepted fills, capital overlap is
not modeled, and no independent timestamps or actual order execution are proven.
Retain the result regardless of direction. Attribution: TipsAudit (2026),
https://tipsaudit.com/data, CC BY 4.0.
