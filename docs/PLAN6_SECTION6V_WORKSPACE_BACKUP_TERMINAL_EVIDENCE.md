# Plan 6 — autonomous Section 6В terminal source-integration receipt

**Date:** 2026-10-21. **Status:** CLOSED_FOR_DEVELOPMENT / TERMINAL for the *approved repository-controlled implementation scope* of **Plan 6, independent atomic Section 6В**, not parent Plan 6, not provider/live gambling, not final Windows/NVDA acceptance.

## Approved capability and actual entrypoints

- Real product CLI `autosport backup-workspace --workspace <existing> --output <outside.zip>` and `autosport restore-workspace --archive <backup.zip> --destination <new-directory>` are dispatched by `src/autosport/cli.py` and executed in `src/autosport/workspace_backup.py`.
- Offline, owner-initiated, private ZIP backup, canonical deterministic manifest with SHA-256 for every member, per-file and total size quotas, no archive path aliases/traversal, cross-platform safe names, source file stability checks, no overwrites, fresh destination and atomic restore publication.
- The backup reuses canonical `WorkspaceEconomicLock` while capturing the source, coordinating with Plan 5's durable economic-writer contract rather than inventing a parallel authority.
- `os.walk` enumeration errors fail closed. Otherwise Python would silently omit an inaccessible subtree from a supposedly complete archive. The regression explicitly denies nested `os.scandir`, requires exception/no output archive and absence of leftover temp files.
- ZIP manifest length is rejected before decompression; Linux restore uses `renameat2(RENAME_NOREPLACE)`; Windows restores do not overwrite existing directories. Tests are in canonical `tests/` and reachable through normal pytest discovery.

## Exact Git integration and source readback

- Canonical [PR #2325](https://github.com/Oleksii-debug/Autosport/pull/2325) **MERGED** into `main` as `b3dd2628804326f9ecc6c57682c302be8b1842b0`.
- Merged PR candidate had source regression commit `14a07a0b1497035e5dc9340a196d7892a0b50cb3` and new test commit `4c79077238c6918152a9e86bdff1d4fa81448224`.
- Post-merge `main` source `src/autosport/workspace_backup.py` Git blob: `15765c339f777cf472766228492edb7914b537b6`; `tests/test_workspace_backup_walk_errors.py` blob: `67f83db04937458c7a1c5777c7b326b614225ca3`.
- Independently observed product `main@ff6e831473485409c7252439aa5b5d4c543e4a79` still includes the integrated source and test. GitHub compare from merge feature SHA to that main: ancestor (`behind_by=0`).
- Prior manifest pre-decompression fix and collision-safe restore path are already included in merged source, not required through another open source PR.

## Verification classification — do not conflate classes

- **ISOLATED CPYTHON SOURCE-ALIGNED:** old `os.walk` silently skipped a denied nested directory (demonstrated negative); same walk with failure callback aborted (PASS). Prior existing #2325 comments document narrow offline backup and atomic no-replace tests.
- **NATIVE FULL-REPOSITORY PYTEST:** NOT_EXECUTED because the source checkout was not available to this execution environment.
- **GITHUB ACTIONS:** exact candidate CI/Windows/Endurance queued at inspect; later skipped job workflows are NOT_EXECUTED, never qualified as PASS. No fabricated CI success.
- **ACTUAL WINDOWS ZIP, UIA, OWNER NVDA, LIVE BOOKMAKER, MONEY:** NOT_VERIFIED / NOT_AUTHORIZED. The archive is not encrypted and must stay on trusted private storage. Stop non-cooperating collector processes first; it is not a cross-process online snapshot.

**Terminal source outcome:** `remaining_approved_source_implementation=0`. The atomic engineering feature is integrated and no new development assignment is needed for Section 6В absent a demonstrated regression/new requirement. Broader Plan 6 packaging, Plan 7 PAPER/SHADOW and Plan 8 external and final-owner acceptance remain separate.
