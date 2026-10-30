# WTT archive source and chronology inventory — 2026-10-30

Research-only structural audit. These records are **not admitted** to model
training, the historical corpus, PaperBook or a production identity registry.
No betting prices, fills, profits or causal model performance are inferred.

## Source, attribution and exact bytes

The publisher's OSF project is zheng jun Li, *Racket Sports Multi-Dataset:
Tennis, Badminton and Table Tennis Professional Match Records (2018–2025)*,
[DOI 10.17605/OSF.IO/PJUW8](https://doi.org/10.17605/OSF.IO/PJUW8),
[node metadata](https://api.osf.io/v2/nodes/pjuw8/), file version 1
[`racket_sports_standardized_final.zip`](https://osf.io/download/3f6ak/).
The [publisher repository](https://github.com/Li02003121/racket-sports-dataset/tree/c5d57a90510fb22249f4975d1cae1f435b7dc517)
identifies WTT results as the source and declares the data CC BY 4.0; OSF node
metadata independently reports CC BY Attribution 4.0 International. Code in
that repository is separately labeled CC0. This records the publisher's
license claim and attribution, not independent verification of the publisher's
rights from WTT, a legal conclusion, or permission to redistribute upstream
data. The WTT archive covers professional events and is not established to
cover the target recurring Ukrainian, Czech or Slovak circuits.

One initial download attempt left no response or receipt, so its remote outcome
is unknown; a [separate protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6515399908)
was published before the subsequent read. That read started at
05:02:34.217343 UTC and returned HTTP 200 at 05:02:46.318237 UTC with standard
TLS verification. Its 2,073,125 bytes have SHA-256
`5246b5fe1ba5d1d71edcef0e23ef0ee93d7289508dd0545fc124ed569f991409`,
matching OSF file metadata. A ZIP central-directory check found six CSVs; only
the two named WTT members were decompressed. Their CRCs were checked on read;
no claim is made that the four unrelated members were decompressed or CRC-tested.

The publisher's [`fix_all4b.py`](https://github.com/Li02003121/racket-sports-dataset/blob/c5d57a90510fb22249f4975d1cae1f435b7dc517/fix_all4b.py)
contains a certificate-verification-disabled context for part of its name
lookup code. It was inspected as text, not executed here. It is unknown which
published mappings were produced by that route; the source names and IDs were
not independently verified.

## Structural results

| Source file label | Rows | Exact within-file match ID duplicates | Native participant-ID columns |
| --- | ---: | ---: | --- |
| WTT men | 5,442 | 0 | Both present on all 5,442 rows |
| WTT women | 4,850 | 0 | Not present in the file |

The filenames and README supply the above labels: `sport`, `gender` and
`dataset_id` fields are empty in all 10,292 rows. All four rank/rank-points
fields are also empty in every row. Exact `match_id` values are unique **within
each file**, but 21 exact strings occur in both files; those are source-key
collisions across tables, not proof that the two rows describe the same match.
No source-native ID in this report is an authenticated real person.

In the men's table, 753 distinct numeric raw participant-ID strings occur in
rows with two different IDs and a valid event-date label. One exact name string
is associated with two distinct raw IDs; no exact-name merge is justified. In
an explicitly **fresh empty test-only** Autosport `ParticipantIdentityRegistry`,
all 753 raw IDs remained unresolved. This does not characterize a different,
populated registry. Women's identity resolution was not attempted because the
native ID fields are absent; its unresolved state is not a count of zero.

The two conflicting IDs were checked once each on the official WTT athlete
page under a separate [protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6515447870).
Both requests returned the same 6,438-byte generic HTML body, title `World Table
Tennis`, with no requested ID, source name or JSON-LD block in that body. Thus
**0/2 mappings were confirmed**; whether the two IDs denote one or two people
is unknown. No source script, cookies, credentials, CAPTCHA or access-control
bypass was used.

All rows have a date-only `event_date` label. In the men's file, 2,925
(raw-ID, event-date-label) groups repeat and 5,438/5,442 rows include at least
one repeated ID on that label. Each table also has 82 `(event_date,event_name)`
label groups containing more than one round label. A date label is not proof
of an individual match's start/end time; there are no verified decision,
finished or result-availability timestamps. Neither file order nor a strict
prior-label-date split alone proves that earlier results were available when
later decisions occurred.

All 10,292 rows declare `best_of=7`. Using 11-point sets with a two-point lead,
and interpreting winner code 1/2 as source player 1/2, a structural parse gives:

| Declared best-of-seven check | Men | Women |
| --- | ---: | ---: |
| Nonterminal for the declared format | 5,217 | 4,628 |
| Terminal and winner-code-consistent | 222 | 221 |
| Invalid set score | 3 | 0 |
| Additional set after declared terminal | 0 | 1 |

The 9,845 nonterminal records are **not** a claim that actual matches were
unfinished. The declared `best_of` metadata, scores, or their interpretation
may be wrong. No format was silently corrected and no missing sets were
invented. Source outcomes, event identity, timings and player identity still
need independent reconciliation.

## Reproducibility and boundaries

`wtt_archive_inventory.py` checks the pinned archive hash, reads only the two
bounded named ZIP members, detects within/across-file keys, source-ID/name
ambiguity, date grouping and set-score structure, without publishing identities.
`wtt_identity_audit.py` reuses the existing canonical identity registry in a
fresh isolated test-only state; it never opens a product registry or equates
names. Thirteen synthetic regressions cover missing IDs, source-key overlap,
format inconsistencies, dates, hash corruption and no-name output. The local Linux/Python 3.12 research/identity suite passed 266 tests; syntax
compilation passed. The exact GitHub CI status is recorded in the corresponding
issue checkpoint.

Only the source publisher's license representation was located; underlying WTT
rights, event IDs, identities, actual timing and availability need evidence
before canonical ingestion or a prospective causal evaluation. Do not treat
this inventory as a clean holdout, fitted model, verified result, market
baseline or real-money authorization.
