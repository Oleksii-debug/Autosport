# Autosport — Plan 5 Section 9: terminal runtime/security/reliability qualification

Contract: `PLAN5_SECTION9_TERMINAL_V1`
Evidence class: `REPOSITORY_SOURCE_AND_HOSTED_FIXTURE_TESTS_ONLY`
Canonical authority: Drive `5. П’ятий план`, `PROJECT_PLAN_INDEX.md`, `MULTI_PLAN_CLOSURE_STATE.md`, `MULTI_PLAN_PARALLELISM_CONTRACT.md` and AGENTS.md v4/v3.
Scope: repository-controlled engineering across Plan 5 Sections 1–8. No extra execution or financial authority is created by this record.

## Frozen cross-plan candidate and executed gates

- Exact complete-suite candidate: PR #2275 head `aed1d119f6022395a061e0675418f845cd603350`.
- Full GitHub CI: https://github.com/Oleksii-debug/Autosport/actions/runs/37885872822 — **completed/success**; admission and all four actual Ubuntu/Windows Python 3.11/3.12 jobs SUCCESS, not merely queued/skipped. The Ubuntu 3.12 log reports 8,556 passed / 23 skipped; Windows 3.12 reports 8,561 passed / 18 skipped. Both completed `python -m autosport demo` with `paper_only=true` and `real_money_execution=false`.
- Windows Candidate: https://github.com/Oleksii-debug/Autosport/actions/runs/37885872826 — **completed/success**, actual build job SUCCESS.
- Accepted merge: https://github.com/Oleksii-debug/Autosport/commit/f8640f080435f28aa6f1e94abdd793f16ac60950. This merged exactly the frozen tested #2275 head. Postmerge readback showed **all six changed** source/workflow/CLI/test/evidence Git blobs byte-identical to that candidate.
- Plan 5 Section 8 registry closure: `6744abb6710076ca737055a07af09c7a384912ab`.
- Main additionally has independent Plan-3 calibration and documentary changes relative to the #2275 frozen candidate. Those are not silently called identical to the candidate. A comparison from that candidate to postclosure main showed *no modification to pre-existing Plan-5 runtime/security/endurance/performance source/tests*, just those independent files and the Plan-5 status registry. Section-8 six-blob equality is independently confirmed.

## Required scope — implementation and scientific/negative evidence

| Section | Reused integrated implementation and executed qualification | Falsifier / failure mode |
| --- | --- | --- |
| 1 ProductRuntime | Terminal PR #2263, full CI #37726753316 and Windows #37726753340 SUCCESS; one active workspace/runtime authority | concurrent START/STOP, crash/restart, uncertain STOP, secondary exception detail redaction |
| 2 Security | Terminal PR #1694, CI #37777291415 and Windows #37777291553 SUCCESS; exact source-bound secret scanner | encoding/canary leak, symlink/identity races, session/authority substitution, hostile serialization; 64 KiB bounded scanner |
| 3 Forensic evidence | Terminal PR #1198, cross-platform CI #37778840621 SUCCESS; append-only forensic journal | truncation/tamper, 64 MiB bound, lock races, uncertain append/fsync, secret redaction and restart chronology |
| 4 QA | `docs/PLAN5_SECTION4_QA_EVIDENCE.md`; the same complete suite and source-bound property/state-machine regressions | future leakage, Decimal conservation, fake provider ACK, UNKNOWN/no blind retry, forged agent/model/PAPER evidence |
| 5 Verification/control | `docs/PLAN5_SECTION5_CONTROL_EVIDENCE.md`; existing exact-SHA/runner and supersession controllers | stale PR/head, malicious reassociation, runner identity, fake PASS or premature approval; no self-promotion |
| 6 Reliability | `docs/PLAN5_SECTION6_RELIABILITY_EVIDENCE.md`; Endurance #37738857644 Ubuntu+Windows real executed SUCCESS: 20,000 synthetic events, three restarts, 50 fixture PAPER tickets; complete CI #37778840621 SUCCESS | transaction torn-write, process kill, corrupt registry, writer-lock conflict, deterministic rehydrate and idempotent settlement |
| 7 Performance | PR #2273 frozen head `1ed7647b0fd87044aa489dfe1ab1b4e58ecb7876`, CI #37876567004 Ubuntu+Windows 3.11/3.12 SUCCESS and Windows #37876567038 SUCCESS | five-stage ingest→mirror→opportunity→portfolio→decision instrumentation, 2,000 bounded windows, 12 durable fixture cycles, stale/backpressure/budget WAIT and restart NO_CHANGE |
| 8 Windows lab controller | PR #2275, exact CI #37885872822 + Windows #37885872826 SUCCESS; six source/test blobs matched in main | owner-only original dispatch/rerun, fixed scenarios, hostile/fork JSON, cross-ticket forgery, bounded 16 KiB/5-receipt evidence, concurrency, idempotent restart, false PASS/NVDA privilege |

