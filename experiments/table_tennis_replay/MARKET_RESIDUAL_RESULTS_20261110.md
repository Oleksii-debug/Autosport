# Learned market residuals: completed two-day research, 2026-11-10

**No profitable production agent has been established.** The first learned model
made no hypothetical bets in the November 9 test. Its predeclared next-day update
learned zero weight for both additional signals. All controls, exclusions and
the second-day results are reported below; a profitable individual ticket does
not become a selected strategy. No wagers, production promotion or scheduled
service were created. This is a separate research continuation on PR #2592,
issue #2591; terminal production Sections and the Windows release are unchanged.

Across these two new retrospective cohorts, the unchanged H2H rule returned
**+3.70450 hypothetical units on eight tickets** (five wins, three losses).
The fixed combination returned +2.3110 on three tickets and Elo lost 1.620 on
five. These are three separate policy results, not one portfolio. H2H is a
positive research observation requiring prospective replication, not a proven
income stream; yesterday's separately reported live-paper cohorts remain part
of the wider evidence and are not replaced by these positive historical results.

## What actually learned

`market_residual_calibration.py` fits two constrained corrections to normalized
market probability using the existing causal Elo and H2H forecasts. It includes
the market-only null model. A fixed 25-point coefficient grid, log-loss objective,
ridge penalty 0.01 and 200 whole-day bootstrap refits were registered before new
data. It records explicit abstention actions and exact Decimal hypothetical
settlement; no invented probability is used to encode abstention.

The initial fit used **130 previously available matches over 16 days** and chose
Elo weight **0**, H2H weight **0.25**; **54/200** bootstrap fits chose both zero.
After the complete first test was scored, the predeclared daily update added
**70 of its 71 eligible rows** to the same training objective: **200 rows over
17 days**, weights **(0, 0)**, **104/200** bootstrap fits with both zero. One
additional result had not updated before the declared next-day cutoff and was
excluded regardless of outcome. Original training features were preserved;
today's results never became today's prediction features.

The fixed original model remains a control. The updated model is a separate
research checkpoint, not a production-memory update. Its zero weights mean
the learned probability equals the normalized market probability. Abstention
preserves hypothetical capital but earns nothing and proves no predictive edge.
The daily update did not demonstrate a profit improvement over the fixed learned
control: both abstained on the second cohort, while the original H2H rule won its
three tickets. Updating parameters is not itself evidence that an agent improved.

## Frozen design and chronology

- [Initial trial](MARKET_RESIDUAL_PROTOCOL_20261110.md), commit
  `4bb191b559ef3a0961e6ac6b7c612917f0d00994`, precedes the first new source request.
- [Second-day replication](MARKET_RESIDUAL_REPLICATION_PROTOCOL_20261110.md),
  `a87d11477dccf1d8790201d73d8ace88e841f66a`, precedes first-test scoring.
- [Daily learning challenger](DAILY_RESIDUAL_UPDATE_PROTOCOL_20261110.md),
  `6e104d387363a68ffeea6fdc61aba9c880bfcb86`, also precedes first-test scoring.
- [Updated model freeze](DAILY_RESIDUAL_MODEL_FREEZE_20261110.md),
  `909f1a739a7b574b51d353f822596ccfb3967739`, precedes the second source request.

The private cross-cohort audit verifies commit/source/scoring chronology, disjoint
selected IDs, unchanged control-model hashes, and every selected price response.
Selection uses fixed finish-hour strata and an ID hash, never winners, price or
support. No failed/unsuitable event was replaced. Features/actions were frozen in
separate artifacts before label joins. The primary has six action policies; the
replication has those six plus two predeclared daily-learning policies. The two
additional policies are not independent datasets.

**These are retrospective temporal reconstructions.** Events had already happened
when data and models were obtained. Source timestamps and temporal ordering do
not prove real-time local availability or that any recorded price could have
been executed. The November 9 and November 10 history snapshots also differ;
cross-day P&L differences cannot isolate the effect of refreshed history.

## November 9 temporal test

All **120** selected closing-price responses were captured;
**71** passed the fixed source/market/time checks.
Exclusions: `alias_has_separate_result`: 21, `ambiguous_fixture_link`: 21, `confirmed_kickoff_outside_fixed_window`: 5, `invalid_or_missing_source_result`: 1, `missing_or_suspended_close`: 1.

