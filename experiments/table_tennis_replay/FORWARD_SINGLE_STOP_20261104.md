# Forward PAPER session stopped on proposal deadline — 2026-11-04

Research only. No live order or bet was placed; prices, identities, training
rights and official outcomes were not independently verified. A stopped run
must not be resumed or silently replaced.

## Fixed design and checkpoints

The [protocol](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6631559732)
was posted at 13:13:24 UTC, before the first request. Its code was fixed at
`eb75a5c928c8bfdb7be6cbc071afcb73d523d065`. The scope reconstructed from the
previous stopped archive contained 20,188 exact source IDs, with SHA-256
`1b96321aae1cc134b3a909701898dd37a471e60312f2a38890a553e46e12a822`.
It is not a complete universe or a verified physical-match crosswalk. The two
alternative hypothetical books each used a 100-unit initial balance, 1-unit
stake and an assumed 0.01-unit cost per opened ticket.

| Event | UTC local receipt or freeze | GitHub checkpoint |
| --- | --- | --- |
| Odds 1 received | 13:13:44.401911 | — |
| Generic results 1 received | 13:13:49.329465 | — |
| Proposal 1 frozen | 13:13:50.882111 | [13:14:05](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6631574465) |
| Odds 2 requested / received | 13:14:15.402300 / 13:14:20.909371 | — |
| Action 1 frozen | 13:14:22.259608 | [13:14:41](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6631588092) |
| Generic results 2 requested / received | 13:14:51.205529 / 13:14:59.265023 | [stop](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6631612506) |

GitHub comment timestamps have second resolution; source timestamps use the
operator's local clock and cannot independently establish synchronized time.
The proposal and action body hashes are respectively
`53399c540d9fba4d0b3813eecb1eb4afdff0b7f4856b03b65d5239c77841188f` and
`fdf3d170381c41ff3b5fee30fdf6681d1e77c29e7a9ec1ac6c595b9bb367a3ce`.
Odds receipts were 36.507460 seconds apart. The initial board had 17 source
rows, 16 eligible before and after exact-ID exclusions; the fixed rule selected
two. The later product was in the `at_least_100_percent` category, so each
alternative book assumed one open ticket on the same pair. These are not
independent tickets, real accepted prices or evidence of profitability.

The second generic-results request started 30.296158 seconds after odds 2 and
returned 38.355652 seconds after it. `freeze_proposal` rejected computation
outside the 30-second limit; **no proposal 2 was frozen**. Per protocol, no
odds 3, generic results 3, dated follow-up or retry was requested. The
transcribed `--base-head` passed to the `results2` command was incorrect, but
the runner reads that argument only in `prepare`; its `--history-head` and
`--scope-sha` were correct. This did not cause the timing failure.

Offline `verify_session` validated all four retained response/receipt pairs;
`verify_actions(..., through=1)` reconstructed the first frozen action and
confirmed it preceded the second generic-results request. The two generic
responses contained 24,328 and 24,330 distinct exact fixture IDs, respectively;
neither contained either selected exact ID. These counts are overlapping
snapshots, not independent outcomes. The hypothetical pair remains unsettled;
absence of an exact ID does not rule out a provider alias or a later result.

## Identity and source limitations

The existing identity audit found no populated fields among its eight checked
candidate player-ID keys on either 17-row odds snapshot. It found four distinct
exact name strings across the two selected fixtures, none present in the odds
boards of the two archived 2026-10-30 stopped sessions. This limited comparison
does not establish different people, event independence, or a cross-provider
identity. Fixture and selection IDs are not treated as person IDs. The
provider describes its displayed odds as informational and its `finished_at`
as a last live observation; verified sporting outcomes, data-training rights,
actual fills and bookmaker settlement remain absent.

The [private archive](https://drive.google.com/file/d/1obzFGvXUk0DQTfC6jent1_XTFfZFWriB/view?usp=drivesdk)
contains the response bodies, receipts, prior scope, frozen first decision,
identity audit and a capture trace. It has 21 manifest entries, size 2,354,561
bytes, and local SHA-256
`d642f197c0d4a1c7a1c7d5daf5b0aa867d695946ed3f7ba08b48b9ed04d3fdb5`.
The Drive metadata reports `shared: false` and one owner permission; the
connector did not report a remote SHA-256, so none is claimed.

## Preservation for future work

`stopped_single_scope.py` verifies that pinned archive, its manifest, four
source receipts, first proposal and first action, then adds all recorded exact
fixture IDs to the prior scope. The reconstructed future exclusion contains
**24,346** exact IDs, SHA-256
`491bdaacc05990349e387fb1e68de02a294394e84d267c3dc42281a3fe6bbbc3`,
as of 2026-11-04T13:14:59.265023Z. The historical head remains
`130ac6e916b9703ea887231a4b60e9e037d670d242612ec555c69e1f564bf295`;
the stopped archive is a separate evidence anchor, not an appended dated
history link. The list itself stays private and can be reconstructed from the
two separately pinned archives. No identity or holdout independence is implied.

For *future separately pre-registered sessions*, `forward_scoped_capture.py`
now checks the verified quote timestamp before requesting generic results and
stops if its 30-second window is already exhausted or the clock is earlier
than the quote. This cannot guarantee that an admissible request returns and
freezes in time; the final journal check remains in force. It does not alter
or authorize continuation of this stopped session. Synthetic tests cover both
deadline boundaries, missing quotes, no-source-call behavior, tampering and
scope reconstruction. The local run of `python -m pytest -q
experiments/table_tennis_replay tests/test_participant_identity.py
tests/test_opponent_intelligence.py` passed **380 tests** with the repository's
`src` and `experiments/table_tennis_replay` on `PYTHONPATH`; `compileall`
also passed for the six changed Python files. These checks do not establish
source truth or model quality.
