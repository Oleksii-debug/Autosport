# Agent economics experiments — 2026-10-27

Owner priority: improve and measure agents' virtual economic performance before
packaging. This contribution targets Account C's existing #2583 lineage at
`2c3cb80c348dd5328668cae042b4b2507baab0f8`. It does not reopen terminal Plan-3
component engineering or claim whole-product / Plan-7 closure. Conflict keys:
`learning-models`, `paper-shadow-convergence`.

## Demonstrated problem

The existing curriculum forces PARLAY_2 in seven slots and SINGLE in two slots
out of ten. Negative learned rewards cannot veto those actions. The exact-head
[endurance run 40468148917](https://github.com/Oleksii-debug/Autosport/actions/runs/40468148917)
finished successfully but lost **301.04 virtual UAH**: 10000 → 9698.96,
230 decisions, **zero WAIT**, maximum drawdown 433.3857142857. LOW_ODDS parlay
reward was −1224.30, LOW_ODDS single −145.00. Successful execution is not
successful economics. Summaries are in `evidence/agent-economics-20261027/`.

Artifact 12723157465 ZIP SHA256:
`7eef51a252cec403a11782d31b788983092fddbac6f249da66058d9680a8bfbd`.

The sandbox settles falling prices as wins, unchanged/missing prices as voids.
These are synthetic rewards, not actual match-result returns. Its memory must
not be promoted to a real-outcome agent as evidence of an edge.

## Experimental repair and compatibility

An opt-in `loss_guard_min_observations` allows negative cumulative strategy
memory to veto a forced action and admit WAIT. Frozen periodic retry cycles
retain bounded exploration if conditions recover. The original stake ceiling
(at most 0.5% of current bankroll, capped at 50 UAH) remains authoritative.
Observation counts include neutral sandbox transitions; this is a conservative
heuristic, not a confidence test. Recovery retries may still lose money.

Default behavior and existing unguarded plan payloads/hashes are unchanged.
Guard configuration is bound into a new plan identity and roundtrips through
existing durable policy/PaperBook recovery. No running owner checkpoint is
edited and no ongoing GitHub training is cancelled. This is an additive assist
to #2583, not a competing campaign integration or runtime-default promotion.

`compare_paper_loss_guard.py` freezes sample count and parameters before fetching
a stream, journals snapshots, resumes after interruption, and compares baseline
and guard on identical chronological inputs. Both choose before the next
snapshot is revealed. Results include losses as well as gains; existing results
cannot be overwritten. Local capture was interrupted after 48 records when its
runtime ended, then resumed. It is an interrupted replay experiment, not an
uninterrupted forward campaign. A separate exact-commit GitHub workflow collects
a fresh stream; its concurrency explicitly does not cancel active experiments.

## Actual recorded match outcomes

Source: [TipsAudit Football Prediction Dataset](https://tipsaudit.com/data),
CC BY 4.0 attribution: TipsAudit (2026). Raw CSV is unchanged; these scripts and
derived reports are Autosport analysis. Source SHA256:
`c7964f06b9aa02800381c50aca4637ef8c1ad7e940855d3f9c3b5f1bd40e53c5`.
7390 rows: 40 void/unsettled excluded, 7350 common-price eligible matches.
Train before 2026-09-21 UTC: 4993 matches. Validation: 2357.

Hypothetical fixed one-unit stakes, recorded actual final results:

| Validation proxy | Net units | ROI | Max drawdown units |
| --- | ---: | ---: | ---: |
| Lowest opening Pinnacle odds | −33.030 | −1.40% | 73.120 |
| Highest opening Pinnacle odds | −414.890 | −17.60% | 453.330 |
| Third-party recorded pick | −73.099 | −3.10% | 75.514 |

These are deliberately simple comparison strategies, **not deployed Autosport
agent results**. Every proxy also lost on training data, so a selector trained
only on that earlier period abstains. An additional hypothetical 1% stake cost
reduces each validation ROI by one percentage point.

Opening quotes may be backfilled; event-time price and result availability are
unverified. Source-pick quotes differ from the opening-price proxies. No soft
or closing quote is used as an agent input. This source is useful for falsifying
naive strategies, not proving an executable edge or a feasible capital schedule.

## One frozen probability/value candidate

`evaluate_recorded_value_agent.py` reuses `probability.paper_value`. It fits
probability-bin residuals only on training outcomes, shrinks them toward market
probabilities, then chooses at most one single per match. All three forecast
bins need sufficient support; the maximum odds and minimum expected return are
frozen in `examples/recorded_value_calibration.json` before evaluation.

Configuration SHA256:
`f854cbe94618f52eea91640ee36e06e028d6e929a8cbc4c683e3b5fabc28f4df`.
Bins 10; prior 100; minimum bin observations 100; expected return ≥5%; odds ≤10.
No threshold search or post-result retuning was performed.

Result: **0 bets, 2357 WAIT, 0 earned units**, no demonstrated earning capability.
2328 forecasts had sufficient support; missing support is not the principal
reason for abstention. Brier mean per outcome improved slightly from
0.1977331620 to 0.1977083874, without finding a tradable edge. The baseline
validation aggregates had already been inspected before designing this candidate:
this is explicitly exploratory retrospective analysis, not a pristine holdout.
Keep the failed-to-earn result; do not relax the threshold just to manufacture bets.

## Validation and continuation

Local Python 3.12: **78 tests passed** across both new experiment suites, recorded
benchmark/value tests, sandbox campaign, transparent bandit policy and canonical
settlement-learning suites. Tests cover future-outcome isolation, real-result
settlement, fee drag, sparse-memory abstention, duplicate rejection, risk caps,
negative/positive/neutral streams and durable restart. This is targeted Linux
evidence, not a full project/Windows/NVDA qualification.

Reproduce with the exact archived CSV (the upstream URL is mutable):

```bash
python scripts/benchmark_recorded_match_returns.py --csv tipsaudit-predictions.csv --output benchmark.json
python scripts/evaluate_recorded_value_agent.py --csv tipsaudit-predictions.csv --config examples/recorded_value_calibration.json --output value-agent.json
python scripts/compare_paper_loss_guard.py --workspace paired-run --config examples/paper_loss_guard_comparison.json
```

Next decision is based on paired net reward and drawdown, including adverse
results, not win rate alone. Economic promotion additionally needs an independent
pre-match signal, verified quote/result timing, and a frozen prospective campaign
with execution costs. Merely recalibrating the same odds has not supplied that
signal. Synthetic and recorded-outcome memories remain separate. No real-money
execution or guaranteed-profit claim is authorized by these experiments.
