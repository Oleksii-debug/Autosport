# Sequential paper decisions frozen before later source results

This is a bounded table-tennis research observation, not a trained model,
bookmaker order or proof of profit. The [protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6511603324)
and decision-code SHA-256
`5ccd05ed08bc4b4e8fc4a1944d76ee5cc98b5a11fbf7d046a966b99b47b2b441`
were published before the first new request. The code used for collection was
not changed after the protocol; two additional tests were added afterward.

## Method

Three odds and three general-results responses were saved with local receipts.
The 765 exact fixture IDs from the pinned prior observation and dated source
history were excluded, as were IDs already present in this session's results
by the relevant proposal. This is not an exhaustive exclusion of every earlier
source or a cross-provider identity mapping. For each of two transitions, the
first two remaining eligible fixture IDs were selected at the initial snapshot,
with the shorter-priced side and home on a tie. The proposal was frozen before
the next odds request. The next quote had to satisfy the existing exact market,
identity-string and selection checks, with an observed receipt gap of 30–60
seconds. The action journal was frozen and its hash published before the next
general-results request.

The same fixed pair was tested in two **separate** counterfactual PaperBook
instances using existing PaperRiskPolicy. One required no decline in the
two-price product; the other allowed a decline of up to 2%. Each started with
100 hypothetical units and risked one per opening, with a separate illustrative
0.01 unit cost and the existing reserve guard. A NO_BET baseline opens nothing.
All hypothetical stakes remained open throughout the decision sequence; no
result could fund an earlier action. A dated follow-up can settle only when both
legs meet the existing exact-ID, ordered source labels, full set scores,
source-time and correction checks. The next general-results request start is a
conservative upper bound after each action's publication, not an official finish
time. Missing or conflicting outcomes remain pending.

## Observation, 30 October 2026

The first new odds request started at 00:12:34.020949 UTC. Six source fixtures
passed the first market and exclusion filters; two were selected. The first
transition's observed receipt gap was 33.843998 seconds and the later product
met both thresholds, so each independent policy opened **one hypothetical
ticket**, using the same two source fixture IDs. The first action SHA
`b051267d66c5a85174ae399379f3f98239433596086f64cf9b15290d303a658f`
was [published](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6511621466)
before the following results request.

The next proposal selected the same two fixture IDs. After 33.767865 seconds
the later product was below 98% of the preceding product, so **both policies
rejected** a second hypothetical opening. The second action SHA
`dfc31977e2505fd4ff4699263ddf38b1572b08864cfeeea9473de7a0c215aa7b`
was [published](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6511631854)
before the following results request. No second ticket was opened, so peak
open tickets sharing a fixture was one in each policy. An independent Fraction
check reproduced both product categories from the recorded source prices.

The single dated follow-up was requested at 00:14:21.061663 UTC and received
at 00:14:24.418777 UTC. Its SHA-256 was
`2a2e776a609d47157aa5e5226271b25ebcb4e2bf107761a249aa9321fd74698d`;
the source identified `postgres` and returned four structurally valid rows.
Neither selected fixture appeared, so **one ticket remains pending in each
separate paper policy**; there are zero established wins or losses. The 0.01
unit cost is a hypothetical stress, and carrying pending stake at face value
is an accounting convention, not a recoverable balance or realized result.
The three general-results bodies were byte-identical, not three independent
outcome updates.

As a post-selection descriptive check, three of the four exact source name
strings for the first two fixtures appeared in the earlier known source data.
Exact-string overlap does not prove person identity, independence, club,
venue or geography. The two policies and two transitions share fixture IDs and
must not be counted as independent observations.

The public hash anchors and ordered local receipts are reproducible, but they
do not independently synchronize clocks or prove that the feed showed a live
and executable price at an exchange or bookmaker. The provider's `finished_at`
is its last-live observation, not an independently verified finish. No source
rights for training, person identities, bookmaker settlements, accepted prices,
profitability, Windows or NVDA evidence are established. No model training,
production-book update, real-money action or promotion occurred.

The raw observations, receipts, journal, prior pinned history and reproduction
instructions are stored in a private Drive archive referenced in issue #2591;
the public repository contains no raw provider data or keys.
