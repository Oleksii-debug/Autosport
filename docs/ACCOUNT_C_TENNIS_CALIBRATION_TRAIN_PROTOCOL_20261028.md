# Chronological calibration of market-anchored research agent

After inspecting earlier results on this published 2025 tennis file, test one
new, nonpromotional hypothesis: train a small regularized logistic adjustment to
the market-implied probability using past Elo, then assess later matches. This
is post-hoc development research; 2025 outcomes have already been observed and
cannot be claimed as an untouched holdout or prospective evidence.

- Data, source attribution, exclusions and hypothetical price/fee assumptions
  match `ACCOUNT_C_FULL_TENNIS_RESEARCH_PROTOCOL_20261028.md`.
- Generate training examples from dates 2024-12-27 through 2025-06-30, using
  Elo features computed only from strictly earlier match days. Match price is
  the reported best close and may not be executable or available before start.
- Fit `sigmoid(logit(market_probability) + intercept + w*logit(elo_probability))`
  by 500 full-batch gradient steps, fixed learning rate 0.4, L2=0.1 on `w` and
  L2=0.01 on intercept, both initialized to zero. No fitting on July–December.
- Evaluate July–December with exact same chronological day-batched Elo updates,
  0.05 edge gate, known-both-player guard, one hypothetical stake unit and 2%
  hypothetical fee on winning profit. Keep NO_BET and the original controls.
- Report all bets, losses, NO_BET, return, drawdown, Brier and log loss; preserve
  the full negative result and the training sample count. Runtime policy and
  real-money, forward-evidence and promotion flags remain unchanged.

Results rely on unverified source availability/settlement chronology and best
prices possibly coming from distinct books. The prior model evaluations saw the
later data, so no unbiased efficacy or real profitability claim is possible.
This sport is tennis, not table tennis.
