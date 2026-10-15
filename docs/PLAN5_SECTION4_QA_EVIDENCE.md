# Plan 5 / Section 4 — cross-domain QA qualification

Evidence contract: `PLAN5_SECTION4_QA_V1`
Evidence class: REPOSITORY_SOURCE_AND_HOSTED_FIXTURE_TESTS_ONLY
Plan authority: `PROJECT_PLAN_INDEX.md`, `MULTI_PLAN_CLOSURE_STATE.md`, canonical Drive `5. П’ятий план`.
No external bookmaker credentials, financial movements, website login, or physical NVDA execution.

## Reuse / qualification decision

The required QA implementation already exists in the integrated Python test suite and CI workflows.
No duplicate QA framework, financial authority, provider router, model/Risk authority or state engine
is introduced merely to close this section. Existing tests are the implementation;
the ledger below is their cross-domain, source-bound qualification evidence.

- Frozen all-suite source identity: `eeee53bcf34fc001e740d579d6b6df7f9760f730`
  (canonical Plan-5 Section-3 PR #1198 head).
- Integrated source commit: `920721843aee70d082d14027596af010a675d048`
  (merge of PR #1198 into then-current main).
- CI: https://github.com/Oleksii-debug/Autosport/actions/runs/37778840621,
  exact frozen head, **COMPLETED SUCCESS** in four matrix jobs.
- Logs prove **7894 passed / 22 skipped** on Ubuntu 3.11 and Ubuntu 3.12,
  **7899 passed / 17 skipped** on Windows 3.11 and Windows 3.12.
  All four runner jobs also completed the `python -m autosport demo` step.
- `.github/workflows/ci.yml` verifies `AUTOSPORT_SOURCE_SHA` at checkout through
  `scripts/verify_source_checkout.py --source-sha` and runs `python -m pytest -v tests`.
- Post-merge main readback found the *same blob SHA* as the tested head for every
  QA representative below. These are representative evidence roots, not a claim
  that every main dependency at merge time equals the tested predecessor.
- Exact post-merge main CI #37798155872 and Windows candidate #37798155881
  were still QUEUED / NOT PASS on evidence collection; no assertion about
  their eventual conclusion is made. The previously queued PR Windows candidate
  #37778840644 was likewise NOT PASS. A subsequently proven failure requires
  a concrete Section reopen and focused repair.

## Cross-domain deterministic / negative / state-machine evidence

| Domain and contract | Integrated test module | Git blob SHA (main and tested head) | Falsifier / invariant |
| --- | --- | --- | --- |
| Decimal financial properties | `tests/test_calculation_properties.py` | `f7166f12f2c42d8e827f7bb84bb9912fc0008c6e` | Hypothesis 64-example properties, money conservation, deterministic process Decimal context, payout identity |
| Causal replay / freshness | `tests/test_replay_timestamp_order.py` | `b729c2aa8e2e30d1c3937a50a88374a494de7d4a` | no-future observation/ingest, late/stale sequence suppression, duplicate/conflicting sequence, UTC offset correctness |
| Provider UNKNOWN / idempotency | `tests/test_bookmaker_receipt_reconciliation.py` | `40e9adaf50bb49a6cf3181cf6e54033f23b329f1` | exact receipt replay, conflicting receipt identity rejection, partial outcome and UNKNOWN cannot manufacture external receipt |
| Crash/partial writes/restart | `tests/test_recovery_reconciliation.py` | `752bc205019171c624f8d9f7f588170c397a75f3` | late crash, second crash, pre-manifest crash, durable history, corrupt registry, tampered paper state, active-writer race |
| Agent model/effect authority | `tests/test_agent_loop.py` | `fba978267a379f91bc14706e299c00b5ff981777` | restart no duplicate action, UNKNOWN until reconciled, no future authority, forged observation/action/memory rejected |
| Campaign financial authority | `tests/test_campaign_economic_authority_adversarial.py` | `ab72dc5a38fac9f37062d2ec4812cf13d565e69b` | caller-mutated PnL/evaluation membership rejected before persistence |
| Exact source provenance | `tests/test_historical_outcome_source_identity.py` | `361c6ff41611cb4eb56d003fdb5dab1d084a6be4` | forged/relabelled source, duplicate JSON keys and nonfinite numbers fail closed |
| PAPER no-future truth | `tests/test_paper_execution_future_quote_causality.py` | `d04d35f863eeeff764a364b082d57a95a9514453` | future quotes and causally invalid availability are rejected without reclassifying unrelated errors |
| Forensic audit boundaries | `tests/test_forensic_session_journal.py` | `5e3b69a15001dbf51bb7bb83d23a3277927f2878` | tamper, truncation, 64 MiB bound, lock racing, uncertain fsync/write, secret redaction, unclean restart |

## Interpretation, recovery and follow-up

- All fixtures remain fixture/source evidence; no PAPER campaign, real bookmaker
  execution, wallet/settlement truth, manual NVDA or whole-product acceptance is inferred.
- Provider ACK is not a fill; UNKNOWN can never trigger a blind write retry.
- Existing test implementations are reused, not reimplemented; fail-closed QA checks
  are therefore machine-executable, not narrative-only.
- Exact CI is SUCCESS at the named frozen source. Queued post-merge CI is not
  misreported as PASS and is an explicit follow-up qualification signal.
- Under AGENTS.md Simplified Closure Protocol v3, a section whose complete
  repository-controlled implementation exists and whose *available* machine tests
  passed can be closed without requiring unavailable external runner evidence.
  A real newly discovered failure invalidates the affected part of this closure.

Source readback and test-list inventory performed 2026-10-08.
