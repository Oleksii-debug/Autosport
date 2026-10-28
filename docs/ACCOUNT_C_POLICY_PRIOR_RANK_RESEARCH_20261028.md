# Simulated prior ranking: bounded research observation

This is a retrospective development experiment on constructed price sequences, not a live trial or evidence of betting returns. The source prior is an inspection of the simulated Account C run 40622520332; its artifact digest and limitations are recorded in `ACCOUNT_C_POLICY_PRIOR_INSPECTION_20261028.json`. No desktop policy or running campaign was modified.

The experiment retains the current warmup and current-positive admission rules. When several admitted choices exist, it ranks them using their current reward and at most two virtual prior observations. The prior cannot override WAIT or admit an action with a non-positive current reward. The control uses identical ranking with prior weight zero; the original campaign is included separately. The synthetic snapshots are shifted to 06:00 UTC, after the source artifact's 04:42:57 creation, and are explicitly constructed data, not forward evidence.

| Constructed scenario | Original reward | Control, no prior | Prior rank | Maximum drawdown (all variants) |
| --- | ---: | ---: | ---: | ---: |
| Monotonic adverse prices | -1468 | -1468 | -1468 | 1468 |
| Monotonic favorable prices | 160534.32 | 160534.32 | 162033.12 | 0 |
| Adverse then recovering prices | -1468 | -1468 | -1468 | 1468 |

Numbers are simulated UAH-equivalent reward under the line-movement model, not settled sporting outcomes. They omit realistic market execution, fees and verified odds, and are sensitive to the artificial sequence. The recovery case still waits after early losses and does not establish recovery. Three selected scenarios are insufficient to establish robustness; a prior from a different regime can mislead. The result is not authorized for promotion. Before any real import or production strategy change, independently validate provenance and environment compatibility, preserve independent ledger authority, and evaluate against a genuinely untouched chronological set of settled matches with timestamped executable odds, costs and risk controls.

Reproduce with `PYTHONPATH=src python scripts/compare_sandbox_policy_prior.py`; the machine-readable result contains the script and prior-file SHA-256 values. Focused tests check tampering, WAIT, negative admission and timestamps. Source artifact authentication and evidence limitations remain as stated in the prior inspection.
