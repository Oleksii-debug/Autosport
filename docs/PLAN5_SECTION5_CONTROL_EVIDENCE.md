# Plan 5 / Section 5 — continuous qualification and repair control-plane evidence

Evidence contract: PLAN5_SECTION5_CONTROL_V1
Evidence class: REPOSITORY_SOURCE_AND_HOSTED_TEST_ONLY
Canonical plan: Drive `5. П’ятий план`.
Baseline main before closure: `a8f9d485ce19ea711ee5a12539f7540bccc0533a`.
No provider account, credentials, financial actions, owner PC access, or physical NVDA acceptance.

## Implementation reused (not duplicated)

1. `.github/workflows/ci.yml`, `.github/workflows/windows-build.yml`, and `.github/workflows/endurance.yml` admit only qualifying PR head revisions before expensive jobs, then checkout the exact source SHA and run `scripts/verify_source_checkout.py --source-sha` before testing/building. Main pushes and explicit dispatches retain their separate identities. Runner OS/matrix and GitHub run/job IDs are owned by Actions, not model content.
2. `scripts/cancel_superseded_pr_workflow_runs.py`, `scripts/cancel_superseded_pr_workflow_runs_scoped.py`, and the trusted default-branch `.github/workflows/pr-qualification-supersession.yml` re-read current PR/run identities before cancelling obsolete work. The source workflow ID and repository are bound; a stale run cannot promote or replace the current head. CI and PR authority is still external to test data and worker narration.
3. The Windows candidate workflow independently re-extracts the package, compares `BUILD_INFO.source_sha` to the workflow source SHA and compares package/EXE digests. Evidence export and UIA smoke artifacts are uploaded separately; `HUMAN_TESTED=false` and `NVDA_VERIFIED=false` stay explicit.
4. When a gate fails, GitHub run/job status and uploaded diagnostics identify the failing exact SHA; repair is a normal source commit on the same canonical lineage, followed by a new exact-head qualification. No test output can self-promote a failed, skipped, cancelled, queued, or superseded run to PASS. This is the defect->repair->retest control mechanism; it does not authorize automatic merging or financial effects.
5. Controllers have repository-scoped least privilege: the test workflows have read-only contents/PR permissions, while only the trusted default-branch supersession workflow has `actions: write` to cancel stale jobs. No personal file scanning or bookmaker secrets are required.

## Positive machine qualification and exact readback

- Frozen all-suite source: `eeee53bcf34fc001e740d579d6b6df7f9760f730`.
- Executed GitHub CI run: https://github.com/Oleksii-debug/Autosport/actions/runs/37778840621 — completed SUCCESS with four real Ubuntu/Windows Python 3.11/3.12 test jobs, not admission-only success. Existing Section-4 ledger records 7894 passed/22 skipped on Ubuntu and 7899 passed/17 skipped on Windows, per Python version.
- The following blobs matched **exactly** in that tested source and in closure-baseline main:

| Source / falsifier | Git blob SHA |
|---|---|
| `scripts/cancel_superseded_pr_workflow_runs.py` | `fda104fe649f89b19536dc55622b27d012b6a884` |
| `scripts/cancel_superseded_pr_workflow_runs_scoped.py` | `9b822120f511506f1bbc8878e5653428bf0dfca1` |
| `scripts/verify_source_checkout.py` | `f9b08a2c376c74305db641836cd254a95e0a2857` |
| `.github/workflows/ci.yml` | `66db14e711809cefbe304d2891bd857b97cb098e` |
| `.github/workflows/windows-build.yml` | `0ce9d92dbbdc2d883c2e1561f726339ccf36928d` |
| `.github/workflows/endurance.yml` | `a8169d39b7ec456b748f0baea729848b3d7620ed` |
| `.github/workflows/pr-qualification-supersession.yml` | `10f0a0f03caedd71c444b582f0b9549af9becb8b` |
| `tests/test_pr_qualification_supersession_controller.py` | `bf2d3f636eaf5c59e916acf080ee26221d8a5e61` |
| `tests/test_ci_admission_authority.py` | `482c8e10b228d79c46622ad334a955db2f01e6ab` |

## Negative/failure/recovery and security evidence

Existing executed tests cover stale-head and rerun races, branch/repository/workflow rebind, foreign-workflow cancellation, changed association, ambiguity, fake admission state, and partial/failed workflow handling. GitHub Actions controller is run-to-run recoverable: a later trusted trigger re-enumerates actual live runs rather than treating its previous in-memory state as authority. No second runtime/coordinator, financial capability, credential store, or model-owned promotion authority is introduced by this section.

Caution: some newer GitHub Actions were QUEUED/NOT PASS when this audit ran; a workflow summary SUCCESS with only admission and SKIPPED test jobs is **not** machine qualification. A later demonstrated regression requires a narrow, evidenced reopen. Actual owner-PC interactive qualification is outside this repository-controlled section (Plan 5 Section 8 and final acceptance).

## Terminal source decision

All available repository-controllable Section-5 source/test/control requirements exist in the integrated source; no missing product mutation was identified. Under `AGENTS.md` Simplified Section Closure Protocol v3, reuse + successfully executed hosted tests + same-blob main readback constitutes terminal Section 5 engineering closure. This is not evidence of a real bookmaker, a physical owner's PC, manual NVDA use, or whole-product completion.
