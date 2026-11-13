# Table tennis: pre-kickoff test, set-score replication and refit — 2026-11-13

**No durable profitability established.** Two distinct boards produced 13
forecasts, with 12 source-resolved. Unchanged H2H returned
**+0.185000 units on 2 resolved hypothetical tickets**,
with 1 winning. All eight rules and abstentions are retained below. These are
not actual wager returns. The previous November 9 prospective H2H results
(+0.53 and −0.55) remain separate, and the November 9–10 retrospective positive
result is not substituted for either prospective test.

## Pre-kickoff cohorts

Same five-league eligibility, underlying history, archived model weights, rule thresholds,
and cost model. Two distinct scheduled kickoff windows and no repeated source
IDs. Both forecast digests appeared on the existing research branch before
scheduled kickoff, and neither cohort contributed features to the other.
Matched exact source IDs and oriented league/player labels only. Quoted prices
and source-reported scores are not accepted fills or verified player identities.
The later response was shared byte-for-byte between the two allowed follow-ups.
The first read's partial result is separately preserved.

### Board 1

Forecast commitment `214915efcb481bf9cd1b19c344c7c162c820ceb5`; forecast SHA256 `e3462b898a22687bcb28c6b280d466f6f47104311721789be2f366965a243ff2`.
Board receipt 2026-11-13T10:18:14.158438+00:00; 8/9 resolved;
1 pending and 0 conflicting.
League coverage: {'TT Elite Series': 1, 'Setka Cup': 6, 'TT Cup': 2}.
Prematch exclusions: {'fixed_cap': 0}.
Unresolved hypothetical actions: market=0, elo_anchor=0, h2h_anchor=0, combined_anchor=0, fixed_mean=0, fixed_guard=0, adaptive_mean=0, adaptive_guard=0.

| Rule | Settled tickets | Wins | Net | Stress net | Max drawdown |
|---|---:|---:|---:|---:|---:|
| market | 0 | 0 | +0.000000 | +0.000000 | 0 |
| elo_anchor | 0 | 0 | +0.000000 | +0.000000 | 0 |
| h2h_anchor | 2 | 1 | +0.185000 | +0.117500 | 1.01 |
| combined_anchor | 0 | 0 | +0.000000 | +0.000000 | 0 |
| fixed_mean | 0 | 0 | +0.000000 | +0.000000 | 0 |
| fixed_guard | 0 | 0 | +0.000000 | +0.000000 | 0 |
| adaptive_mean | 0 | 0 | +0.000000 | +0.000000 | 0 |
| adaptive_guard | 0 | 0 | +0.000000 | +0.000000 | 0 |

| Probability model | Brier | Log loss |
|---|---:|---:|
| market | 0.25907084 | 0.71632280 |
| elo_anchor | 0.24906040 | 0.69336157 |
| h2h_anchor | 0.26877719 | 0.74065830 |
| combined_anchor | 0.25837786 | 0.71536869 |
| fixed_mean | 0.26133626 | 0.72186877 |
| adaptive_mean | 0.25907084 | 0.71632280 |

### Board 2

Forecast commitment `fae290f14e01d67583791ff61be2d2fb31694540`; forecast SHA256 `7a3c0f80acb98c7515bb5dbaca1ceeb9e024f707d491bf3e6f44cd20dab4194f`.
Board receipt 2026-11-13T10:36:07.522730+00:00; 4/4 resolved;
0 pending and 0 conflicting.
League coverage: {'Setka Cup': 3, 'TT Cup': 1}.
Prematch exclusions: {'winner_price_absent_stale_or_ambiguous': 1, 'fixed_cap': 0}.
Unresolved hypothetical actions: market=0, elo_anchor=0, h2h_anchor=0, combined_anchor=0, fixed_mean=0, fixed_guard=0, adaptive_mean=0, adaptive_guard=0.

| Rule | Settled tickets | Wins | Net | Stress net | Max drawdown |
|---|---:|---:|---:|---:|---:|
| market | 0 | 0 | +0.000000 | +0.000000 | 0 |
| elo_anchor | 0 | 0 | +0.000000 | +0.000000 | 0 |
| h2h_anchor | 0 | 0 | +0.000000 | +0.000000 | 0 |
| combined_anchor | 0 | 0 | +0.000000 | +0.000000 | 0 |
| fixed_mean | 0 | 0 | +0.000000 | +0.000000 | 0 |
| fixed_guard | 0 | 0 | +0.000000 | +0.000000 | 0 |
| adaptive_mean | 0 | 0 | +0.000000 | +0.000000 | 0 |
| adaptive_guard | 0 | 0 | +0.000000 | +0.000000 | 0 |

| Probability model | Brier | Log loss |
|---|---:|---:|
| market | 0.15024527 | 0.48650361 |
| elo_anchor | 0.15943817 | 0.50682370 |
| h2h_anchor | 0.13761301 | 0.46082114 |
| combined_anchor | 0.14825956 | 0.48343654 |
| fixed_mean | 0.14692110 | 0.47980512 |
| adaptive_mean | 0.15024527 | 0.48650361 |

The repeated cohort is another same-day board, not another independent day.
The first comparison is unchanged H2H versus market; other rules are controls.
All actions frozen before result collection, one-unit hypothetical exposure,
0.01 cost and 2% haircut on payout; stress is 5% on identical tickets.
Drawdown is in scheduled kickoff order. Rows without valid results are not
imputed or counted as settled. Pair-cluster comparisons are descriptive;
shared players and same-day dependency are not resolved by resampling.
Over all 13 forecasts, allowing either result on each unresolved row yields
a paired H2H-minus-market Brier range of
-0.00056285 to
+0.00493166 and log-loss range of
+0.00176625 to
+0.01277401. Negative favors H2H.
These are missing-outcome sensitivity bounds, not statistical confidence limits.

