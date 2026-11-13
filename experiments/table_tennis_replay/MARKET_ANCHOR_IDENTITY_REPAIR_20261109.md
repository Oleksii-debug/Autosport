# Source-linkage defect found before scoring — 2026-11-09

During collection for the frozen market-anchor screen, two provider alias groups were present under both requested IDs. The saved result archive showed separate meetings: one pair had different orientations, different set scores and last-live timestamps roughly 2.5 hours apart; another pair had different results and last-live dates. The closing response reused one scheduled kickoff and pre-match record across the alleged twins.

This is concrete evidence that a provider-claimed prematch/live link is not sufficient to price repeated meetings. The original exact-identity admission requirement is therefore enforced conservatively: if another ID in the closing alias group has any separate row in the result archive, reject that closing response with alias_has_separate_result. Do not merge those result rows or invent a corrected price. This exclusion is based on conflicting event identity, before any aggregate strategy returns are computed. The 240 selected IDs, policies, coefficients, thresholds, date split and costs are unchanged; excluded cases are not replaced.

A regression test demonstrates rejection of an alleged linked twin with its own result. Original source responses remain private. Other unresolved alias errors can still exist; no confirmed event/person identity or executable price is claimed.
