# Provider fixture-ID linkage audit — 2026-10-30

This is a **retrospective** research diagnostic. It does not resolve player
identities, establish official event identity or license data for training,
prove that a ticket was accepted, change a frozen decision or authorize money.

## Source and causal contract

The provider [documents](https://moviodds.com/doc) that pre-match and live
records may have different IDs and that `/closing?fi=...` returns its claimed
`fixture_ids` and, when available, `prematch_id`. This is one provider's
assertion; it does not link a person to an organizer's participant record.
The provider's [table-tennis page](https://moviodds.com/bet365-table-tennis-odds-api/)
describes its odds as informational; no license for machine learning or
redistribution was established. The requested [Setka Cup participant page](https://tabletennis.setkacup.com/en/participants/1648)
returned an application-loading shell to the available web reader, not a
verifiable current cross-provider identity match. Do not merge on names.

The [precommitted protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6513910577)
fixed the six distinct fixture IDs in the three previous odds responses (set
SHA-256 `1d9167f76b2b206e94c85f63b83f3a7626f23be10ac28fc6253bb07bd61c6146`)
and the earlier scoped exclusion set of 20,001 IDs
(`31c987453e93f05af529f9cf8b45bcdcf8391bcfce8866ba64caaba81e2710d7`).
The latter is not an exhaustive universe of previously observed data. The
frozen first decision was computed at 02:07:58.411872 UTC. Input and identity
sets, odds receipts, frozen PAPER actions and latest result-history pin were
checked using the existing research modules before any new alias request.

`fixture_alias_audit.audit_as_of` ignores records whose local receipt is at or
after the supplied cutoff. An exact prior ID or provider-claimed overlap is
flagged; a missing response, a malformed alias group, and multiple visible
responses do not silently establish novelty. `no_scoped_overlap` means only
that a structurally valid source claim did not intersect this particular scoped
set or another candidate group. It is **not** an assertion that the event was
new or independent. The full-directory `read_captures` integrity scan may
reject later damaged or incomplete files; it is not itself an as-of loader.
No raw source IDs or names are published in this report.

## Observed evidence

Six fixed `/closing` requests were made, one per ID, no credential, no retry,
with at least one second between requests. The first request began
03:03:14.813336 UTC and the final response arrived 03:04:15.644877 UTC. All
six response bodies and receipts were retained before parsing. All six
responses passed the predeclared linkage checks. Each reported a singleton
`fixture_ids` set; all six supplied a `prematch_id` equal to the sole ID in
that response. There was no source-claimed overlap with the 20,001 scoped
prior IDs and no overlap among the six returned groups. This lack of reported
extra IDs does not establish that none existed or that the feed is complete.

At the **earlier decision cutoff**, all 6/6 responses were unavailable. At the
**later audit cutoff**, 6/6 were `no_scoped_overlap`; none was marked prior
or cohort overlap, invalid, ambiguous or unavailable. The two report SHA-256s
are respectively
`2e354fc683cae1959ba8eb9e672612b2f1c6e4036436aeaf421ee4fad960ea68` and
`7a12e41d9354c792bfad65599f69969cd8ef01d2523047489124d9207655bf53`;
they were [pinned](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6513929458).
An independent read of the raw linkage arrays and receipt checksums reproduced
the aggregate singleton and intersection counts. As-of filtering does not
prove synchronization of local and source clocks or that an unrecorded feed
revision never existed.

## Related PAPER outcome

A separately precommitted [third dated recheck](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6513866175)
resolved the two previously frozen source IDs at 02:59:52.621355 UTC with 2
eligible, 0 quarantined, 0 unresolved source-reported legs. One selected side
won and one lost; the single shared pair has a **hypothetical loss of 1.01
units** in each of two alternative book copies, including the assumed 0.01
cost. An independent set-score comparison reproduced 1 winning and 1 losing
selected leg; one previously observed record remained unchanged, while the
other was absent earlier. Both earlier pending states remain recorded without
backdating. This is one dependent pair and not bookmaker settlement or an
estimate of profitability. [Pinned latest link](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6513874628):
`130ac6e916b9703ea887231a4b60e9e037d670d242612ec555c69e1f564bf295`.

## Verification and remaining limits

Nineteen focused synthetic tests cover prior overlap through a different ID,
shared aliases, conflicting source groups, ambiguous repeated replies,
malformed/missing fields, strict same-time/future exclusion, receipt tampering,
unavailable requests, no retries and source-session integrity. The broader
local Linux/Python 3.12 research/identity suite passed 253 tests before the
six source reads; syntax compilation also passed. Windows, physical NVDA,
source truth, participant cross-provider identities, source-use rights,
independence, actual prices and bookmaker settlement were not verified. No
model was trained or promoted and no real wager was made. The final GitHub CI
status must be read for the exact committed head, not inferred from local tests.
