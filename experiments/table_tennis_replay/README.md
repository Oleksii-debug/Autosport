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
