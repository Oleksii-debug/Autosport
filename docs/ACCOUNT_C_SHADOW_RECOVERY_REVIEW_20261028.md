# Shadow recovery experiment — not a production strategy

The prior sandbox run entered 198 WAIT steps after 32 tickets. This experiment
asks whether observing hypothetical price movements while waiting can detect a
change without opening additional PAPER tickets during those observations.
The method keeps eight recent non-neutral counterfactual movements per action;
seven favorable movements may trigger at most four shadow admissions. Only
already revealed snapshots are used. The underlying model awards outcomes for
quote changes rather than actual table-tennis results. It is entirely synthetic.

| Simulated scenario | Baseline net / drawdown | Shadow net / drawdown | Capped shadow net / drawdown |
|---|---:|---:|---:|
| Persistent loss | -1,468 / 1,468 | -1,468 / 1,468 | -1,468 / 1,468 |
| Persistent favorable movement | 160,534.3200 / 0 | 160,534.3200 / 0 | 160,534.3200 / 0 |
| Persistent recovery | -1,468 / 1,468 | 44,976.8456 / 1,468 | 669.3256 / 1,468 |
| Short recovery followed by reversal | -1,468 / 1,468 | -858.7544 / 2,503 | 669.3256 / 1,468 |
| False signal, later reversal | -1,468 / 1,468 | -1,439.08 / 1,468 | -1,439.08 / 1,468 |
| False signal, immediate reversal | -1,468 / 1,468 | -1,636 / 1,636 | -1,636 / 1,636 |

The reversal scenarios and capped variant were added after inspecting prior results; none
of these cases is independent validation. In the reversal, the drawdown grows
by 1,035 simulated UAH despite a less negative final balance. The cap of four
is only on shadow admissions, not on all later tickets: the underlying policy
may continue exploiting a positive historical mean after an admission. Thus the
experiment does not bound total subsequent exposure. A separate post-hoc capped
variant allows at most four tickets in total after the first shadow readmission.
That reduces exposure in the constructed reversal but still loses an additional
168 UAH in the immediate false-signal case. Its positive figures are
artifacts of constructed price paths and synthetic settlement, not demonstrated
profit on sporting outcomes or executable prices. Do not deploy or promote it.

Reproduce with `PYTHONPATH=src python scripts/compare_account_c_shadow_recovery.py`.
The accompanying JSON binds script, campaign and fixture source hashes and each
scenario's snapshot hash. The focused research test verifies that shadow history
is not updated before the outcome and that WAIT creates no ticket. This does
not establish live execution, actual profitability, or out-of-sample performance.
