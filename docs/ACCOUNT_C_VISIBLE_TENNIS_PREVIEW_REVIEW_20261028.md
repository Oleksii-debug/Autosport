# Retrospective public tennis preview: economic pilot

Source: minmaxxxer, ATP/WTA Closing Odds 2025, public viewer, CC BY 4.0,
https://huggingface.co/datasets/minmaxxxer/atp-wta-closing-odds-2025.
The normalized first 100 displayed rows are in
`examples/research_tennis_preview_202412.csv` (SHA-256
`99f74d0c86d7556c5448b7cc80d6c75327bd39827174b8e50ea47147bcc7300c`).
Normalization retains match ID, timestamp, player IDs, winner, score and
reported best closing prices; retired rows have their status represented as RET.
The CSV columns called `cote*_ouverture` are adapter field names and contain
**best closing**, not opening odds. Input is attributed, reduced and modified.

This is a 100-row already viewed tennis preview, not table tennis. The publisher's
result and price provenance, actual order fill, availability and simultaneity of
best prices are unverified. The date split was selected after some preview rows
were visible. This is neither independent holdout nor promotional evidence.

Thirty-two results before 30 December 2024 warmed the fixed Elo model. Of 68 later
events, 65 were eligible and 3 retired/ambiguous events excluded. Hypothetical
one-unit stake, reported best closing prices and 2% of winning profit fee:

| Control | Bets | Wins / losses | Net units | Return on stake |
|---|---:|---|---:|---:|
| No bet | 0 | 0 / 0 | 0 | unavailable |
| Market favorite | 65 | 51 / 14 | +18.2028 | +28.00% |
| Cold-start Elo + edge threshold | 58 | 9 / 49 | -27.9202 | -48.14% |

Both are hypothetical model outputs, not actual earnings or verified fills. The
reported best prices can be from different bookmakers and the small, preselected
sample can strongly distort the estimate; no inference of future profitability.
Drawdown is ordered by match start rather than known settlement order.

An explicit breakdown of evaluation opportunities found both players unseen in
47 of 65, exactly one seen in 10, and both seen in 8; ratings update only after
each day. The poor result motivates a separate **post-hoc** cold-start abstention
trial; it does not prove cold start is the sole cause or justify promotion.

Seven focused tests pass for exact hypothetical payouts/fees, missing-data
handling, same-day prediction causality, input-order invariance and date fencing.
Run from repository root:

```sh
PYTHONPATH=src python scripts/audit_account_c_tennis_preview.py \
  --input examples/research_tennis_preview_202412.csv \
  --output /tmp/tennis-preview-result.json
```

Independent, broader, licensed data and an untouched prospective evaluation are
required. `profitability_claim=false`, `promotion_authorized=false`,
`forward_evidence_eligible=false`, `real_money_execution=false`.