| Policy | Tickets | Wins | Net units | Stress net units | Maximum drawdown |
|---|---:|---:|---:|---:|---:|
| Market reference | 0 | 0 | +0.000000 | +0.000000 | 0.000000 |
| Elo anchor | 3 | 1 | +0.400000 | +0.295000 | 2.020000 |
| H2H anchor | 5 | 2 | -0.934000 | -1.060000 | 3.030000 |
| Fixed combination | 1 | 1 | +1.146000 | +1.080000 | 0.000000 |
| Frozen learned mean (0, .25) | 0 | 0 | +0.000000 | +0.000000 | 0.000000 |
| Frozen learned mean + sensitivity gate | 0 | 0 | +0.000000 | +0.000000 | 0.000000 |

| Probability model | Brier | Log loss |
|---|---:|---:|
| Market reference | 0.238138660 | 0.668531851 |
| Elo anchor | 0.239099347 | 0.670835685 |
| H2H anchor | 0.240301663 | 0.673422029 |
| Fixed combination | 0.239341915 | 0.671303017 |
| Frozen learned mean (0, .25) | 0.238507187 | 0.669323837 |

## November 10 replication

All **84** selected closing-price responses were captured;
**65** passed the same checks and the declared kickoff window.
Exclusions: `alias_has_separate_result`: 9, `ambiguous_fixture_link`: 5, `ambiguous_selection`: 4, `kickoff_date_or_order`: 1.

| Policy | Tickets | Wins | Net units | Stress net units | Maximum drawdown |
|---|---:|---:|---:|---:|---:|
| Market reference | 0 | 0 | +0.000000 | +0.000000 | 0.000000 |
| Elo anchor | 2 | 0 | -2.020000 | -2.020000 | 2.020000 |
| H2H anchor | 3 | 3 | +4.638500 | +4.403750 | 0.000000 |
| Fixed combination | 2 | 1 | +1.165000 | +1.067500 | 1.010000 |
| Frozen learned mean (0, .25) | 0 | 0 | +0.000000 | +0.000000 | 0.000000 |
| Frozen learned mean + sensitivity gate | 0 | 0 | +0.000000 | +0.000000 | 0.000000 |
| Daily updated mean (0, 0) | 0 | 0 | +0.000000 | +0.000000 | 0.000000 |
| Daily updated mean + sensitivity gate | 0 | 0 | +0.000000 | +0.000000 | 0.000000 |

| Probability model | Brier | Log loss |
|---|---:|---:|
| Market reference | 0.248912843 | 0.690572259 |
| Elo anchor | 0.250335963 | 0.693509992 |
| H2H anchor | 0.246790137 | 0.685398669 |
| Fixed combination | 0.248212895 | 0.688796272 |
| Frozen learned mean (0, .25) | 0.248226203 | 0.688897965 |
| Daily updated mean (0, 0) | 0.248912843 | 0.690572259 |

Each row is a different policy on the same cohort. **Do not add policy returns
together.** One-unit hypothetical stake, 0.01 per-ticket cost and 2% winning-payout
haircut apply. Stress uses a 5% haircut on exactly the same selected tickets;
there is no stress-driven reselection. Drawdown is ordered by scheduled kickoff,
not a cash-flow model of concurrent accepted wagers. Zero-ticket ROI is undefined,
not a profitable zero-risk return. The assumed costs are research parameters,
not a claim about a bookmaker's actual charges.

Probability metrics include every eligible row, including abstentions. Lower
Brier/log loss measures probability quality, not guaranteed profit or calibration
alone. Fixed five-bin diagnostics and pair-clustered Brier-difference resamples
are retained privately; repeated players, only two test days and sample selection
limit their interpretation. Bootstrap probability bands measure sensitivity to
training-day resampling, not certified bounds on true win probabilities.

## Why more history has not yet demonstrated an edge

