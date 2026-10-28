# Outcome-and-odds diagnostic: implementation status

The research-only script `scripts/audit_account_c_outcome_odds.py` accepts local
CSV files and computes the frozen protocol in
`ACCOUNT_C_OUTCOME_ODDS_BACKTEST_PROTOCOL_20261028.md`.

It compares no bet, market favorite and a past-days-only Elo research challenger
with exact unit-stake payout accounting and an explicitly assumed winning-profit
fee. No same-day result enters another prediction. Retired/ambiguous scores and
missing/invalid odds are counted as exclusions; conflicting IDs fail closed.
Reports include source and evaluator SHA-256, null ROI for no bets, disclosed
drawdown ordering assumptions and explicit false forward/profit/promotion flags.

Six local fixture tests passed for payout/fees, denominator/exclusions, same-day
causality, input-order invariance, duplicate/winner rejection and nonfinite odds.
They are tests of code behavior, **not** evidence of observed profit.

The publisher's described tennis CSV source and CC BY 4.0 attribution are listed
in the protocol. As of this status, no actual publisher CSV has been acquired,
its data have not been examined, the parser has not been validated against that
source, and no real-data backtest has run. Example reproduction after obtaining
and independently validating the exact files:

```sh
python scripts/audit_account_c_outcome_odds.py --input /path/to/2021.csv \
  --input /path/to/2022.csv --output /tmp/outcome-odds-research.json
```

The dataset reports one bookmaker's prices and excludes unsettled matches; price
availability, order execution, costs and actual settlement timing remain
unverified. Results in this tennis experiment cannot establish table-tennis edge.
