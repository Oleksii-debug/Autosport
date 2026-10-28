# Exploratory random-regime comparison

This follows the deterministic shadow-recovery experiment. Twenty seeded
synthetic price paths each start with 39 adverse moves, followed by randomly
chosen 15-step directional regimes and bounded noise. Both policies receive the
same 120 sequential snapshots. The shadow policy can spend at most four tickets
after its first shadow readmission. The sequence generator, seeds, snapshot
digests, all case results and source hashes are recorded in the accompanying
JSON. Run with `PYTHONPATH=src python scripts/evaluate_account_c_shadow_random_regimes.py`.

The capped candidate finished above baseline in 19 cases and below it in one.
Repricing the same simulated tickets with an assumed 2% fee on each positive
ticket reward leaves that comparison at 19 above and one below. This is a
post-hoc calculation, not a rerun with fees affecting bankroll or policy; it
does not include spreads, slippage, rejected prices or settlement costs.
Maximum drawdown was larger in one case, by 42 simulated UAH; the four-ticket
cap was reached in all cases. These aggregates do not represent a confidence
interval, independent holdout, expected future return, or actual matched bets.
The design follows the already inspected deterministic experiments; the
artificial trends, high quoted odds, assumed costs, and movement-based settlement
may favor this rule. A different path can lose more than baseline, as the
immediate false-signal example in the companion review shows. No simulated
gain should be presented as earnings.

No production policy, model promotion, real-money execution or forward evidence
is authorized by this experiment. A useful next evaluation requires separate
chronological data containing lawful, point-in-time executable prices, identity
binding to settled match outcomes, realistic costs and an untouched holdout.
