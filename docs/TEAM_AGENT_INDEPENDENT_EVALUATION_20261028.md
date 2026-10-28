# Team agent: independent recorded-outcome evaluation

This research contribution brings the unique team-residual model from #2586
onto the active #2587 shadow-learning lineage. Existing shadow memory, campaign
collection, PaperBook, risk, durable journal, CLI/Factory and Plan 6 remain
owned by their existing components. The candidate is not connected to runtime
promotion or execution.

The model learns bounded, regularized team corrections to a market probability
prior from earlier results, returns canonical research ForecastRecord values,
and preserves training state across restart. Its uncalibrated uncertainty is 1;
the existing research critic rejects it. Training duplicate results or using a
result before its availability is rejected. Sparse histories fall back to the
market. No accepted market price or provider entitlement is inferred.

On the previously inspected TipsAudit source, both the frozen and online model
lost after hypothetical 1% costs (-18.030/63 bets and -25.380/109 bets), with
worse aggregate Brier and log loss than the market baseline. Those candidates
were rejected for that sample; the results are preserved in
`evidence/agent-economics-20261027/team-residual-experiment.json`.

An independent dataset from Football-Data.co.uk evaluates the same fixed model,
threshold and costs on English Premier League seasons. 2022/23 is training;
2023/24 and 2024/25 are separate later seasons. Only pre-closing PSH/PSD/PSA
prices are used, and current outcomes cannot affect the current choice.

| Agent | 2023/24 | 2024/25 | Total |
| --- | ---: | ---: | ---: |
| Frozen model | -20.32 / 113 bets | -5.92 / 130 bets | -26.24 |
| Delayed online | -11.68 / 106 bets | +4.66 / 136 bets | -7.02 |

All figures are hypothetical units after 1% per-stake costs. The positive later
season has an ROI bootstrap interval including negative outcomes. The online
model learns from earlier test-period outcomes, so its result is prequential,
not an untouched frozen holdout. Quote availability, fills, result timestamps,
stake limits and overlapping capital are not proven. The specified 48-hour
result delay and UK kickoff timezone are assumptions. Retrospective results do
not authorize promotion, real execution, profitability or statistical superiority.

Source permits use for quantitative testing:
https://www.football-data.co.uk/downloadm.php and its notes.txt. No redistribution
or commercial entitlement is claimed; source CSVs are not included. Exact raw
SHA-256 identities, explicit exclusions, methodology and full metrics are in
`evidence/agent-economics-20261027/independent-team-2022-2025.json`.

Reproduction:

```
python scripts/evaluate_independent_team_agent.py --season 2223 2223.csv --season 2324 2324.csv --season 2425 2425.csv --evaluation-season 2324 --evaluation-season 2425 --config examples/independent_team_2022_2025.json --output independent-results.json
```

This is progress toward the owner's earning-program goal, not completion of it.

## Fixed price source and incomplete-season diagnosis

A further 2025/26 source check found Pinnacle pre-closing prices on only
210 of 380 league rows, with 170 excluded. The missing quotes are concentrated
from January onwards. A positive result on the available subset must never be
described as the result for the entire season.

The evaluator now requires an explicit, fixed price source (`pinnacle` by
default, or `bet365`), reports input/eligible counts for each season and never
falls back from a missing quote to another bookmaker. The protocol digest binds
the selected bookmaker and season list, in addition to the original model
configuration. Source CSV files are read once, and their original hashes are
retained. This remains research-only; price availability is not verified.

The same fixed model, threshold and cost were replayed for 2022/23–2025/26 using
only the Bet365 columns. Each season has 380 eligible rows. Online hypothetical
net units after 1% cost: 2023/24 **-17.39** (83 bets), 2024/25 **+7.49**
(98 bets), 2025/26 **+24.33** (157 bets). The frozen model records **-10.41**,
**-13.51**, **+18.24**, respectively. Per-season bootstrap intervals include
negative outcomes. Bookmaker selection was made after observing the incomplete
Pinnacle dataset and prior results, so this is exploratory, not a pristine
holdout or evidence of executable profitability. Earlier results are retained.

The raw-source reports identify both source price sets, input denominators,
source hashes, protocol hashes, missing rows, decisions digest, costs, drawdown
and uncertainty. No calibration, execution or promotion authority is granted.

```
python scripts/evaluate_independent_team_agent.py --season 2223 2223.csv --season 2324 2324.csv --season 2425 2425.csv --season 2526 2526.csv --evaluation-season 2324 --evaluation-season 2425 --evaluation-season 2526 --price-source bet365 --config examples/independent_team_2022_2025.json --output independent-bet365.json
```

## Current-season continuation, same frozen parameters

The same Bet365 method was extended without retuning to the 80 recorded matches
in the 2026/27 season through 25 October. The frozen model recorded -0.51 units
after costs on 23 hypothetical bets; the delayed-online model recorded -5.62 on
29. Online Brier per side was 0.206736 versus the market baseline 0.203480.
The online agent updates from older recorded results after the assumed 48-hour
delay. Selection of Bet365 followed inspection of the missing Pinnacle quotes
and earlier results, and source prices/results are not independently timed or
executable. This is exploratory and small, not a prospective performance or
profitability claim. No parameter was changed after inspecting this sample.

Source hash, fixed-source protocol identity, complete metrics and uncertainty:
`evidence/agent-economics-20261027/independent-team-bet365-2026-2027-to-oct25.json`.
The active live-observation campaign and already running qualification checks
were not interrupted by this separate follow-up evidence branch.

## Execution price stress (28 October 2026)

The independent evaluator now replays the same selected actions at four fixed
scenarios: zero, 1%, 5%, and 10% reductions to the **profit portion** of winning
prices, with the original 1% stake cost charged on all picks. These are artificial
stress scenarios, not evidence that these quotes were obtainable or that any
bet was filled. Selection and learning remain unchanged. Results and input
hashes are in `docs/evidence/agent-economics-20261027/independent-team-bet365-price-stress-2022-2027.json`.
The bookmaker and study were selected after prior results were viewed, and
online learning uses assumed result delays. The partial 2026/27 sample contains
29 hypothetical online selections; its net changes from -5.62 units at zero
haircut to -6.3535 at 5%. This does not establish prospective edge or profit.
