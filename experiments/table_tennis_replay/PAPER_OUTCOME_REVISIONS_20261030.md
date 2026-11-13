# Revision-aware outcomes for a frozen paper portfolio

This research-only adapter protects the previously frozen hypothetical
portfolio from silently using a revised source outcome. It does not create a
real order, train a model, certify a player identity or establish profit.

## Demonstrated gap and repair

The earlier `forward_portfolio_journal.settle_actions` validates a single
dated response against the frozen actions and the recorded general-results
session. It does not receive other dated responses. A constructed test showed
that two independent calls can therefore report a paper win and then a paper
loss when the later dated row changes the same leg, without identifying that
dated-source correction. This is a synthetic demonstration of a limitation,
not evidence of a correction in the actual sample. The reproduction and scope
were recorded in [issue #2591](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6512404751).

The additive `forward_portfolio_revisions` adapter reuses the frozen action
verifier, dated-result audit, strict source parser, sporting signature and
existing PaperBook. It checks ordered body/receipt/link triplets against an
externally retained latest SHA-256 and retains each earlier as-of snapshot.
For each selected source fixture, an eligible row that later disappears,
becomes invalid or changes ordered source names, league, status, full set
scores, winner or last-live time becomes persistently quarantined, including
if a subsequent row reverts. A prior finished-but-ineligible row, or earlier
conflicting source identity, also blocks a later purported repair. A matching
unfinished row may become eligible at a later receipt; an `updated_at` change
alone is not a sporting correction if the row remains admissible.

A ticket with any unresolved or quarantined leg remains pending. The adapter
reconstructs each hypothetical book from the already frozen decisions and
settles eligible tickets only at the corresponding later receipt. A source
last-live time must pass the existing conservative post-action bound; it is not
independently verified official finish. Shared legs propagate quarantine to
all affected tickets. Previous reports are never backdated or overwritten.

The history supports up to 256 receipts and one writer. A partial append fails
closed for review. The generic history-link field named `forecast_sha256`
contains the final frozen **action** journal SHA in this experiment, not a
forecast. The hashes detect modification or rollback only relative to the
supplied latest pin; they cannot prove that a source request or provider
correction was not omitted, that the supplied pin is the newest, or that
local clocks and external information are correct.

## Recorded three-receipt check

The [pre-request protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6512467510)
was published at 01:13:22 UTC on 30 October 2026. The adapter code and test
digests and the prior two-link head were recorded before collection. Both
earlier dated receipts were reconstructed from checked private archives, and
the adapter reproduced the prior paper-book outputs exactly. The two-link
head before the new request was
`6b29f9b2dbeda2d88ad58faa9aa5884c3a34dfd2346df3630a391003763da20f`.

The new request started at 01:13:33.897775 UTC and its response was received
at 01:13:39.301972 UTC. The source reported `postgres`, with 23 structurally
valid rows. Body SHA-256:
`ea4bc2315669c1beec171249a1864a18a89ad1f8d1fd3a0bf8201935683c2394`.
The new history head, [published separately](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6512482517),
is `8842cef389259d2f52be1449d30201be3ffd31d3b2ced8547d3e112bca51c133`.

| Local receipt, UTC | Eligible legs | Quarantined | Unresolved | Paper ticket in each alternative book |
| --- | ---: | ---: | ---: | --- |
| 00:14:24.418777 | 0 | 0 | 2 | Pending |
| 00:20:16.272336 | 2 | 0 | 0 | Source-reported win |
| 01:13:39.301972 | 2 | 0 | 0 | Same source-reported win |

An independent comparison found no change in the two selected source records'
sporting signatures between the latter receipts. The third receipt rechecks
the **same pair and same hypothetical ticket**, not a new independent sample.
No real accepted price, authoritative player identity, training rights,
official bookmaker settlement, synchronized clock, economic edge or real
profitability was established. Prior source and experimental constraints
remain as in [the original observation](FORWARD_PAPER_JOURNAL_20261030.md).

Raw responses, the full private journal, both pre-existing reports, new linked
receipt and reproducibility instructions are retained in a private Drive
archive identified in the issue checkpoint. No raw source data or keys are
committed here.
