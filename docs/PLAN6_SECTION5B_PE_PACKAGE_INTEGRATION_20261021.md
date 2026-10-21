# Autosport — Plan 6 §5Б: integrated Windows PE and trusted package closure (2026-10-21)

## Status

**IN_PROGRESS_NOT_DONE — NOT TERMINAL.** This receipt records two actually merged acceptance-relevant source changes, not a completed Plan 6 §5Б or released Windows product. Exact main readback performed on 2026-10-21; subsequent product changes may advance main without invalidating recorded Git blob identities.

## Reuse → integration

1. Historical PR [#1385](https://github.com/Oleksii-debug/Autosport/pull/1385) provided the original Windows AMD64 PE32+ semantic oracle and regression suite; its divergent branch was superseded, not merged directly. The canonical product now has those modules integrated via merged [#2408](https://github.com/Oleksii-debug/Autosport/pull/2408), merge commit `1f6400a2ac927a34bc2686fd3a2c43f3251a80c3`.
2. The *official* `scripts/package_windows.py` invokes the shared PE oracle for both source-SHA/ZIP-pinned, privately captured `Autosport.exe` and `Autosport-Data.exe` prior to ZIP assembly. The validator is part of the exact-Git source manifest verified independently by `release_package.py`.
3. The canonical Windows `scripts/build_windows.ps1` launches package construction with a finite, individually SHA-verified Git archive and isolated `python -I` loader. That loader lacked the newly needed PE validator and the existing transitive update admission/metadata modules. Merged [#2412](https://github.com/Oleksii-debug/Autosport/pull/2412), merge commit `ad2c1695e6eb3f2f9f389eb0dcc710ba1bd200b7`, includes all three in the immutable trusted source allowlist, source manifest and ordered loader, with negative/tamper and real-module isolated-subprocess regression tests.
4. Real `mmap.mmap` buffers originally failed the PE validator's byte-input guard; merged [#2411](https://github.com/Oleksii-debug/Autosport/pull/2411) fixed the reproduced package admission regression on `main@846c141a0074350cf9cc269ed45eff2e5641febe` and added a file-backed mmap regression.

## GitHub readback

- Post-#2408, 4/4 modified production/test Git blobs were identical on the merged head and `main@1f6400a2ac927a34bc2686fd3a2c43f3251a80c3`.
- Post-#2412, 4/4 changed paths (`scripts/build_windows.ps1`, `scripts/package_windows.py`, `src/autosport/release_package.py`, `tests/test_windows_trusted_package_consumer.py`) were byte-identical to the PR head on `main`.
- #1385 was explicitly marked superseded and CLOSED without merge after its implementation had been reused and integrated.
- The two scoped merged heads' CI/Windows candidate checks were observed as **SKIPPED or QUEUED**, not as green. Native exact-head repository pytest, packaged Windows/installer smoke, signed delivery, human/NVDA and live-money evidence were **NOT EXECUTED/NOT VERIFIED** in this work.

## Remaining §5Б approved acceptance

Complete, independently verified **bundled executable component/SBOM and licensing provenance**; prove build-time dependency/config/migration/license alignment against the shipped exact candidate rather than merely the build host; execute exact Windows build and source-release qualification where available. The preexisting `release_sbom.py` and `release_dependency_graph.py` explicitly describe build-host metadata and **must not** be relabeled as a bundled EXE SBOM or license clearance. Maintain `IN_PROGRESS_NOT_DONE`; do not mark this Section terminal on source-file existence alone.

`REAL_MONEY_EXECUTION=false`, `HUMAN_TESTED=false`, `NVDA_VERIFIED=false`, `WHOLE_PRODUCT_COMPLETE=false`.
