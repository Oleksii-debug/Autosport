# Predeclared daily-learning challenger — 2026-11-10

This separate challenger is registered BEFORE the first temporal trial is scored
and BEFORE the November 10 replication source is requested. The original six
policies and both fixed populations remain unchanged and separately reported.
This does not replace a failing test, revise an outcome, or refit the fixed model.

The question is whether one ordinary next-day training update improves the agent.
After all November 9 selected price attempts finish, freeze/evaluate that entire
cohort under its original protocol. Train a NEW model using the original 130
calibration rows PLUS every eligible November 9 forecast row whose result update
is strictly before November 10 00:00 UTC. Neither wins, returns nor forecast support
select the extra training rows. Preserve the input manifest and duplicate-ID check.

Use EXACTLY the existing two-residual formula, 25 coefficient pairs, fixed ridge
0.01, 200 whole-day bootstrap draws, seed20261110, and cutoff November 10 00:00 UTC.
No hyperparameter change, manual coefficient choice or variant search. Its source
features are the pre-day Elo/H2H values originally frozen for those events; do not
recompute training features with their own results. The first test becomes new
training data ONLY for this next-day challenger, not for its original assessment.

Freeze and publicly publish the new model's hash and learned weights BEFORE
fetching the replication's November 10 outcomes. Evaluate its mean and bootstrap
sensitivity-gated actions on the EXACT same eligible November 10 fixtures,
prices and feature history as the six original replication policies. These are
TWO additional predeclared policies, not two new independent datasets. The fixed
model remains a control; it is not overwritten. Compare all eight action rules
and the six distinct probability forecasts. Same costs, stake, threshold, bins,
full-population reporting and no-wager/execution restrictions apply.

This is a chronological RETROSPECTIVE learning experiment. Data collection and
model fitting occur after the historical events. Source-time filtering does not
prove historical local availability. No live prospective performance, production
promotion, repeatable profit or real monetary reward is claimed. No scheduled
service or automation is created.
