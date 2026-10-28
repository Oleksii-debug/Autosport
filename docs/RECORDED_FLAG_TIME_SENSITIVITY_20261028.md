# Source-time sensitivity after first live flag run

The first predeclared strict run required equality between preimage time and
`value_recorded_at_utc`. It excluded 1058 otherwise eligible rows due to this
equality check. Examination of these rows found the recorded field was 1–55
seconds later than the hashed preimage field. The provider documents neither
field here as an independently certified publication time. Exact equality is
therefore retained as a strict sensitivity result, but is too restrictive to
describe the full source-sealed population.

This separately versioned exploratory analysis changes only the time rule:
preimage time <= recorded time < kickoff and at most 90 minutes apart, with both
times at or after the v2 cutoff. The 90-minute upper bound is the provider's
stated freshness horizon, not an independently verified guarantee that a
bookmaker accepted a price. Recompute both original and re-quote scenarios and
retain the strict result unchanged. The choice to revise the time rule followed
inspection of the source timing and the strict analysis; the new result is not
precommitted or an untouched holdout. No threshold is changed based on outcomes.
All other selection, costing, conflict and interpretation rules remain as in
`RECORDED_LIVE_FLAGS_PROTOCOL_20261028.md`.
