# Bounded recovery probe: development result

Identical synthetic snapshots were evaluated with the current sandbox v3 rule
and a research-only subclass. The results are not comparable with the real
sporting economy; the simulation awards wins on falling quotes regardless of the
match outcome. The 180-step horizon and interval of 40 were fixed before this run.

| Scenario | Baseline net | Probe net | Baseline / probe drawdown | Baseline / probe tickets |
|---|---:|---:|---:|---:|
| Persistent loss | -1468 | -1636 | 1468 / 1636 | 32 / 36 |
| Favorable | 160534.32 | 160534.32 | 0 / 0 | 180 / 180 |
| Loss then recovery | -1468 | 26728.9984 | 1468 / 1552 | 32 / 82 |

The probe discovers the constructed recovery but adds 168 UAH loss under continued
unfavorable conditions. In the recovery fixture the four scheduled exploratory
opportunities are bounded, but a positive result can reactivate normal exploitation;
therefore the total ticket count is not limited to 36. The huge synthetic gains
are an artifact of deterministic line-movement settlement and are not a forecast.

Important design defect discovered on review: the 40-step interval is divisible
by the 10-step curriculum, so every probe occurs in a parlay slot. Single-family
recovery is not tested and cannot be claimed. A future separately declared trial
must account for both families and risk budget, preserve all negative results,
and pass independent chronological evaluation with actual outcomes and costs.
Do not deploy or import this subclass into production memory. The active candidate
is unchanged, and no model promotion, forward evidence or profitability is claimed.

Reproduce with `PYTHONPATH=src python scripts/compare_account_c_recovery_probe.py
--output /tmp/recovery.json`. The JSON records exact snapshot and source hashes.
