# Autosport — Plan 6 §5Б, binary CArchive census integration (2026-10-21)

## State / release truth

**IN_PROGRESS_NOT_DONE — not a Section terminal closure.** This is a completed and merged, acceptance-relevant implementation slice in the existing official Windows build/package path. It does not authorize labelling the full §5Б, Windows installer, Plan 6, or product DONE.

- Existing root main at integration start: `84bfc1eb64838068da9f320806a411bdc6a9aea2`.
- PR [#2419](https://github.com/Oleksii-debug/Autosport/pull/2419) merged into `main@349d612309cd2cbb357a044f942ef5e3108dc4ed` from immutable final PR head `e6ebc6850eddf8444374050b0c6f575e05810adc`.
- Post-merge GitHub readback: all **5/5 source/test blobs byte-for-byte match** final PR head:
  - `src/autosport/release_sbom.py` `0627e42a1d614694b564e80d135c95ed91897ed4`
  - `src/autosport/release_package.py` `2b4e2d931444f7060a150a6eb0b681756f939ac0`
  - `scripts/package_windows.py` `05bcf3aead0432c28272d657df14b1b5b2c23812`
  - `tests/test_plan6_5b_embedded_executable_inventory.py` `14e7a8dddad5950f1fd3a4ed9857403f75e8fe6e`
  - `tests/test_release_build_dependency_metadata.py` `6e87f26b9fbc609a8e078b3a5a6ff7b4c33b7d55`

## Implemented

Canonical `scripts/package_windows.py` now reads the actual PyInstaller CArchive table from the **already SHA-pinned, PE-validated** `Autosport.exe` through `PyInstaller.archive.readers.CArchiveReader`. It captures source commit SHA, the packaged executable SHA-256 and a bounded inventory of member names, CArchive typecodes, compressed/uncompressed sizes. It rejects malformed metadata, unsafe paths, unbounded sizes, forged source/executable identities and source swaps during capture.

Canonical `release_package.py` writes `EMBEDDED_EXECUTABLE_INVENTORY.json` into the same official ZIP covered by `PACKAGE_MANIFEST.json` and `SHA256SUMS.txt`; its verification checks the embedded inventory against the actual package executable and canonical metadata bytes. Existing source and build-host dependency inventories remain unchanged. Focused regression and negative tests have been **committed** and are wired to CI, not claimed executed.

## Remaining approved §5Б implementation / qualification

1. Reconcile nested embedded PYZ Python modules, native dependencies, distribution attribution, third-party licenses and notice obligations against the exact built EXE; existing CArchive census alone is explicitly **NOT a full transitive SBOM** and **NOT license clearance**.
2. Qualify an exact Windows package/build candidate and apply available tests. Head `e6ebc6850eddf8444374050b0c6f575e05810adc` CI [#39673187014](https://github.com/Oleksii-debug/Autosport/actions/runs/39673187014) and Windows Candidate [#39673187057](https://github.com/Oleksii-debug/Autosport/actions/runs/39673187057) were **QUEUED / NOT EXECUTED** at integration, not PASS. Check current state before claiming any qualification.
3. Section 5Б remains IN_PROGRESS_NOT_DONE. The completed binary-census slice should not be reimplemented without concrete defect. Signed production distribution, human/NVDA and real-money acceptance remain separate evidence domains.

`REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`; `WHOLE_PRODUCT_COMPLETE=false`.
