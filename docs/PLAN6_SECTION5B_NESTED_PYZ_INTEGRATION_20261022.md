# Autosport — Plan 6 §5Б: nested PYZ source integration receipt (2026-10-22)

## Actual main integration
- Code PR [#2455](https://github.com/Oleksii-debug/Autosport/pull/2455) is **MERGED**, merge commit `3f9ce84b7d883cc8bdb307a71d50847a1d3c8eeb`.
- Its accepted branch head `220c50cb8e22c085449ddba152498c380c955971` converged the concurrent aggregate CArchive packed/unpacked resource guard from PR [#2454](https://github.com/Oleksii-debug/Autosport/pull/2454) with this nested PYZ implementation.
- Main readback at that exact merged SHA: `src/autosport/release_sbom.py` Git blob `1b31a43d501ae1a9c326921970aaa5d9e4cf9a08`; it contains both `max_total_packed = 1 << 30` and `EMBEDDED_PYINSTALLER_CARCHIVE_AND_PYZ_TOC_ONLY`, with `reader.open_embedded_archive(...)` under the sealed snapshot.

## Accepted functional scope
1. The official `scripts/package_windows.py` requests nested `PYZ` table capture for **both** `Autosport.exe` and `Autosport-Data.exe`.
2. The existing shared `release_sbom.py` release-inventory validator now supports strictly differentiated outer-only schema v1 and outer-plus-PYZ schema v2. It checks exact executable/source identities, nested module type, bounded offsets, sorted names and Windows-case aliases; it does not execute embedded Python modules.
3. Existing canonical Windows package and Data-tool packagers embed and revalidate inventories as part of sealed package manifests, not as unattached reports.
4. Focused negative/positive tests are in `tests/test_plan6_5b_nested_pyz_inventory.py`; earlier aggregate-budget regression is preserved in `tests/test_plan6_5b_embedded_aggregate_budget.py`.

## Qualification honesty
At merge, exact-head PR CI [39692005264](https://github.com/Oleksii-debug/Autosport/actions/runs/39692005264) and Windows candidate [39692005468](https://github.com/Oleksii-debug/Autosport/actions/runs/39692005468) had **SKIPPED** outcome. Other head and merged-main workflows were queued. **No green native Windows or full-repository pytest PASS is claimed** by this receipt. Source-level integration and GitHub post-merge readback are established.

## Terminal state
**Plan 6 §5Б = IN_PROGRESS_NOT_DONE, NOT CLOSED_FOR_DEVELOPMENT.** This completed slice does not establish full third-party distribution-to-executable attribution, a transitive bundled-program SBOM, license/notice clearance, or exact Windows package qualification. Continue only those approved remaining requirements; do not redevelop the now integrated PYZ census or aggregate CArchive guard without a reproduced defect. Production signature, physical owner/NVDA acceptance and real bookmaker execution are separate final stages.

`REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `WHOLE_PRODUCT_COMPLETE=false`.
