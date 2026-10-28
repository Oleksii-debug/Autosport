# Exploratory transfer of the set-margin rating

The K=20 full-margin rule selected on the inspected 2022 Setka history was
applied to the separately sourced CC BY 4.0 2026 sample described in
`ACCOUNT_C_TABLE_TENNIS_SAMPLE_AUDIT_20261028.md`. Ratings start at 1,500 and
learn only from earlier calendar days in this sparse 2,000-match sample. For
the three matches with final set margin above three, the update magnitude is
capped at three after that day; no match is selected or excluded using its
future outcome. The 2026 sample was already inspected and the 2022 source has
unresolved rights and date conflicts, so this is a post-hoc exploratory
comparison, not an untouched or production-authorized validation.

On 819 records dated September 1 or later, match-only K=10 Brier was 0.249788;
the K=20 margin rule gave 0.249094, against 0.250000 for constant 50%.
The margin rule had a lower Brier score on 29 of 50 evaluation days and a
higher score on 21. A seeded 2,000-resample day-block bootstrap of
match-minus-margin Brier gives approximately -0.000108 to 0.001455; the interval
includes zero and day independence is not established. The publisher's supplied
probability on the same 819 records had Brier 0.244679; that model may have had
much more input history and its training and timing are not independently known.

No executable prices or match settlement are supplied, and no profit or agent
promotion is implied. Reproduce with `PYTHONPATH=src python
scripts/audit_table_tennis_margin_transfer.py`; the JSON binds the source,
analysis and margin-rule hashes. The matching tests keep every evaluation row
and assert the research-only boundary; same-day causality is tested separately.
