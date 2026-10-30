# Two-receipt reconciliation of frozen in-play forecasts

This is research-only, not an executable strategy. It compares the **same 11**
previously frozen source fixture IDs and market-implied home probabilities at
two later result receipts. The H2H feature was unavailable for all 11 because
the local identity registry was empty. No model was trained.

## Policy and lineage

The [reconciliation protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6508373531)
was recorded before the second dated-results request. The forecast journal
SHA-256 is `407bba8c83e854636c74f857609d2954f9f5c92048ec31aff941390a15aee917`.
The earlier result body SHA-256 is
`dc1e6c4b31d9499062ee1bfa52cc394b8530c9464807bd45339646c9e61add18`, received
at 20:10:43.174312 UTC. The new request started at 21:09:03.587783 UTC and
received its response at 21:09:12.293813 UTC. Its body SHA-256 is
`93fac61b09231f511e6a1ea051f4504981687a21034fffda6a1491d90bf74094`.
The source reported `postgres` for both responses; the newer response contained
700 rows, including 691 structurally valid and nine not finished. Capture and
receipt times are local observations, not an independently trusted clock.

`causal_h2h_longitudinal.py` reuses the frozen-journal and dated-result
verification and paired scoring modules. It preserves the earlier report and
partitions all frozen IDs into unchanged eligible, newly eligible, formerly
eligible but missing, quarantined, and unresolved. A changed participant,
league, final status, set score, final score or last-live timestamp, or an
earlier finished but ineligible result that is later repaired, is quarantined;
an absent result is unknown. A change to `updated_at` alone is allowed if the
result remains otherwise admissible. Current Brier and log loss use the same
admitted IDs for both baselines; no missing or conflicting label is imputed.

This implementation compares **exactly two supplied receipts**. It cannot prove
there was no intervening provider correction. Do not chain it while discarding
earlier receipts or treat a later two-receipt comparison as full revision
history; a multi-receipt history requires separate retention and quarantine
handling. Chronology is based on local receipt times, and neither the feed nor
its hashes prove the external source's truth.

## Observations

At the first follow-up, one frozen ID was eligible and ten were not present.
At the later follow-up the reconciliation yielded one unchanged eligible and
ten newly eligible, zero missing formerly eligible, zero quarantined, and zero
unresolved. These categories partition the 11 frozen IDs. The earlier as-of
result is preserved: market Brier `0.0072009767414818885` and 50/50 Brier
`0.25` on one observation.

For the **same 11** source-reported results at the newer receipt, market Brier
was `0.15573255403875894`, versus `0.25` for 50/50; market log loss was
`0.4727717137443161`, versus `0.6931471805599453` for 50/50. An independent
fraction-arithmetic and set-winner check reproduced the Brier calculation on
the admitted rows; it did not independently check source truth, clock, rights,
selection or identity.

The journal was frozen before the first results request, but the scoring and
later reconciliation were defined after some outcomes were already known. This
is an exploratory, selected **in-play** cohort, not an independent holdout;
11 results do not establish calibration, edge or profitability. Each
`finished_at` is the provider's last-live time, not an independently verified
official finish. Displayed prices were not verified as accepted. Names, source
IDs, people, geographical scope and rights for training remain unverified.
No source result is a verified bookmaker settlement; no actual bet, production
update, model training or promotion was performed.

The source responses, local receipts, full denominator, code and arithmetic
check are preserved in a private Drive bundle referenced from the issue
checkpoint. No raw responses or credentials are committed here.
