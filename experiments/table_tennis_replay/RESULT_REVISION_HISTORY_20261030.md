# Receipt-ordered source revision history

This read-only research experiment follows one frozen cohort through several
later dated-result receipts. It does not train a model, execute a bet, verify
person identity or establish profitability.

## Contract

The existing `causal_h2h_score.evaluate` validates the frozen journal, source
body and receipt, match and price fields, scores and local chronology at each
receipt. The new `causal_h2h_revision_history` records each dated source body,
receipt and hash-linked metadata in ordered triplets, and reuses the existing
two-receipt sporting signature. Every link binds its sequence, forecast SHA,
body SHA, receipt SHA and previous link SHA. Verification requires an externally
supplied latest-link SHA and rejects a gap, an incomplete triplet, changed
bytes, unexpected files or a different head. As-of metrics are retained for
each receipt; later data never changes an earlier snapshot.

For a previously eligible fixture, later disappearance, invalidity or changed
source participants, league, status, sets, final score, last-live time or
winner creates persistent quarantine, even if a still later response reverts.
A finished-but-ineligible source row that later becomes eligible is likewise
quarantined, including after an intervening absence. An unfinished/absent row
can become eligible only at a later receipt; an `updated_at` change alone does
not create a correction if the result remains admissible. Unknown or
quarantined outcomes never become losses or training labels. Each eligible
cohort uses identical IDs for the frozen market and neutral 50/50 Brier and
log-loss metrics. The original immediate post-freeze results snapshot remains
checked by the reused scorer in addition to the dated history.

The verifier supports up to 256 dated receipts, with a single writer. An
interrupted append leaves an incomplete triplet which fails verification and
must be reviewed; there is no automatic repair. These hashes only detect
changes relative to the **supplied** independently retained latest pin. They
cannot prove that every source read or provider revision was recorded, that a
caller supplied the newest pin, that the provider is truthful, or that local
timestamps are independently trusted. Concurrent writers, unobserved
revisions and external source integrity are not qualified.

## Recorded three-receipt experiment

The [protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6510650352)
was posted at 23:08:42 UTC on 29 October before the third request. It reused
the same 11 forecasts, journal SHA-256
`407bba8c83e854636c74f857609d2954f9f5c92048ec31aff941390a15aee917`.
Both earlier receipts and their metrics were already known, so this is a
source-consistency audit, **not** an independent forecast evaluation. The
two-link head pinned before this request was
`800f7a4068bb222163ddbebe3dbb973f1027e36f56f3960c0167a50e3f1eed19`.

The third request began at 23:08:54.893121 UTC and was received at
23:09:01.016201 UTC on 29 October. Its source body SHA-256 was
`90e2cca32198790e788891d36765f3bdfe1a3aa44a1891fcd823778879417ce9`.
The provider reported `postgres`; its response had 764 rows, of which 755 were
structurally valid and nine were not finished. These are source rows, not 764
independent observations of this forecast cohort.

| Local receipt (UTC, 29 October) | Eligible | Quarantined | Unresolved |
| --- | ---: | ---: | ---: |
| 20:10:43.174312 | 1 | 0 | 10 |
| 21:09:12.293813 | 11 | 0 | 0 |
| 23:09:01.016201 | 11 | 0 | 0 |

The same 11 eligible source results survived the third receipt with no
observed correction or disappearance. The latest market Brier remained
`0.15573255403875894` against neutral `0.25`, and log loss remained
`0.4727717137443161` against `0.6931471805599453`. No new independent forecast
was added by this recheck. The new externally retainable history head is
`57d9ebb63fae22fb531f67cbe300a8f923bf70fc515f42ce3eed86a95582a483`.

At the original odds capture and latest result response, the 11 selected
fixtures have source fixture IDs and source name strings, but no top-level
home or away participant ID field. Market selection IDs are not confirmed
participant IDs; [the provider documentation](https://moviodds.com/doc) says
selection IDs belong to a match and can change. The 22 distinct name strings
are not evidence of 22 independently identified people. Indexed Setka Cup
pages state that use of their materials requires written permission; a fresh
participant-page fetch returned only a loading screen, so current terms could
not be verified. No Setka content was imported. No source entitlement, geographic classification,
identity crosswalk, verified bookmaker settlement, or accepted price was
established. Provider `finished_at` denotes last-live-seen, not an independently
verified finish time. These limitations exclude this cohort from qualified
training or real-money claims.

Synthetic regression tests exercise revision then reversion, disappearance
then return, invalid-finished then repaired, unfinished to finished, old
as-of preservation, tampering, stale pins, missing links and incomplete
writes. The raw source responses and names remain in a private Drive bundle;
no credentials or raw source data are committed to the repository.