## Main integration readback of durability and exact code identity

Post-Section-8 merge, these current-main blobs exactly matched the independently recorded Plan-5 Section-6 tested/reused blob inventory:

- `src/autosport/endurance.py`: `56538df8ba4abf4a1f27b12f5b9b061be3b900d5`
- `src/autosport/recovery.py`: `f2aca557c60aa8dbbb1f0d184fbc6bfea0c30496`
- `src/autosport/run_transaction.py`: `94af71706ce176ef1c4cb960cc8fce2555497dcf`
- `src/autosport/workspace_lock.py`: `ea87fd97d3314eda24464693125d8622653587a8`
- `tests/test_endurance.py`: `1f47c569c731df78f698881cc0b092a434d98758`
- `tests/test_recovery_reconciliation.py`: `752bc205019171c624f8d9f7f588170c397a75f3`
- Section 8 `tests/test_windows_lab_contract.py`: `cd044c7726fa1b7dea604211af5e58bc1b065cc5`

The cross-platform #2275 candidate ran the entire `tests/` suite; Section 6 independent endurance and Section 7 independent performance campaigns provide stronger scenario-specific evidence without recreating controllers. Exact PR SHA, SHA-bound source checkout and postmerge Git blob comparison are the source-of-truth identifiers; no chat claim upgrades a queued, skipped or superseded workflow.

## Authority, privacy, recovery and degradation constraints

1. ProductRuntime maintains one active authority. Lab controller is explicitly nonexecuting; research/model/test/agent output cannot issue financial operations or approve a release. No second settlement or trading runtime was introduced.
2. Provider ACK is not a fill, an UNKNOWN external effect is not blindly replayed, and model/research has no Risk/owner authority.
3. Transaction, journal, workspace lock and ticket observation identities are durable and fail closed on broken provenance, malformed files, replay/conflict, symlink/identity changes or partial publication; tests exercise negative paths and recoverable restarts.
4. Secrets are not projected into operator logs, evidence, JSON receipt errors or automated-model results. Scanner, journal, latency and lab observation records enforce distinct bounded evidence budgets.
5. Timing/freshness/backpressure faults are a deterministic WAIT/STOP, not an invisible stale decision or assumed success. Measurements are on fixtures/CI hosts, not an owner's exact PC throughput benchmark.
6. No source-level acceptance has been transmuted into real bookmaker access, money movement, remote private backups, a live privileged Windows VM or physical human/NVDA test. `REAL_MONEY_EXECUTION=false`, `HUMAN_TESTED=false`, `NVDA_VERIFIED=false`, `WHOLE_PRODUCT_COMPLETE=false`.

## Terminal decision and reopen threshold

The Plan-5-specific executable components, scientific/negative/failure/restart tests, cross-platform qualification and source readbacks are already integrated in the canonical repository. No new runtime, financial, security or test framework is required merely for Section 9. Under AGENTS.md Simplified Section Closure Protocol v3, existing implementation + executed available machine gates + exact source readback justify terminal **Plan 5 Section 9 DONE** for repository-controlled engineering. A demonstrable failed downstream main gate, documented regression, source-identity break or materially changed acceptance contract requires explicit scoped `REOPENED` with evidence; absent that, ordinary workers must skip Sections 1–9.

This is Plan 5 closure only; it does not assert whole Autosport product release readiness or satisfy Plan 7/8 external/physical acceptance.
