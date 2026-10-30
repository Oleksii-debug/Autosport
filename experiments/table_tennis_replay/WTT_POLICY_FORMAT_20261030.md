# Retrospective Singapore Smash 2023 format comparison

Issue #2591; exploratory source-quality work only. The event was selected after
the larger WTT corpus and its score anomalies had already been inspected. The
analysis plan was recorded [before this scoped score comparison](https://github.com/Oleksii-debug/Autosport/issues/2591#issuecomment-6516158952),
but this is not a prospective holdout or model evaluation.

## Source and comparison

- Source: zheng jun Li, Racket Sports Multi-Dataset, DOI
  [10.17605/OSF.IO/PJUW8](https://doi.org/10.17605/OSF.IO/PJUW8).
  The fixed OSF archive is 2,073,125 bytes, SHA-256
  `5246b5fe1ba5d1d71edcef0e23ef0ee93d7289508dd0545fc124ed569f991409`.
  The publisher's data-license claim does not independently establish the
  upstream WTT rights. See [the inventory](WTT_ARCHIVE_INVENTORY_20261030.md).
- WTT's [Singapore Smash 2023 ticket policy](https://worldtabletennis.com/assets/files/Singapore%20Smash%20Event%20Ticket%20Policy%20-%20V1.pdf),
  section 2, states that singles rounds 64/32/16 are best-of-five games and
  quarterfinals onward are best-of-seven. The directly retained PDF is 297,195
  bytes, SHA-256 `aa86a20cb4efc51ebfb81cf2f1028b4e286bf8eb3150bc373e6774c52c99d30a`,
  HTTP 200 received 2026-10-30T06:02:57.594531+00:00 with normal TLS verification.
  This is the advertised policy, not independent proof of actual execution or
  the identity of each row.
- Exact source filter: `event_name == Singapore Smash 2023`, a valid source
  date label in 2023, and round label `R64`, `R32`, `QFNL`, `SFNL`, or `FNL`.
  We conditionally associate these labels with their corresponding policy
  rounds. `8FNL`, `RND1`, `RND2`, `RND3` and other labels are excluded rather
  than guessed. Source files are publisher-labeled men's and women's singles;
  the relevant row-level sport and gender fields are empty.
- Score checks assume ordinary 11-point/two-point-margin sets, and winner
  codes 1/2 refer to source sides 1/2. These checks assess structural
  consistency only, not the reality or accuracy of any reported match.

| File and conditional round group | Rows | Declared `best_of=7` | Score terminal and winner-code-consistent under declared 7 | Same score under policy 5/7 |
|---|---:|---:|---:|---:|
| Men: R64, R32 (policy 5) | 48 | 48 | 0 | 48 |
| Women: R64, R32 (policy 5) | 48 | 48 | 0 | 48 |
| Men: QFNL, SFNL, FNL (policy 7) | 7 | 7 | 7 | 7 |
| Women: QFNL, SFNL, FNL (policy 7) | 7 | 7 | 7 | 7 |

Among exact event-name rows, 64 men and 63 women are excluded because their
round labels were not mapped; zero are excluded for invalid/outside-2023 date
labels. No individual source match was joined to an official result. Thus the
policy gives a concrete, plausible explanation for 96 of the prior 9,845
nonterminal-under-declared-format rows **conditional on the label mapping**;
it does not establish a correction for those rows or for the rest of the data.
Do not replace `best_of`, infer missing games, or extrapolate this policy to
other tournaments or years.

## Publisher code and identity limitations

The public publisher repository at commit
[`c5d57a90510fb22249f4975d1cae1f435b7dc517`](https://github.com/Li02003121/racket-sports-dataset/tree/c5d57a90510fb22249f4975d1cae1f435b7dc517)
contains `racket_scripts.zip` (Git blob `bc8ff728388c06d506cd5f7cb7a76c7a543ba8db`,
SHA-256 `50d30b4f46d8c90dad4efb59633f7d3d1c2091637b492162c7a4ebac5f9e9ad3`).
It was read as data, never executed. Its `patch_from_features.py` constructs
ID-to-name/country dictionaries with ordinary repeated assignment, so a later
input row would overwrite an earlier conflicting value without a conflict
check. `fix_all4b.py` similarly assigns from a women's source and contains an
insecure TLS configuration for optional webpage lookups. We did not run or
reuse that code. The input feature files, actual execution order and whether
either script produced this exact archive are not established. This finding
identifies an audit risk, not proof that the archive contains overwritten or
incorrect identities. It does not resolve the previously observed one-name /
two-source-ID ambiguity.

## Reproduction and limits

Run `wtt_policy_reconcile.py` on the pinned archive and policy PDF with
`--expected-archive-sha256` set to the source hash above. It reuses
`wtt_archive_inventory.audit` and `_score_shape`, rejects either mismatched
hash, and emits aggregates only. An independent standard-library reproduction
in the private evidence bundle uses a separate score parser and agrees on all
four groups. The source archive, official PDF and retrieved script ZIP are
retained privately; no player names or IDs appear in this report. Five new
synthetic tests cover conditional formats, exclusions, unsupported labels,
tampering, invalid scores and output privacy. Full targeted Linux/Python 3.12
suite: **271 passed**; `compileall` passed. GitHub CI status must be reported
separately for the published exact head.

The record does not establish participant identities, a match-level official
crosswalk, actual match start/finish or source-availability timestamps,
training rights, accepted prices, or bookmaker settlement. No data was
corrected or admitted to training, no model was trained, and no real bets
were made. This is not evidence of profitability.
