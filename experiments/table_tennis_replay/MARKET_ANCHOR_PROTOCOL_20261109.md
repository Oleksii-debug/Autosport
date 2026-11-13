# Frozen market-anchor experiment — 2026-11-09

Scope: research issue #2591, canonical draft #2592; no product/Windows changes.
Purpose: try to falsify fixed player-history corrections to market probabilities on a larger chronological archive, before any production use.

Source results: existing private MoviOdds results response received 2026-11-08T17:34:30.533327Z, SHA256 8331e851bf2a209810824a35ae31c6264c74ce823af8822d7ca17fa01b3f9688.
Source labels scoped to exact league are provisional research keys, not verified people or clubs. No canonical identity registry changes.
The archive was already inspected in earlier work. This is a retrospective, temporally separated screen, NOT an independent prospective holdout. Source rights and actual execution are not verified.

Sampling fixed before collecting new closing responses: UTC 2026-10-23 through 2026-11-07 inclusive. Five leagues: Setka Cup, Setka Cup Women, TT Cup, TT Elite Series, Czech Liga Pro. Within each date choose 15 IDs with lowest SHA256("autosport-market-anchor-v1|" + fixture_id). Choose by reported last-live date and identifiers only, including unknown-status rows. No replacements for missing, unknown, invalid, ambiguous or unavailable prices. All selected rows and acquisition failures remain in private coverage report. At most 240 closing requests, one per second or slower, stop on rate limit/busy response. Read-only API, no account, orders or background schedule.

History: valid best-of-five sporting results, reported update and last-live strictly before UTC midnight of the prediction date, excluding current fixture ID. The source update clock is NOT proof of historical local availability; midnight separation is a conservative descriptive split. Exact duplicate IDs must agree; otherwise reject. Learn no hyperparameters from target outcomes. Replay equal update timestamps as a batch using the prior rating state, so within-batch ordering cannot leak. Recalculate from fixed full pre-day history. Initial match-level Elo 1500, K=24, divisor=400. At least 10 prior matches and 2 distinct opponents on both sides for Elo correction. Pair counts are prior 30 days, minimum 3 meetings for H2H correction.

Frozen candidates on exactly the same included matches:
- market: normalized inverse-price two-way probability.
- elo_anchor: 0.8*market + 0.2*Elo (market fallback if support insufficient).
- h2h_anchor: (prior_home_wins + 20*market)/(prior_meetings + 20), market fallback below three meetings.
- combined_anchor: mean of elo_anchor and h2h_anchor.
Also report unblended Elo accuracy only as a diagnostic; never select the best retrospectively for deployment.

Prices: exact requested fixture; only a singleton fixture_id, or a linked pair consisting exactly of requested live ID plus distinct named prematch_id. Final close, known non-estimated scheduled kickoff, pre_close_basis=kickoff. Kickoff must fall on selected date before last-live. Strict two-side winner-market identity, unique selections, same nonempty market ID, unsuspended finite decimal odds >1, quote time <= kickoff, >= kickoff-24h. A price's last change is not freshness or execution proof. Accept older unchanged close within this bound instead of repeating the earlier ten-minute change filter. This filter is prespecified, not tuned after results.

Economic screen: one unit per selected event, at most one side, choose maximum estimated net edge if >=0.03; fixed 0.01 cost per wager and 2% haircut of winning decimal payout. Break ties toward home. Decimal arithmetic. Report 5% payout-haircut stress on the SAME tickets; no retuning. Accuracy: Brier/log loss vs market; economics: stake, P&L, ROI, wins, max drawdown in scheduled-kickoff order, market-implied expected P&L on same chosen tickets, every UTC date (including zero bets). Day-block bootstrap 2000 draws, seed 20261109, descriptive intervals with only 16 days; serial/cross-day player dependence remains. Report both Oct23-30 and Oct31-Nov7 periods without selecting a winner.

No claim of profit from a positive historical number. This screen can reject bad ideas; production promotion still requires verified identity/rights, independent forward evidence and executable price evidence. No optional stopping by observed profit; finish frozen population or record source failures.
