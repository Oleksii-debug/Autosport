# Sample-only day-batched Elo: negative probability result

On the attributed CC BY 4.0 table-tennis sample described in
`ACCOUNT_C_TABLE_TENNIS_SAMPLE_AUDIT_20261028.md`, this research baseline starts
every player at 1,500 and updates with K=20. Every match on a calendar day is
predicted using ratings from **before that day**; only after all predictions
for that day do its reported results update ratings. This avoids inventing an
intraday order that the source does not provide. Evaluation is walk-forward:
past evaluation-day results can inform later evaluation-day predictions.

For the 819 matches dated September 1 or later, sample-only Elo Brier was
0.250065, versus 0.250000 for a constant 50% forecast and 0.244679 for the
publisher's supplied probability. Lower is better. Accuracy was 52.63%,
48.47% and 54.82%, respectively; exact 50% ties select player A. Accuracy alone
does not prove better calibrated probabilities or expected returns.

As an additional post-hoc research check, six K values from 0 to 80 were
compared using only the pre-September matches. That training-only selection
also chose K=20, giving the same later-sample result above. Later match labels
cannot affect the selected K in the included causality test. Since the sample
and its results were already inspected before this additional test, it is not
an independent confirmation.

This is an already inspected, non-confirmatory sample. The sample is only 2,000
of a much larger underlying population, so most intervening matches and player
histories are missing. The publisher's model may use a much fuller history and
its training and source timestamps were not independently verified; the models
therefore do not have equivalent input. The data contains no offered odds or
settlement and cannot evaluate profit. The sample-only model is **not promoted**;
the result does not rule out a better model trained on complete causal data.

Reproduce with `PYTHONPATH=src python scripts/audit_table_tennis_day_batch_elo.py`.
Tests verify that changing a same-day outcome cannot change any same-day
prediction, that later days can learn it, and that row order does not change the
predictions. JSON records the input and script hashes and the negative result.
