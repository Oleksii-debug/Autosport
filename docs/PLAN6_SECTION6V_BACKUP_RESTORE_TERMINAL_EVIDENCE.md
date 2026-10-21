# Plan 6 §6В — terminal development integration readback (2026-10-21)

## Approved boundary
Exact owner-invoked workspace backup and fresh-root restore interfaces coordinated with canonical Plan-5 workspace economic writer lock. This is a *development closure*, not final Windows/NVDA/release acceptance and not an assertion of live atomic snapshots against non-cooperating writers.

## Integrated product behavior
- Product entrypoint: `autosport backup-workspace --workspace <workspace> --output <outside.zip>`; `autosport restore-workspace --archive <backup.zip> --destination <new-directory>`.
- Owner-invoked offline lock-held snapshot, 0600 no-clobber ZIP, SHA-256 inventory and bounded 512 MiB/4096 file limits, canonical manifest.
- Restore validates complete ZIP inventory, safe portable paths, case collisions, CRC/hash/size, reserved names and declared limits before creating a new destination. Linux `renameat2(RENAME_NOREPLACE)` and Windows no-replace rename deliberately prevent collision with a concurrently created target.
- No replacement of PaperBook, ledger, economic locks or model/agent authority. No real-money execution activated. Archive contains private data and is NOT encrypted or remotely backed up. Other non-cooperating writers must stop before backup.

## GitHub merge + post-merge provenance
- Canonical PR: https://github.com/Oleksii-debug/Autosport/pull/2325
- Merged exact PR head: `4c79077238c6918152a9e86bdff1d4fa81448224`
- Merge commit on `main`: `b3dd2628804326f9ecc6c57682c302be8b1842b0`
- Additional main readback SHA: `ff6e831473485409c7252439aa5b5d4c543e4a79` (subsequent unrelated merges possible).
- Byte-identical accepted-main vs PR-head blobs:
  - `src/autosport/cli.py` `d1df59dcb48fc71c17c948657e3e2a5015988d09`
  - `src/autosport/workspace_backup.py` `15765c339f777cf472766228492edb7914b537b6`
  - `tests/test_workspace_backup.py` `1b01a11f579c70e2cde2fe1025ac2ef100846869`
  - `tests/test_workspace_backup_cli.py` `368c61d6195329b7ac77ac73b5f259bb6e992e92`
  - `tests/test_workspace_backup_manifest_preflight.py` `26346750b2bfabe2570ecbfde93271fd93bacd40`
  - `tests/test_workspace_backup_walk_errors.py` `67f83db04937458c7a1c5777c7b326b614225ca3`

## Test and evidence-class truth
- Regression/CLI/failure-injection test files are committed and in main. **Hosted exact-head CI, Windows qualification and full native pytest were QUEUED / NOT EXECUTED at audit**. Do not report them as PASS. Independent isolated Linux filesystem check reproduced that plain `os.rename` can overwrite an existing empty directory; implementation uses `RENAME_NOREPLACE` on Linux. That primitive check is not an integrated product test.
- `HUMAN_TESTED=false`, `NVDA_VERIFIED=false`, `REAL_MONEY_EXECUTION=false`, `WHOLE_PRODUCT_COMPLETE=false`.
- Any concrete failed tests or demonstrated regression may reopen only the affected source contract. No speculative reimplementation of this completed atomic source scope.

**РОЗДІЛ ПЛАНУ 6 №6В — ЗАКРИТО ДЛЯ ПОДАЛЬШОЇ РОЗРОБКИ (CLOSED_FOR_DEVELOPMENT / TERMINAL).** Remaining Plan-6/7/8 sections must be handled separately.
