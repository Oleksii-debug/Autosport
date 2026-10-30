# Stopped prospective overlap observation and journal preflight repair

This bounded source-observation experiment was stopped at the first violated
time guard. It did **not** produce an overlapping open-ticket portfolio, a
new scored outcome, a trained model, an accepted order or a profitability
claim. All times below are recorded local UTC; comparisons with GitHub comment
times assume clocks are comparable.

## Frozen protocol and scope

The [protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6519089799)
and [exact code checkpoint](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6519097109)
were published before the first new source request. The original parent was
`5c7d6d0e3512356ddd9250a959a980ef14b5b7b4`; the helper and its tests were
committed and read back at `985e1e97e360adb25e9d84a873d450fbae6e4abf`.
The prior research scope held 20,031 exact source IDs, SHA-256
`82b98e8946063f2ab588d23fcad4c7e853bea4ffa572236c5563192cffcb1139`.
After A's first proposal, `overlap_capture_scope.derive` validated its first
captured cycle and frozen proposal, and formed B's scope by adding the two
selected exact IDs. B's 20,033-ID scope, SHA-256
`a63319474affad0b2f7b76659f32afc2412407a323ceebdca04fde99cbc27e6a`,
was [published](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6519112861)
before B's first request. These are scoped exact source strings, not resolved
persons, complete past matches or proof that the two pairs are independent.

The source's [own page](https://moviodds.com/bet365-table-tennis-odds-api/)
describes the odds as informational. Its [documentation](https://moviodds.com/doc)
defines `finished_at` as the last time seen live. Neither is proof of data
rights for learning, actual finish, accepted prices, or bookmaker settlement.
Requests were unauthenticated, read-only HTTPS; no key, retry, bypass, POST,
or real-money transaction was used.

## Actual recorded sequence

| Stage | Observation |
|---|---|
| A1 | Odds requested 09:06:18.149292 and received 09:06:27.238033. Twelve source-eligible fixtures after prior exclusions; two exact IDs selected. Proposal frozen 09:06:31.904201 and [anchored](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6519106785). |
| B1 | Odds requested 09:07:08.765911 and received 09:07:14.109188. Ten source-eligible fixtures after B's exclusions; two selected. Proposal frozen 09:07:20.435124 and [anchored](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6519120632). |
| A action 1 | Odds2 received 09:07:40.562592, **73.324559 s** after A odds1. The interval exceeded the fixed 30–60 s window; both policy alternatives opened zero tickets. [Action anchor](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6519126899). |
| B action 1 | Odds2 received 09:08:19.845012, **65.735824 s** after B odds1. Both alternatives again opened zero tickets. [Action anchor](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6519137380). |
| Stop | A results2 arrived 09:08:41.331054. Proposal2 was computed 09:08:43.273669, **62.711077 s** after its quote, exceeding the separate 30 s maximum. `freeze_proposal` raised `ValueError`; [capture was stopped](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6519151247). |

The two observation windows overlapped, but both first actions abstained and
no assumed ticket was open. Four odds responses and three generic result
responses were recorded. A1 and B1 generic result bodies were byte-identical
(20,174 rows each); A2 had 20,175 rows and different bytes. These are source
responses, not independent samples of selected outcomes. No B results2,
third cycle, dated follow-up, result scoring or interleaved portfolio replay
was attempted after the stop. No match outcome or return is inferred.

The old journal implementation had already written the stale A proposal2 body
and receipt before raising. Their pair is retained with its actual hash and is
**invalid**, not a frozen decision authorizing another request. A's first
action was verified through the existing `verify_actions(..., through=1)`.
B's first action was independently replayed against its saved record; full
`verify_actions` could not be completed because B results2 was intentionally
never requested. The two first selected exact-ID sets were disjoint by
construction, which says nothing about source aliases or player identities.

## Narrow repair

The [observed ordering defect](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6519156114)
was that a rejected computation timestamp was checked only **after** the
immutable research-journal pair had been written. Four synthetic tests first
failed on the old code: early and stale timestamps for a proposal and an
action left misleading complete-looking pairs. The repair checks the computed
time against its captured receipt and 30-second window before writing either
file. A fifth test checks that an actual partial I/O failure still leaves the
partial evidence in place and blocks overwrite. The repair does not remove or
rewrite the stopped run's stale pair, change the original pre-capture code
pin, or resume collection. A quote that has already gone stale does not become
fresh by waiting; any new collection needs a separate frozen protocol.

The targeted Linux/Python 3.12 research/identity suite passed **313 tests**,
including five scope-derivation and five new preflight/I/O tests; syntax
compilation passed. Previous pinned evidence was replayed with the repaired
code as a backward-compatibility check. A temporary earlier extraction with an
extra file failed the existing evidence-directory verifier; it was preserved,
and the check used a freshly extracted, manifest-verified archive instead.
Exact-head GitHub CI must be stated separately.

Source identity, aliases, training rights, official results and bookmaker
execution remain unverified. Raw responses, source names and IDs are retained
privately. No data was admitted to a production identity store or learner.
