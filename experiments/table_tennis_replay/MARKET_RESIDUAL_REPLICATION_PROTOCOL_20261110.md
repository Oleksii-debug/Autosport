# Fixed second-day replication — 2026-11-10

This is a separately reported replication of
[the market-residual trial](MARKET_RESIDUAL_PROTOCOL_20261110.md), registered
while the first trial's prices are still collecting and BEFORE its outcomes
have been joined/scored. It does not replace, enlarge, or change that first cohort.
Both cohorts will be reported regardless of P&L; no winner-based optional stopping.

Reuse the exact learned model SHA256
5a4d8e88eaf760e380d0cef9cbbc9b9660e3789396d3e07e86a84e2786d749ec,
weights (0,0.25), all 200 stored bootstrap fits, six policies, thresholds,
normal/stress costs, score bins, source-link checks and evidence restrictions.
No refit on either cohort. Original 130 calibration rows remain the only fit data.

The historical test window is November 10, UTC finish hours 00 through 06;
select at most 12 previously unobserved source IDs per finish hour by
SHA256(`autosport-calibration-replication-v1|` + ID), at most 84 total.
Require confirmed kickoff on November 10 at or after 00:00 and before 07:00 UTC.
Prior-observation exclusions additionally include EVERY ID in the captured
November 9 result response. The two samples cannot share a source fixture ID.
Make one public GET `/results?sport=table_tennis&date=2026-11-10`, then one
sequential closing request per selected ID, same pause/error/no-replacement rules.
Collector SHA256: b49c5d4b3eb34426bd46c1e419ddd96745d04ff6ad5e905abaa1e195d72e5f66.

To test the ordinary next-day history update, every policy uses the SAME history:
original November 8 archive plus valid November 9 results from SHA256
debe48973b9c2f6df5f6a792e52bbac0ce2d59bf2b5b7d1648248b7213b754c5. Quarantine any valid-result
ID shared by these two archives from BOTH histories; keep all IDs excluded from
test selection. Use only finishes AND source updates strictly before November 10
00:00 UTC. Current test outcomes never enter its features. Store the quarantined
ID list and history input hashes in the forecast artifact. Late-November-8 data
remain incomplete; neither history nor identity coverage is called complete.

The November 9 history was captured on November 10. This is therefore a
retrospective counterfactual with source-time filtering, NOT proof the full
history/quotes were locally available at the historical decision time. Model
parameters are also fitted after these test events. Do not call this a live
prospective trial or verified economic performance. One additional partial day
still does not establish independent, repeatable profit or authorize promotion.

Report this cohort separately from the first. Feature history necessarily differs
between days; avoid attributing a P&L difference between cohorts solely to history
refresh. Preserve all abstentions and exclusions. No real orders, scheduled task,
production-memory change, terminal-Section reopening, or Windows-release claim.
