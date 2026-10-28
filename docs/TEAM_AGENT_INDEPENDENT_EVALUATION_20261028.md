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
