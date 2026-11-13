# Setka-derived educational sample: date and time qualification

Research-only source audit, 2026-11-04. No model training, source-person merge,
odds request, independent holdout, settlement or real-money action occurred.

## Source and provenance

The [SCORE dataset page](https://data.scorenetwork.org/table_tennis/table_tennis_sept2022.html)
describes 7,851 rows, 18 columns and Setka Cup matches in September 2022, with
`Date` documented as `yyyy-mm-dd` and `Time` as the time of the match. It
attributes the file to a [Kaggle dataset](https://www.kaggle.com/datasets/medaxone/one-month-table-tennis-dataset)
and Setka Cup. This is a chain of publisher claims, not verified provenance.

The published CSV was fetched through the repository at commit
[`5f1d6bd9767017002b35f69bf48ac4d963612297`](https://github.com/SCOREnetworkorg/sports-data-repository/tree/5f1d6bd9767017002b35f69bf48ac4d963612297),
Git blob `4ff05ff9af6cbf422140b3c126be38495b402674`; locally computed
SHA-256 `d400562f49ced084133c8e926025efb6023dd62a8341fd4484864d567a4e392d`,
619,172 bytes. The read-only [audit protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6632619441)
was published before fetching its bytes. After a header, row-count and
first-five-date preview, the [amendment](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6632640341)
specified that the two slash-date conventions would be reported separately;
the subsequent full count was not pre-specified before that preview.

The GitHub repository's root listing has no `LICENSE` file and GitHub's
repository metadata returned no recognized license. That is not proof about
all possible permissions. The Kaggle page did not expose its license in the
accessible response. The organizer's search listing states a written-permission
condition for use of its website materials, while the current direct page
returned a loading shell. Whether those conditions apply to this particular
derivative file and whether its publisher has relevant rights are unknown.
Raw rows and names are not republished here, and the file is not admitted to
learning or to a purported independent validation set.

## Date discrepancy

The pinned file indeed has 7,851 rows and 18 columns, but **zero ISO-format
date labels**. The first five previewed labels were `6/10/2022`. On the full
file:

| Interpretation | Valid rows | Invalid rows | Interpreted coverage | September 2022 rows |
| --- | ---: | ---: | --- | ---: |
| Month/day/year | 7,851 | 0 | 2022-06-10 to 2022-07-08; 5,771 June and 2,080 July | 0 |
| Day/month/year | 2,907 | 4,944 | Valid interpretations span January–August and October–December 2022 | 0 |

Of the 7,851 labels, 2,600 have two **different** valid interpretations,
307 have the same date under both, and 4,944 parse only as month/day/year.
The counts are inconsistent with the page's claimed September coverage under
both tested slash conventions. A separate `datetime.strptime` calculation
reproduced the date totals. This does not prove which dates or events actually
occurred, nor whether a different transformation or publication error explains
the discrepancy. We do not silently relabel or correct the source.

## Exact labels and timing sensitivity

The source has 565 distinct exact player-name strings and 7,851 distinct `X`
row labels. `X` is described as a row number, not a provider match ID. One
row has the same exact player string on both sides; it is excluded from the
temporal calculation. Thus **7,850 rows** remain in 29 exact raw date-label
groups. No normalization, fuzzy merge or name-to-person claim is made.

Within each raw date-label group only, parsed times are ordered numerically;
all equal times are evaluated as one batch before updating prior state. A
prior row means strictly smaller time. The 30- and 60-minute thresholds are
inclusive on the upper bound and are illustrative spacing checks, not inferred
match lengths or publication delays.

| Structural observation | Rows, denominator 7,850 |
| --- | ---: |
| Share at least one exact name with another row at the same time | 12 |
| Have the same unordered pair of exact name strings at the same time | 8 |
| Have an earlier row that day sharing at least one exact name | 6,313 |
| Have an earlier row that day with the same unordered pair | 903 |
| Have an earlier row sharing a name within 30 minutes | 369 |
| Have an earlier row sharing a name within 60 minutes | 4,310 |
| Have an earlier row with the same pair within 60 minutes | 55 |

No earlier-pair row falls within 30 minutes under this calculation. These
figures count source rows, not verified people or distinct physical matches.
Same-time names may represent aliases, duplicates or different people; no
such explanation is proven. Equivalent date strings with different raw
spellings are deliberately separate, which may undercount coincidences.
Neither timestamp semantics nor timezone nor prior result availability is
established. Therefore even a strictly earlier label does **not** qualify a
causal feature; the 30/60-minute figures are not measured leakage.

## Reproducibility and checks

`setka_source_time_audit.py` reuses existing `_day` and `_present` checks,
verifies the caller-pinned SHA-256 and CSV structure, and reads no outcome,
score or price field into these calculations. It emits only aggregates in
[`setka_source_time_20261104.json`](setka_source_time_20261104.json).
`setka_source_time_reference.py` uses a separate quadratic pairwise counting
algorithm on the **same validated input tuples**, and a separate standard
library parser for slash-date interpretations. The two temporal counts and
month totals matched on the pinned file; the reference is not an independent
input validation or verification of source truth. Its aggregate output is
[`setka_source_time_reference_20261104.json`](setka_source_time_reference_20261104.json).

Synthetic tests cover ambiguous and invalid dates, time limits, same-time
batching, reversed pairs, 30/60-minute boundaries, row-label collisions,
missing names, shuffled data, outcome-field invariance and hash/CSV failures.
The local `python -m pytest -q experiments/table_tennis_replay
tests/test_participant_identity.py tests/test_opponent_intelligence.py` run
passed **400 tests**, including 20 new cases; the three new Python files also
passed `compileall`. The [issue](https://github.com/Oleksii-debug/Autosport/issues/2591)
records exact-head CI status. Raw source data remain outside this repository;
no data-use license or verification of player identities is inferred from
public availability.
