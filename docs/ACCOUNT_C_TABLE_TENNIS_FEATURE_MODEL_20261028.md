# Train-only classifier on supplied table-tennis features

This uses the attributed CC BY 4.0 2,000-row sample and the limitations in
`ACCOUNT_C_TABLE_TENNIS_SAMPLE_AUDIT_20261028.md`. The publisher supplies
pre-match Elo and solidity features, but their actual source, timing and
training lineage were not independently verified. There are no offered prices
or match settlements. The sample and previous results were already inspected.

An exploratory logistic classifier uses rating difference, solidity difference
and an intercept. It trains 600 fixed gradient steps with a fixed ridge penalty
only on the 1,181 records before September 1; 819 later records are evaluated.
The fixed procedure is reproducible and a test confirms later features and
labels cannot influence its fit. On the later records:

| Forecast | Brier score | Accuracy |
|---|---:|---:|
| Constant 50% | 0.250000 | 48.47% |
| Publisher probability | 0.244679 | 54.82% |
| Train-only feature classifier | 0.244633 | 55.80% |

The Brier difference versus the publisher is roughly 0.000046. A seeded
2,000-draw bootstrap resampling 50 whole evaluation days gives a
publisher-minus-classifier interval of approximately -0.00313 to +0.00336.
It includes zero, depends on day-block assumptions and is not a confirmatory
holdout. The apparent difference is not evidence that the classifier is better.
Accuracy alone cannot establish calibrated odds, actual fills or profit.

Reproduce with `PYTHONPATH=src python scripts/audit_table_tennis_feature_model.py`.
The JSON records coefficients, fixed training parameters, source and script
hashes, and uncertainty. The model is research only and is not promoted to an
agent. An independent chronological source with verified pre-decision capture,
licensed prices, results, realistic costs and execution evidence is still needed.
