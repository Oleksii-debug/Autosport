# Setka result-history exploration — source rights and timing unresolved

The SCORE Sports Data Repository describes a dataset of 7,851 Setka Cup matches
and links its source to Kaggle and Setka. The inspected CSV was retrieved at
repository revision `66a314e63ca1ce649d24f4daf48b5564ead01e34`:
[source CSV](https://github.com/SCOREnetworkorg/sports-data-repository/blob/66a314e63ca1ce649d24f4daf48b5564ead01e34/data/table_tennis-sept2022.csv)
and [source description](https://data.scorenetwork.org/table_tennis/table_tennis_sept2022.html).
The inspected bytes have SHA-256
`d400562f49ced084133c8e926025efb6023dd62a8341fd4484864d567a4e392d`.
No license or sufficient reuse rights were established; the original CSV is not
redistributed in this repository and cannot be used for product promotion on
this evidence. The audit script accepts a local CSV and reports a source URL
only if its exact hash matches these bytes.

The page calls the dataset September 2022, but all raw date values parse as
month/day/year from 2022-06-10 through 2022-07-08; day/month parsing fails on
4,944 rows. This is a source conflict, not independent proof that the parsed
dates are correct. Time zone and actual availability times are not established.
One row, ID 4376, has the same player name on both sides and is explicitly
excluded. Other abbreviated names may collide; identities are not independently
verified. The remaining 7,850 rows have distinct IDs and winner fields agree
with recorded set counts. Results are source assertions, not independently
verified settlements.

## Exploratory chronological comparison

Using the only date interpretation that parses all rows, 5,770 matches before
July 1 select an Elo K from {0, 5, 10, 20, 40, 80}; K=10 minimizes Brier score
on those earlier records. Every day uses pre-day ratings, then applies that
day's outcomes only after its predictions. Later dates can learn from previously
completed dates. On 2,080 July records:

| Model | Brier score | Accuracy |
|---|---:|---:|
| Constant 50% | 0.250000 | 50.05% |
| Fixed K=20 | 0.242897 | 56.39% |
| Earlier-period-selected K=10 | 0.242834 | 56.39% |

The observed later period covers only eight interpreted dates and has already
been inspected; it is not an untouched confirmation sample. Day batching does
not fix incorrect date labels or identity collisions. Tie predictions choose
the first named player. This file has no bookmaker quotes, execution, costs or
market settlement, so there is no profitability or financial-edge claim. The
experiment is not promoted to agents.

Reproduce, after separately obtaining the exact file and resolving appropriate
rights, with `PYTHONPATH=src python scripts/audit_setka_result_history.py --csv PATH`.
The companion JSON binds source, model and script hashes; tests cover duplicate
IDs, contradictory results, excluded self-matches and non-attribution of
unrecognized CSVs. The unresolved source conditions must be addressed before
any product use.
