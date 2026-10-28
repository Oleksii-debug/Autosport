# Continue the outcome-learning campaign through the next match day

The 49-cycle run 40600793916 was still active when this continuation was
prepared. It had restored the previous checkpoint successfully. The first
broader forecast population began with 346 upcoming matches; its earliest
captured kickoff is 2026-10-28 16:00 UTC. A further four-hour run alone can end
before any of these match results exist, so this is a demonstrated continuity
gap against the owner's request for ongoing learning from actual outcomes.

The next queued run retains the same source and read-only rules but has six
sequential segments, each 49 cycles about five minutes apart, approximately
24 hours total plus setup. The first segment restores the newest completed
same-branch run; each later segment restores the exact previous segment of its
own run. Each segment retains its own checkpoint, including on failure. A later
run requires the last started segment from the newest completed run. Expired,
missing, ambiguous, corrupt or oversized checkpoints fail closed rather than
silently replacing accumulated memory with older evidence. Archive SHA-256,
state and raw source digests are verified during restoration. The existing
workflow concurrency group has cancellation disabled, so this continuation
queues behind the active run and does not interrupt it.

This is one bounded campaign, not a recurring schedule. It stores no secrets,
does not place tickets, has no account or real-money authority, and does not
automatically promote a strategy. Most result receipts and their corresponding
candidate outcomes are still in the future; no earnings are asserted. Provider
timestamps and executable prices remain independently unverified.

The separately frozen historical calibration test worsened Brier and unit
economics. A second, exploratory team-result residual reduced historical net
loss but remained negative; it is not installed in this running campaign.
Both must remain unpromoted until independent evidence supports use.