### Price and portfolio diagnostics after the first result read

The separate closing-price read found 6 strict links among nine
first-board fixtures. On both H2H tickets, entry to closing odds were 2.5 to 2.5; 2.25 to 2.375.
Neither entry price beat the closing quote; normalized closing prices implied
negative net return at both entries after the assumed costs. This is a market
proxy, not a calibration guarantee or a claim about executable quotes.

Using the same two choices and total stake, the canonical PAPER ledger replay
gave 0.1850 for singles and -2.01
for a hypothetical single parlay in the observed one-win/one-loss scenario.
The latter assumes multiplied prices, without any confirmed combined quote or
joint win probability. All four outcomes and dependence bounds are in evidence.
Neither post-hoc diagnostic changes an action or promotes a portfolio rule.

## Set-count alternative: separate historical evidence

Exploration on the already examined November 9–10 cohort produced +2.6185
on five set-count tickets versus +3.7045 on eight match-winner H2H tickets.
The separately fixed November 11–12 replication selected 128
IDs and admitted 77. Exclusions: {'kickoff_outside_window': 3, 'ambiguous_fixture_link': 24, 'alias_has_separate_result': 21, 'ambiguous_selection': 2, 'invalid_or_unavailable_result': 1}.
The historical source already contained outcomes before the protocol; features
use only source-reported pre-day times. This does not prove historical local
availability or prospective performance. There was no alternative threshold
search. Only the same rule can be pooled across the two historical days.

### 2026-11-11

36 eligible matches.

| Rule | Settled tickets | Wins | Net | Stress net | Max drawdown |
|---|---:|---:|---:|---:|---:|
| market | 0 | 0 | +0.000000 | +0.000000 | 0 |
| h2h_anchor | 2 | 2 | +1.540667 | +1.431667 | 0 |
| h2h_set_anchor | 2 | 2 | +1.442667 | +1.336667 | 0 |

### 2026-11-12

41 eligible matches.

| Rule | Settled tickets | Wins | Net | Stress net | Max drawdown |
|---|---:|---:|---:|---:|---:|
| market | 0 | 0 | +0.000000 | +0.000000 | 0 |
| h2h_anchor | 2 | 1 | -0.256000 | -0.310000 | 1.010 |
| h2h_set_anchor | 1 | 1 | +0.754000 | +0.700000 | 0 |

### Both historical days

| Rule | Settled tickets | Wins | Net | Stress net | Max drawdown |
|---|---:|---:|---:|---:|---:|
| market | 0 | 0 | +0.000000 | +0.000000 | 0 |
| h2h_anchor | 4 | 3 | +1.284667 | +1.121667 | 1.010000000000000000 |
| h2h_set_anchor | 3 | 3 | +2.196667 | +2.036667 | 0 |

| Probability model | Brier | Log loss |
|---|---:|---:|
| market | 0.24423013 | 0.68002689 |
| h2h_anchor | 0.24390096 | 0.68084743 |
| h2h_set_anchor | 0.24466328 | 0.68192346 |

Pair-cluster differences are in the evidence. The set-count
rule is still a candidate; no outcome-based promotion or replacement of H2H.

## New research memory

The predeclared update appended all 12 independently
verified resolved pre-kickoff rows, including abstentions, to the exact old
200 rows. Retrospective set-count results were not
added. New fit: 212 rows, weights [0.0, 0.0],
109/200 resamples at both weights zero. Model SHA256
`8e53ddffd2b2411cace364fd1648e7da2bc0121e4014d6f499235468019cf4eb`; cutoff 2026-11-14T00:00:00Z. The two prior models remain
unchanged. This fit has no untouched test yet and does not change earlier
results, production agents or release artifacts.

The existing shadow-memory implementation also preserves
23 earlier and 12 new
pre-kickoff observations, including abstentions, in a separate verified
checkpoint. Its raw research preference is `WAIT`. This is
neither an execution decision nor evidence that the rule generalizes. No
retrospective simulated rewards were added to this checkpoint.
Across November 9 and 13 prospective cohorts, unchanged H2H has
+0.165000 net units
on 5 hypothetical tickets
over two dates, falling to +0.005357 under the same stress
haircut. The separate November 9–12
retrospective H2H comparison has +4.989167 units on twelve hypothetical tickets;
do not combine the prospective and historical returns as if one live track record.

## Player history and verification

The refreshed archive contains 31609 raw rows.
After validation and two known timestamp migrations quarantined, the private
inventory covers **29524 matches,
1331 league/name labels and 9235 observed pairs**;
6263 pairs have multiple matches. The observed pairs
are 3.82% of 241,711 possible
within-league label pairs; unobserved pairs remain unknown, not inferred
matches. Names are not verified global people or club memberships. Prior public
directory snapshots yield 181 unique
name candidates for TT Elite Series and 52 for
TT Cup. These are name-only profile links and establish no club affiliations.
Removing those two rows from the pre-decision
history changed no probability or action for either frozen board. Official TT Cup
report yielded no unique candidate for three TT Cup forecasts; its scores were
not substituted. Accessed match-winner feed and report:
https://moviodds.com/doc and https://ttcup.com/reports/matches/.

Research and shadow-memory checks: 696 passed, 2 skipped in 17.54s. Full product test collection
was blocked by missing `tk_uia` and `hypothesis` dependencies; no full-suite or
Windows release claim. Separate offline checks
reconstructed source populations, historical feature counts, frozen actions and
rational-number payouts. Data and receipts remain in the private evidence
package. Research PR #2592; no scheduled tasks or real-money execution.
