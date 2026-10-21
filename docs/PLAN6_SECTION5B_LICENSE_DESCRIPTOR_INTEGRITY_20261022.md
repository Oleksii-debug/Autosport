# Plan 6 §5Б — LICENSE/NOTICE source-file identity repair (scoped, nonterminal)

Date: 2026-10-22. Status: **SCOPED REPAIR MERGED; SECTION 5Б IN_PROGRESS_NOT_DONE**.

## Demonstrated gap

The build-host LICENSE/NOTICE reader in `capture_dependency_license_evidence` previously verified `Path.lstat()` and then reopened `Path.read_bytes()`. A path replacement after the metadata probe could bind different file bytes to the original distribution metadata while reporting a sealed hash of the substituted contents. The package must not confer stronger trust to such evidence.

## Integrated repair

- [PR #2465](https://github.com/Oleksii-debug/Autosport/pull/2465) merged into exact `main@901a23140b0d3508d76f93a10708d0b6f3855dd8`; candidate `895484802365c23b595eeb732c95e958a5b51e54`, merged with expected head.
- Canonical production source `src/autosport/release_sbom.py` Git blob `45d117abc6ff40cf6001c172eb31818e5ac2bb07`.
- Regression source `tests/test_plan6_5b_dependency_license_evidence.py` Git blob `d8bdfe11691f730dbf2096f8f33b10915d4eb53f`.
- Independent GitHub readback verified **both blobs byte-identical** between candidate and merged main.
- The source opens a bounded regular-file descriptor, enforces unchanged device/inode/size/mtime before and after read, rejects symlink swaps via `O_NOFOLLOW` when supported, and reads at most 262145 bytes (maximum plus sentinel). Failure closes the descriptor and fails the package build.
- Two repository regressions added: substitute a different inode after initial `lstat`, and refuse oversized source before reading it.

## Evidence classes

**Verified:** GitHub merge/readback, exact file inventory, isolated source-aligned helper scenarios (ordinary regular source accepted; direct symlink, oversized source and substituted inode refused).

**NOT VERIFIED / NOT EXECUTED:** complete repository pytest, GitHub exact-head CI result while runner queued, real Windows candidate build/package/install, actual embedded binary-to-distribution attribution, third-party legal clearance, physical NVDA, real-money execution. The newly authored regression tests have **not** been represented as actual checkout pytest PASS.

## Remaining approved scope and routing

Plan 6 §5Б still requires actual bundled-executable/transitive distribution SBOM attribution plus license review and reproducible exact Windows artifact qualification. Existing direct/build-host provenance and NOTICE capture are reused, not reimplemented. These obligations are not a reason to reopen completed 5А/5В or Plans 1–5. §6А (lifecycle), §7А (exact Windows gates), §7В (plan-level closure), Plan 7 PAPER/SHADOW convergence and Plan 8 final external acceptance remain independent, unfinished gates.

**Do not set SECTION 5Б DONE, Plan 6 DONE or WHOLE_PRODUCT_COMPLETE until their respective evidence exists.** This record is a precise scoped merge receipt, not another feature proposal.
