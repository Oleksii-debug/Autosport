# Candidate source for matched prices and results — unverified

MoviOdds documents table-tennis price history and final-result endpoints:

- https://moviodds.com/bet365-table-tennis-odds-api/
- https://moviodds.com/doc (sections on results and historical odds)

Its documentation describes historical timestamped price points, fixture and
selection identifiers, suspension, truncation, and result records containing
scores and a status. It also says that pre-match and live fixture identifiers
may differ, that some final statuses are unknown, and that `finished_at` is
the last live observation rather than an independently verified exact finish.
These are the provider's statements, not independently confirmed behavior.

On 2026-10-28 the available retrieval tool could read documentation but could
not retrieve the public `/odds` or `/results` endpoints. No payload, source
uptime, odds availability, quality, retention or result accuracy is verified.
Rights for collection, retention and product use are UNKNOWN; technical access
or the absence of an API key would not establish those rights. No authenticated
access, scraping, automated collection, promotion or real-money action is
authorized by this note.

Before any research use, obtain a permitted and independently captured sample;
record source and applicable rights, actual observation/capture times, and a
stable event-to-player-to-selection mapping. Reject truncated histories,
suspended or stale prices, unknown final statuses, ambiguous fixture joins,
unresolved corrections, and any observation after its decision cutoff. A price
change timestamp by itself is not proof that a quote remained available later,
nor does an archived quote prove that a bet would have been accepted at that
price or stake. A result needs independently recorded reveal/capture time;
`finished_at` alone is insufficient as an availability witness. Use the
project's existing outcome/settlement authorities and cost model rather than
converting provider text directly into a winning bet.

Even after data access, reserve a genuinely new chronological evaluation and
apply costs, abstention rules and conservative execution assumptions. No
profitability claim is supported by the documentation alone.
