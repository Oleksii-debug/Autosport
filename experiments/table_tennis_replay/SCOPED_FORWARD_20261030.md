# Scoped forward PAPER observation — 2026-10-30

Research only. This is neither a trained model nor permission to use data for
training, an executable price, a bookmaker settlement or a profitability test.
No bets were placed. Sources, players, rights and physical tournament locations
are not independently verified.

## Design and exclusions

The [protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6513193439)
was posted at 02:07:44 UTC, before the first new source request at
02:07:49.053587 UTC. The original prior H2H exclusion contained 765 exact
fixture IDs. `forward_prior_scope.build_scope` revalidates that frozen prior
journal and its three dated revision links, then adds every exact fixture ID in
its three captured odds, three generic results and three dated bodies. The
resulting scoped union contains **20,001** IDs, SHA-256
`31c987453e93f05af529f9cf8b45bcdcf8391bcfce8866ba64caaba81e2710d7`.
The 19,982 generic-result IDs overlap heavily with the base; those counts are
not additive. The prior historical head was
`8842cef389259d2f52be1449d30201be3ffd31d3b2ced8547d3e112bca51c133`.

This union is limited to those recorded sources, not every past experiment.
The provider [documents](https://moviodds.com/doc) that pre-match and live IDs
can differ for one match. It does not establish a cross-provider person ID.
Fixture-ID exclusions and equal or unequal names therefore do not prove that
matches or players are independent. Among the four exact name strings on the
selected two new source fixtures, none appeared in the previous forward
session's odds boards; this narrowly scoped string comparison is not person
resolution and does not prove no earlier encounter.

The fixed rule selected the first two sorted eligible fixture IDs at each
proposal, using only results received by then and the shorter shown quote.
Proposal and action hashes were posted before the next odds and generic result
requests respectively. Each book starts with 100 hypothetical units; each ticket
assumes a one-unit stake and 0.01 unit in costs. One policy requires the later
two-leg price product to be at least the earlier one, the other accepts 98% or
more. Both use the existing `PaperBook`, `TicketLeg`, `PaperRiskPolicy`, quote
checks and revision-aware outcome adapter. No-bet stays zero.

## Observations

- Six initial market fixtures remained after the scoped ID and already-seen
  result exclusions. Both proposals chose the same pair of source fixture IDs.
- Transition 1, 33.5134 seconds: product at least 100%; one hypothetical
  ticket opened in each alternative book on **the same pair**.
- Transition 2, 47.618466 seconds: product below 98%; both policies rejected
  opening a second ticket. The product categories were independently checked
  with exact rational arithmetic from the original decimal JSON.
- Three general result responses each had 20,016 rows and were byte-identical
  (SHA-256 `5b950923e62ea71f365a4fc5228338ece09891dbbad28acd6358a85b9fe30fb9`);
  they are not three independent sets of outcomes.
- The first dated response, received 02:09:58.005608 UTC, had 38 source rows,
  all structurally valid. None of the six distinct live fixture IDs, including
  the selected pair, appeared. Both selected legs were unresolved, so the one
  ticket in each alternative book stayed pending. Its latest link was pinned
  [here](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6513227427).
  Pending stake carried at face value is not realized profit or loss.

The [separate later-recheck protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6513244996)
was published before its additional request; the recorded first outcome and
its as-of report must remain unchanged. The later recheck concerns the same
pair, not a new sample.

## Source and research boundaries

The provider's [table-tennis page](https://moviodds.com/bet365-table-tennis-odds-api/)
describes its odds as informational; it does not establish rights to train or
redistribute a dataset. Its documentation defines `finished_at` as the last
live observation, not independently verified official completion, and says
`/history` can connect a pre-match ID and a live ID. No crosswalk to an official
player registry, actual location, bookmaker acceptance or sporting outcome was
verified here. A Setka Cup participant page is an organizer source, but an
unverified name match is insufficient to merge people across providers.

Raw responses and a reproducibility bundle are private. Hash chains protect
only recorded receipts against a supplied latest pin; they cannot demonstrate
that no other source reads or corrections occurred, or that local and GitHub
clocks agree. The frozen code's single-response settlement function alone is
not revision-aware. No model was fitted, no causal benefit or calibrated
performance was claimed, and no real-money or production authority changed.

## Second predeclared dated recheck

The second request began 02:15:13.992953 UTC (more than five minutes after the
first receipt) and was received 02:15:17.841616 UTC. Among 39 source rows, all
39 structurally valid, exactly one of six observed live fixture IDs matched;
that match is one of the two selected legs. The revision-aware assessment is
**1 eligible, 0 quarantined, 1 unresolved**. Each alternative book's single
hypothetical ticket remains pending; it is neither a win nor a loss. The first
as-of report remains unchanged. The [new externally pinned link](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6513300669)
`923a74a58fcf5081cb2585adb31fe81abd45660788edf1b709fa348338d5f442`
extends the first link
`59257951b8a0913d94053160d4d1e844b142377289e15a66648fca09d19a5ba6`.
Body SHA-256 `2e2de3d3327ec521a111ee5a7814a15c6f3ff8d76bcde67f8e16909315ed28a8`;
full report SHA-256
`f00e967a60ecd4c387f9f2cc4ba3014766f4caeb0053e0d0d5ad43a7a6e53e60`.

The later recheck runner refuses a stale history pin, an early request or an
existing capture destination; malformed source response bytes are retained
before validation, and no retry is automatic. The two source records are
still not independent evidence of identities, executable prices, official
settlement or research-data rights.