- November 9: 71 eligible; Elo history support 69, H2H support 41; 71 exact league/name pairs. Median quoted overround 7.62%; median minimum probability increase above the normalized market to clear the fixed cost/edge rule 6.04 percentage points.
- November 10: 65 eligible; Elo history support 63, H2H support 42; 65 exact league/name pairs. Median quoted overround 7.79%; median minimum probability increase above the normalized market to clear the fixed cost/edge rule 6.29 percentage points.

History support is present for many forecasts. Having a rating or previous
meetings therefore does not itself establish additional information beyond the
price. The cost hurdle and the observed uncertainty must also be overcome.
These diagnostics are post-hoc descriptions, not newly optimized thresholds or
league filters. No positive subgroup is promoted after inspecting its results.

The existing `market_anchor_support.support` calculation was reused for a
descriptive ticket audit. Removing H2H's largest positive ticket leaves +1.52950
units across the two cohorts; removing the combination's largest positive
ticket leaves only +0.1360. This is a concentration check, not a confidence
interval. The eight H2H tickets span eight source-name pairs but only two days;
their independence is not established, and no variant was promoted by this audit.

The old 130-row dataset also has a separate **exploratory** daily-learning replay:
41 warmup rows and 89 subsequent rows. It is already-seen training material, not
a third new test. Its one learned-model ticket won 1.195 hypothetical units;
that single old ticket is not validation, and the sensitivity-gated model placed
none. Those observations were not used to alter the declared two-day experiment.

Source quality remains a separate obstacle. The previously demonstrated closing
alias defect is still rejected when an alias has its own result or the fixture
link is ambiguous. The replication quarantines overlapping history IDs from both
history snapshots. Exact league/name labels remain research proxies: neither
global player identity nor club affiliation is certified by these results.

A fresh TT Cup official monthly page yielded 1,455 valid rows but **no unique
corroboration for the 10 eligible first-test TT Cup fixtures**: nine had no matching
candidate and one had an unsupported/ambiguous abbreviation. The assumed Prague
timezone is not independently established. This is a coverage limitation, not
proof of agreement or disagreement; no official score claim or test filtering
was manufactured from it. Previously retained roster evidence remains separate.

## Validation and retained evidence

The research code revision `4bb191b559ef3a0961e6ac6b7c612917f0d00994` passed
**684 tests, 2 skipped**. Later commits before this report add protocols/model
freezes, not code changes. New tests cover genuine signal and null learning,
reproducibility, leakage/duplicates, side orientation, bootstrap abstention and
Decimal settlement compatibility.

Independent standard-library verification recomputes the selected populations,
source receipt hashes, result/game validity, all 25 fitting objectives with
40-digit Decimal arithmetic, both models' probabilities and all 200 stored
bootstrap predictions, action thresholds, rational-number payouts, Brier and
log loss. Frozen forecasts are checked against scored rows. This validates the
recorded calculation, not the external truth of every source identity or quote.

| Artifact | SHA256 |
|---|---|
| Fixed model | `5a4d8e88eaf760e380d0cef9cbbc9b9660e3789396d3e07e86a84e2786d749ec` |
| Daily updated model | `01074f8279715996b3a65531afbc5a0997d699d4437c7e87bf50b1b7a0946c22` |
| November 9 frozen forecasts | `29300a3eb3ecff7687f4fe57af2f139113b05eaebf226e8f6228628bbc9f3958` |
| November 10 frozen forecasts | `8ce0c55228a45b3e56e1fb119bcac2f357e606cc10c63c93d11827f05af23e72` |
| November 9 evaluation | `26c0ab736e1d3a853f2ed523efc03ec8f41d7429d6c50f50dd0dcb3c25b22647` |
| November 10 evaluation | `2354cab0c5b0814b040d0acbce5d81f84bf8a7b691312c44decadc16d7df123d` |

Raw source bytes, request receipts, selection/exclusion manifests, models,
training rows, all forecasts/outcomes, independent verification and reproduction
scripts are retained in the private `Autosport-TT-calibration-20261110.zip` package.
No raw participant rows or credentials are published in this aggregate report.

The evidence does not justify increasing stakes, forcing more actions, or
presenting an installer as a money-making product. The next credible profit
claim needs additional verified information that improves untouched forecasts
and then a prospectively recorded paper run with actual observation times and
available quotes. Neither continuous autonomous operation nor future profit is
asserted by this completed experiment.
