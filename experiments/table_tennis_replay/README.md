# Twenty real table-tennis matches: recorded replay

The twenty fixtures were selected from upcoming, not-yet-live matches from
MoviOdds `/odds?sport=table_tennis`, persisted 2026-10-28T03:15:46Z, with both
match-winner prices present. The file `initial-selection.json` freezes names,
scheduled times, source fixture IDs and the actual provider data used to select.
It is a sample of the nearest qualifying matches, not a claim about profit.

The recorder polls the public feed about every 20 seconds and records an initial
observation when each match is first verified live, then a sample about every
three minutes, plus a source-reported final result. Different prematch/live IDs
are linked only when the provider's `/closing` API explicitly associates them;
names alone never create a match link. A delayed or missed sample is marked
missing, not invented; a finished match without a captured start is marked as
incomplete. Match-winner odds are read only from the corresponding match-winner
market, never from a set, handicap or total market. Suspensions and stale market
flags are retained. Source odds are quotes, not confirmed bets or wins.

Source: MoviOdds https://moviodds.com/doc; unaffiliated with bet365, reports
bet365 prices. Per provider, `score` for table tennis contains completed game
scores followed by current game points. The first observed live moment may be
up to a polling interval after play actually begins. The result's source
`finished_at` is the last time the source saw the match, not an official clock.

The run stops when all selected matches have reported finished or after 4.5 hours;
it may end with missing or incomplete matches if the provider fails, changes
identity, postpones events, or does not provide results. Progress is written on
an isolated data branch and the final dataset is retained as a workflow artifact.
The recording is public source data and never contains credentials or wagers.

## Market-anchored research, 2026-11-09

The [fixed price/history protocol](MARKET_ANCHOR_PROTOCOL_20261109.md),
[adaptive-memory protocol](ADAPTIVE_MARKET_PROTOCOL_20261109.md), and
[prospective forecast commitment](FORWARD_ANCHOR_FREEZE_20261109.md) have a
separate [results report](MARKET_ANCHOR_RESULTS_20261109.md). They compare the
market, Elo, head-to-head history, and their fixed combination after hypothetical
costs. Positive small-sample PAPER results are not production-profit evidence.

The new historical screen also rejects alleged prematch/live aliases when the
linked ID has a separate result in the archive. The demonstrated source-link
defect and scope of this repair are recorded in
[the identity note](MARKET_ANCHOR_IDENTITY_REPAIR_20261109.md). A provider's alias
assertion alone is insufficient to prove that two IDs represent one match;
this study does not retroactively revalidate the older recording described above.

`market_anchor_support.py` audits the actual hypothetical tickets, wager days,
opponent pairs, and profit concentration supporting each shadow preference.
It preserves the fixed model and distinguishes observed opportunities from
placed bets. Source responses and research checkpoints remain in the private
evidence package; this directory contains reproducible code and aggregate reports.

The follow-up [same-day history protocol](INTRADAY_FORWARD_PROTOCOL_20261109.md)
and [15-forecast commitment](INTRADAY_FORWARD_FREEZE_20261109.md) compare the same
formulas with and without today's observed completed matches. The two-ID
[timestamp-migration quarantine](INTRADAY_SOURCE_REPAIR_20261109.md) was documented
before new prices or forecasts were collected. These comparisons do not retune
the first cohort or imply that a single winning forecast establishes an edge.

The [completed same-day-history comparison](INTRADAY_RESULTS_20261109.md)
reports all 15 prospective results, seven fixed policies, the exploratory
130-match comparison, and separate observed-feedback memory. Both history arms
made identical prospective decisions: Elo lost 3.03 units, H2H lost 0.55 units,
and the combination abstained. These are hypothetical returns, not real wagers.
`intraday_history_screen.py` keeps the same H2H window endpoint in both arms
and rejects future updates and inconsistent source outcomes. The private evidence
also includes official roster references and independent score corroboration;
abbreviated names and venue halls are not verified people or club affiliations.


## Learned market residuals, 2026-11-10

`market_residual_calibration.py` learns constrained Elo/H2H corrections to the
market, with a null model, day-bootstrap sensitivity checks and explicit actions.
The [completed two-day report](MARKET_RESIDUAL_RESULTS_20261110.md) preserves both
predeclared test populations and the separate next-day learning challenger.
The update learned zero weight for both corrections; abstention is not profit.
The code passed 684 tests with two skips. Recorded forecasts, source receipts,
model hashes and independent arithmetic checks remain in the private evidence
package. These retrospective paper results do not establish executable profit.
