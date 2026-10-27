# Account C selection repair and development evidence

Canonical upstream: PR #2583, source `2c3cb80c348dd5328668cae042b4b2507baab0f8`.
Scope: sandbox agent selection; no change to PaperBook, settlement authorities,
production risk limits, or observed forward-evidence admission.

## Observed problem

The successful two-hour endurance run 40468148917 completed 230 decisions with
232 source snapshots and no network failures. Its independent reload reported a
final simulated balance of 9698.96 UAH from 10000 UAH (rounding display to cents).
PARLAY_2_HIGH_ODDS received 92 observations and approximately +951.86 UAH;
PARLAY_2_LOW_ODDS also received 92 observations and approximately -1224.30 UAH.
Single high/low cohorts had 23 observations each and +116.40/-145.00 UAH.
These are observed sandbox accounting values, not sporting prediction evidence.

The fixed curriculum kept alternating strategies even after their mean rewards
diverged. The previous local one-observation greedy experiment also showed why
immediate exploitation was inadequate: one early loss could suppress a useful
strategy indefinitely. The eight-observation warmup is an exploratory engineering
setting, not a parameter validated on an untouched statistical holdout.

## Change

Sample the least-observed family strategy until each has eight observations, then
let the existing transparent policy select among positive empirical means.
Unprofitable/zero-mean families emit WAIT without opening a ticket. This applies
to the family slots and the free-choice slot. Observations and losses are retained;
there is no rewriting of history to improve scores. Selection protocol and warmup
are included in the immutable plan hash, with campaign state schema v3.

The reward model is still explicitly synthetic price-movement settlement. Voids
remain observations. There is no claim of statistically reliable positive edge.
Full abstention can persist after a market regime changes because unchosen actions
have no factual reward in this on-policy campaign. This limitation is retained
explicitly; fabricated counterfactual rewards would not be an acceptable repair.

## Paired development regression

`scripts/compare_account_c_selection.py` runs the previous fixed-alternation rule
and the new rule over the same pre-generated snapshots using the existing campaign
and PaperBook. Six scenario families yield eight cases (three fixed random-walk
seeds). Each case has 120 decisions; every result is kept, including regressions.
The JSON report records scenario digests and source hashes. It is development
regression evidence and never a forward population, holdout or promotion gate.

| Scenario | Fixed net UAH | Adaptive net UAH | Fixed / adaptive drawdown UAH |
|---|---:|---:|---:|
| Rising prices (all synthetic losses) | -4139 | -1468 | 4139 / 1468 |
| Falling prices | 128415.68 | 133065.68 | 0 / 0 |
| Flat prices | 0 | 0 | 0 / 0 |
| Alternating | 21236.46 | 19977.82 | 100 / 342 |
| Regime change | 39607.84 | 41325.04 | 3000 / 3000 |
| Random walk 11 | 4774.76 | 9078.712 | 250 / 258 |
| Random walk 29 | 5606.9232 | 6629.52 | 706.44 / 506 |
| Random walk 47 | 6315.584 | 9068.04 | 805 / 950 |

Six net improvements, one tie and one regression; drawdown is not uniformly
improved. Large gains in the falling-price fixture expose the simulation model's
limitations: a falling quote is treated as a win irrespective of match outcomes.
These numbers cannot establish real-world profitability or justify larger stakes.

## Validation and next evidence

Behavioral tests cover bounded warmup, use of learned preference, abstention after
losses, restart retention, and refusal of altered selection settings. Existing
causality/settlement/research integration tests remain required. A new two-hour
campaign on the published source must retain independent reload and artifact
digests; earlier run 40468148917 does not qualify the changed source.

Real-money execution, forward evidence eligibility, promotion authorization and
profitability claims remain false. Windows package acceptance remains separate;
Plan6 run 40455640547 compiled successfully but failed the external UIA gate.
