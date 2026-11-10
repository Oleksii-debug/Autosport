# Fixed market-residual calibration trial — 2026-11-10

Research scope on existing PR #2592 / issue #2591. No closed production Section
is reopened. Existing causal histories, source-link checks and Decimal settlement
are reused. The production participant-rating histogram and three-way forecast
calibrators remain unchanged; this experiment learns two binary market residuals.

## Fit before obtaining new test outcomes

Use the exact 130 causal pre-day prediction rows from the November 9 historical
screen, covering October 23–November 7. Join source results only to verify identity,
winner and update time before November 8 00:00 UTC. Prior-day feature construction
was already fixed. These rows are training/calibration data, NOT a fresh test.

For market probability m and the existing market-anchored Elo/H2H probabilities e,h:
`p = sigmoid(logit(m) + a*(logit(e)-logit(m)) + b*(logit(h)-logit(m)))`.
Choose (a,b) from {0,.25,.5,.75,1} squared by minimum mean log loss plus
0.01*(a*a+b*b); exact ties prefer lower a+b, then a, then b. No intercept,
negative weights, feature search or test-driven tuning. The null model (0,0)
is eligible. This is fitting two constrained coefficients, not proof of calibration.

Bootstrap 200 entire UTC prediction days with replacement, seed 20261110;
refit the SAME fixed objective. The fifth percentile of each selected side's
predicted probability is a sample-sensitivity diagnostic, not a guaranteed
confidence bound on the true probability. Cross-day/player dependence and
model bias are not resolved. A guarded policy retains a mean-model action only
if this lower estimate also clears the same 3% net-edge threshold.

The fitted weights are **a=0, b=0.25** (130 rows, 16 days). They are fixed before
requesting the new results source. 54/200 bootstrap fits choose (0,0), showing
substantial uncertainty; no parameter is changed to force more wagers.
Model JSON SHA256: 5a4d8e88eaf760e380d0cef9cbbc9b9660e3789396d3e07e86a84e2786d749ec.
Collector script SHA256: a93a900d230ca9da34f1ce55138c288435e2b4515d0b734b06e775a2927001c6.

## Previously uninspected retrospective test; NOT live prospective forecasts

Make one public read-only GET of `/results?sport=table_tennis&date=2026-11-09`.
Save exact bytes, receipt, hash, and an index of IDs already present in every
previous result response from the restored November 9 evidence package.
Consider the same five leagues (Setka Cup, Setka Cup Women, TT Cup, TT Elite
Series, Czech Liga Pro), source finish hours 14 through 23 UTC on November 9.
Exclude every ID in that previous observation index. Within each finish hour,
select the first 12 IDs by SHA256(`autosport-calibration-holdout-v1|` + ID).
At most 120 selected fixtures; never replace one because its price/result/history
is unsuitable. Selection ignores winner, odds, score and history support.

Attempt one `/closing?fi=ID` request per selected ID, sequentially with at least
1.05 seconds between completed requests; stop on HTTP429/503, retain failures
and unattempted IDs. Do not retry failed IDs or silently change the population.
Use the existing strict winner-market, exact-name, source-ID, quote-time and
alias checks; reject an alleged alias with a separate result. Require confirmed
kickoff on November 9 at or after 14:00 UTC and before the reported finish.

Build test history ONLY from the archive received November 8 at 17:34:30 UTC,
SHA256 8331e851bf2a209810824a35ae31c6264c74ce823af8822d7ca17fa01b3f9688,
using cutoff November 9 00:00 UTC and excluding the two already documented
quarantined IDs 204173406 and 204188383. The missing late-November-8 history is
shared by every rule. No November-9 outcomes enter history or calibration.

Compare six separate policies: market, original Elo/H2H/combined anchors,
learned market-residual mean, and the same mean with bootstrap sensitivity gate.
All have the SAME eligible test population, recorded prices, 3% minimum estimated
net edge, one-unit hypothetical stake, 0.01 cost and 2% winning-payout haircut.
Stress uses 5% on the SAME tickets. Freeze feature rows/probabilities/actions in
a separate file before the label join and retain its hash. No fake probability
is substituted to encode abstention; the guarded policy has explicit actions.

Report ALL selection/eligibility failures, predictions and ticket counts, net and
stress returns, drawdown in kickoff order, Brier/log loss, and fixed probability
bins [0,.2,.4,.6,.8,1]. Compare probability metrics on all eligible events, including
abstentions; do not claim that lower Brier alone proves better calibration.
Old calibration rows, yesterday's two live cohorts and this retrospective temporal
test remain distinct. No optional stopping on a winning ticket, replacements,
parameter tuning, automatic promotion, account access, real bet or scheduled task.

Raw participant/source rows remain private. The new test is later in event time
and previously uninspected in this research, but predictions are reconstructed
AFTER the events: historical executable prices and source availability are not
verified. One day and repeated players do not establish independent profit.

Method context: scikit-learn's official probability-calibration guidance separates
fitting/calibration/testing and notes that Brier/log loss combine calibration with
other prediction properties. We implement the explicit fixed two-residual model
above, not a claim to reproduce CalibratedClassifierCV:
https://scikit-learn.org/stable/modules/calibration.html
