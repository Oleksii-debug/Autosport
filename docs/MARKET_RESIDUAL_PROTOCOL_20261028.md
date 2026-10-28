# Fixed market-residual team experiment

The previous calibration challenger worsened recorded accuracy and net return.
Test a different independent input: previous team results relative to the market's
own expectation. This is exploratory research on a previously inspected dataset;
it cannot promote a strategy or prove executable returns.

Freeze this rule before executing the comparison: group team names exactly by
country and league as supplied; do not infer that names across leagues refer to
the same identity. Missing, identical or ambiguous names yield the original
market forecast. Names are unverified and can still change or collide.

For a past match, expected home points = p(home) + p(draw)/2 and actual points
are 1 for home win, 0.5 for draw, 0 for away win. Update the home team's
residual rating by `24 * (actual - expected)` and the away team's by its
negative, only after the outcome is assumed revealed 48 hours after kickoff.
All ratings start at zero. For a new match, require five revealed matches per
team. Transfer `(home_rating-away_rating)/4000` probability from away to home,
bounded to ±0.05 and bounded so both probabilities stay above a floor equal to
the smaller of 0.001 and half of either starting home/away probability;
draw remains unchanged. Negative transfers move probability from home to away.
The result is a probability vector, not a claimed true probability.

Compare original market vector versus this challenger using exactly the previous
fixed price-gap/odds/daily-cap protocol in
`CALIBRATION_ECONOMICS_PROTOCOL_20261028.md`: no closing prices or tuning.
Report 1%-cost ROI, net units, drawdown, Brier and all decisions. Calibration
and team model are two separate exploratory hypotheses; any positive result
would require a new untouched forward test and a promotion review.

The historical source does not prove publication, price acceptance, team
identity, actual liquidity, or label availability at the assumed time. The
pre-match price order uses kickoff and ID as a proxy for unknown quote times.
No PaperBook, Risk, real execution, production model, or running job is changed.
Source: TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0.
