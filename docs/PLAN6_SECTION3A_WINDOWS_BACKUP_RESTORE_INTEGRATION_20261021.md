# Plan 6 §3А — packaged Windows backup/restore command integration (2026-10-21)

**Status:** accepted scoped source integration; §3А **IN_PROGRESS_NOT_DONE**. This receipt does **not** close the full 3А, Plan 6, Plan 7 or Plan 8.

## Acceptance result

Merged [PR #2430](https://github.com/Oleksii-debug/Autosport/pull/2430) into canonical `main` at exact integration commit `9ab7233958fa87b12cd0e89c386f3a5452dc633b`. Live postmerge main was identical to this SHA at the first readback. On `main`:

- `src/autosport/data_tools_entry.py`: Git blob `0abae79967a1b014852305492d70729a280e0ffd`.
- `tests/test_plan6_3a_windows_backup_restore_dispatch.py`: Git blob `bf695c190f1a7a2d8c974c9458d2a61ee36c60df`.

Both are the accepted exact branch blobs after the merge. No unmerged PR is needed for these two commands.

## Functional scope

The shipped `Autosport-Data.exe` now documents and dispatches the existing `backup-workspace` and `restore-workspace` to `autosport.cli.main([command, *args])`. The canonical CLI already owns SHA-256 file inventories, verification, no-overwrite fresh-directory restore and Plan 5 economic-lock coordination. There is **no new backup writer, privilege or file format**; Plan 6 §6В's previously terminal core is preserved.

## Verification boundary

Eight connector source/readback structural checks passed (command help, handlers, uniqueness, test presence, expected blobs). Four test functions are committed, six parameterized test cases including failure-code propagation and unknown-command rejection. **Do not claim they executed:** hosted CI and Windows candidate were queued on PR readback; no full checkout `pytest`, actual packaged EXE, keyboard/NVDA human test, bookmaker or real-money execution was run here.

Remaining Plan 6 §3А tasks stay open and must be evaluated against its complete, previously approved Windows operator workflow matrix; 5Б, 6А, 7А and 7В are independent unfinished sections. `REAL_MONEY_EXECUTION=false`, `HUMAN_TESTED=false`, `NVDA_VERIFIED=false`.
