# Source-recorded live flags: exploratory results

Historical source: TipsAudit track record snapshot SHA-256
`dd733c32609cd5f5bc4c2d6975a7ab2dca89559e965780e3fea90a0c24567d6e`.
The unchanged original strict-time protocol admitted only 46 settled flags because
1058 otherwise eligible rows had differing preimage and recorded timestamps.
Its current-rule cohort had 8 hypothetical selections, +0.080 units after 1%
cost: far too few to assess a strategy.

A separate time-sensitivity analysis accepted a hashed preimage timestamp before
or equal to recorded time, with both before kickoff and a maximum 90-minute gap.
This rule was revised after inspecting the source timestamps and the first result;
it is exploratory, not preregistered independent validation.

| Cohort, with 1% stake-cost stress | Settled | Hypothetical net units | ROI | Maximum drawdown |
|---|---:|---:|---:|---:|
| All source flags that met the recorded v2 checks | 1100 | -5.100 | -0.46% | 42.800 |
| Current-rule approximation, capped five per kickoff day | 69 | +7.000 | +10.14% | 7.100 |

The 69-candidate day-block bootstrap ROI interval is -13.96% to +38.35%, so the
apparent gain is compatible with loss or luck. It is a different cohort and data
construction from the earlier 88-selection historical three-book proxy (-8.170
units); neither is an independent head-to-head causal comparison. This is the
provider's first recorded signal; the running API observes the current signal
and may identify different moments, prices or selections. The source timestamps,
flag seal, source results and odds are not independently verified in this study.
No acceptance, account limits, overlapping capital, taxes or real-money profit
is established. Multiple hypotheses were already examined on related data.

Post-hoc execution-sensitivity illustration: 49 of the 69 settled selected flags
had a provider-recorded later re-quote. On this future-conditioned subset, the
original-price result after 1% cost was +3.00 units and the re-quote-price result
was +2.41. The missing 20 are not assumed fillable. Even the 49 re-quotes are not
proof a bookmaker would accept an order or that the user could obtain those prices.
This subset must not be used as a pre-match filter or counted as executed bets.

The research protocol and results, including the initially restrictive check,
are retained. Eleven focused Linux/Python tests passed for both source-time
rules, preimage mismatches, duplicate/void/pending behavior, outcomes hidden from
selection and conditional re-quote accounting. No production strategy is promoted.
Source: TipsAudit (2026), https://tipsaudit.com/data, CC BY 4.0.
