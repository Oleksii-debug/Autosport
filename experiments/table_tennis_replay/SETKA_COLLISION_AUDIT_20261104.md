# Setka-derived sample: score and source-row collision audit

This is an exploratory source-quality check, not evidence of player identity,
the number of physical events, historical availability, model performance or
betting returns. It does not alter the source or place an order.

## Evidence and scope

The previously examined SCORE copy has 7,851 rows, Git blob
`4ff05ff9af6cbf422140b3c126be38495b402674` and SHA-256
`d400562f49ced084133c8e926025efb6023dd62a8341fd4484864d567a4e392d`.
Its [publisher](https://data.scorenetwork.org/table_tennis/table_tennis_sept2022.html)
describes first-to-three sets, date, time, two player strings and per-game
scores. The [previous audit](SETKA_SOURCE_TIME_20261104.md) documented
ambiguous source dates, one same-name-both-sides row excluded from temporal
analysis, and eight eligible rows with the same exact raw date label, parsed
time and unordered pair of exact player strings. Those aggregates were known
before this investigation; the eight rows are **not** an independent sample.

The [scope for inspecting collision scores](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6634042242)
was posted before examining those fields. After the first score result was
known, a [separate post-hoc check](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6634111875)
was added for equality of the other decoded fields. No names, score values,
individual rows or row labels are published in this report or aggregate JSON.

## Findings

- The eight previously identified rows form **four groups of two**. Both
  rows in each group have the same source side order; no group reverses it.
- All eight rows pass a structural check against the source's first-to-three
  description: completed per-game point pairs, contiguous games, declared
  set totals, and source-declared winner agree. The two normalized score
  signatures in each of the four groups are equal. No sporting result has
  been independently verified.
- In the **post-hoc** comparison, the two rows in each group have identical
  decoded CSV field strings for all 17 columns other than `X`. The `X` field
  is the publisher's row number and is different across rows. This compares
  decoded field values, not necessarily the raw CSV bytes or official IDs.

These are four pairs of repeated **source-record values**. They are not proof
of four duplicated physical matches: two events, aliases, revisions or other
source behavior cannot be ruled out. Scores and participants were not checked
against an official event record. We did not delete, merge or correct rows.
No additional conclusions follow about source completeness, rights, timestamp
semantics, timezone or availability of a prior result at decision time.

## Method and verification

`setka_collision_audit.py` reuses the prior SHA/schema and eligibility reader
and its exact-label collision count. The row-number labels must be unique;
matched pairs are selected only within the same raw date label and parsed
time. For each selected row, it reuses the existing WTT score-shape checker
with the source-described best-of-five assumption, additionally checking
source winner and set totals. Scores are normalized to the exact-label side
order only within the collision group; this is not a cross-provider identity
mapping. Missing, nonterminal, contradictory or malformed scores fail to
qualify for an equality comparison.

`setka_collision_reference.py` applies a separately written point/score
validator and gets the same aggregate result. It **shares the input parser and
selected groups** with the main calculation; it is not independent data
acquisition or a proof the source is correct. Aggregate outputs are
[`setka_collision_audit_20261104.json`](setka_collision_audit_20261104.json)
and [`setka_collision_reference_20261104.json`](setka_collision_reference_20261104.json).

Synthetic tests include reversed sides, comparable and different scores,
same-time/date grouping, missing and invalid scores, incomplete matches,
extra sets, duplicate row labels, altered SHA, exact raw-field equality and
400 generated score cases. Linux/Python 3.12: **414 local tests passed** for
`experiments/table_tennis_replay`, `tests/test_participant_identity.py` and
`tests/test_opponent_intelligence.py`, including 14 new tests; `compileall`
passed for the three new Python files. The issue records exact-head GitHub CI
status. The source file remains outside the repository; upstream rights and
official crosswalks remain unresolved. This post-selection check does not
authorize model training, an independent holdout, actual bookmaker execution
or money.
