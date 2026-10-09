# Autosport — Plan 2 Section 7: financial reconciliation and correction contract

**Decision:** terminal repository-controllable Section 7 by REUSE → QUALIFY → INTEGRATE → READBACK; existing financial/PAPER and provider/real *read-model* code is retained without introducing a second ledger or permission authority.  
**Conflict key:** `financial-core / paperbook / portfolio-risk`.  
**Predecessor:** Plan 2 Section 6 is terminal DONE in `MULTI_PLAN_CLOSURE_STATE.md` and the canonical Drive plan.  
**Sources of truth:** the authoritative PaperBook/PAPER execution and settlement journals remain separate from RealExecutionLedger and provider-account observations. PAPER observations are NEVER real effects, and a provider ACK is NEVER proof of a fill.

## Section 7.1 — common typed reconciliation seam; partial legs and late corrections

- Shared typed `ExecutionPlan` / `ExecutionAction` / stable action and attempt identities originate in `real_execution_ledger.py` and are explicitly imported by `paper_execution_reality.py`. These are common *input contracts*, not shared writable cash authority. `PaperExecutionLedger` keeps synthetic/observed PAPER attempts, while `RealExecutionLedger.verified_execution_view(plan_id)` returns a typed read-only `VerifiedExecutionPlanView` bound to one immutable ledger byte snapshot (SHA-256 and event count). The latter cannot mint retry or receipt authority; a later effect must be rechecked by its canonical writer.
- `paper_execution_reality._derive_run_economics` and `execute_paper_plan` separately account for each accepted/PARTIAL child, use only its accepted execution stake, expose UNKNOWN as worst-case requested stake, preserve remaining unexecuted legs and stop without blind continuation. `paper_execution_adoption` materializes only confirmed PAPER ACCEPTED/PARTIAL attempts into `PaperBook`; it reconciles expected pre/post state with durable attempt markers to reject duplicate materialization.
- `bookmaker_receipt_reconciliation.py` requires canonical child/parent/venue/account/quote and unique external receipt identity for ACCEPTED, detects conflicting replay, and handles partial refusal/rerouting as *non-money-moving* proposals. `bookmaker_account_reconciliation.py` produces typed OPEN / SETTLED / UNKNOWN account observations and an `UnexplainedBalanceDelta`, explicitly **not** an inferred settlement, fill, or cross-ledger bankroll balance.
- `continuous_session.py` binds detached versioned settlement outcome payloads before PAPER effects, rejects same evidence ID with different outcome payload, and treats a distinct correction evidence ID as new causal evidence. `PaperBook` validates settlement lifecycle, payout and bankroll; `SettlementEngine` publishes atomic deterministic batch effects. A late correction never silently rewrites an already-settled ticket or grants automatic real-money cash/risk capacity.
- Common reconciliation rule: expose each domain's authoritative identity, causal observation, unresolved obligation and evidence grade; no cross-domain sum of PAPER and REAL balances, no exchange-rate guesses, no automatic acknowledgement-to-fill upgrade. The real provider settlement/receipt connection is Plan 4/8 scope.

## Section 7.2 — crash/restart, no duplicate stake/settlement, obligations

- `PaperBook.save/load` checks original accepted stake/open liability, stable ticket/lifecycle identity, conservation and exact Decimal balance, with atomic replacement and fail-closed invalid snapshots. `ContinuousSessionCoordinator._settle` locks the workspace and idempotently consumes unique evidence IDs; a correction/restart with conflicting reused ID fails before balance mutation.
- `PaperExecutionLedger` and `paper_execution_adoption` have exact attempt-marker replay/restart and must not materialize a second ticket for an already applied attempt. The durable recovery and session tests exercise late crash and repeated restart without replaying economic effects.
- `RealExecutionLedger` serializes writer effects, validates exact receipts/unknown causality, retains unresolved attempts as UNKNOWN until new positive provider evidence, never retries on ambiguous/not-found only. Its verified read view remains point-in-time; external ACK and PARTIAL acknowledgement are not fill proof.
- The account snapshot reconciler uses strictly chronological, identity-bound observations; disappearance of an open position becomes UNKNOWN rather than SETTLED. Old-file rollback, stale/mixed accounts, high precision Decimal deltas, crash around prepare/publish, and reused conflicting observation IDs fail closed.

## Section 7.3 — fixtures now, real receipts in Plans 4/8

- Existing deterministic receipt routing fixtures exercise multi-child acceptance, partial and refusal residual routing, exact replay idempotence, conflicting identity, absent receipt and UNKNOWN blocking. No bookmaker credentials or real provider account is needed for repository-controlled financial semantics; no real fill/account cash has been attested.
- Models/learning receive no power to expand hard owner Risk limits; PAPER/real receipts and balances are not interchangeable.

## Frozen exact-SHA qualification and post-merge identity

| Authority | Qualified exact SHA | Executed gates |
| --- | --- | --- |
| Financial/PAPER and account/receipt source/tests | `1f81b7daedb180df4bab22e5877ccea998187f3d` (merged PR #2270) | CI [37849986725](https://github.com/Oleksii-debug/Autosport/actions/runs/37849986725): **SUCCESS**, full pytest and demo Ubuntu/Windows Python 3.11/3.12; Windows Candidate [37849986798](https://github.com/Oleksii-debug/Autosport/actions/runs/37849986798): **SUCCESS**. |
| Current real-execution ledger and its main test battery | `4ea79138873b37f016162b69cde24196e5258c80` | CI [37904783985](https://github.com/Oleksii-debug/Autosport/actions/runs/37904783985): **SUCCESS**; Windows Candidate [37904784003](https://github.com/Oleksii-debug/Autosport/actions/runs/37904784003): **SUCCESS**; Endurance [37904784053](https://github.com/Oleksii-debug/Autosport/actions/runs/37904784053): **SUCCESS**. |

Direct GitHub per-ref `fetch_file` readback confirmed identical blobs on their respective qualified heads and current integration main (`e9b49da08efdc6d76ce606bc8ca7ffe9c1e95e45` at selection):

| File | Integrated blob SHA |
| --- | --- |
| `src/autosport/paper.py` | `2e1155734d1bd3c8e4c5e9d93451a79a7b9da064` |
| `src/autosport/paper_execution_reality.py` | `9fee46085310f414477dd7837cf646a96fdda7e9` |
| `src/autosport/paper_execution_adoption.py` | `ca617ce0f384f8d7b7e13d8f2fe8e8dbbc1faebe` |
| `src/autosport/continuous_session.py` | `367b7c528ced00163fe684e95f9389e735cce8ca` |
| `src/autosport/settlement.py` | `24a8f799e82c62a3fbbcf7959af23c6b8281a91e` |
| `src/autosport/bookmaker_account_reconciliation.py` | `9d0b9579ec35bfc1b89d80702005d008e3e6cd05` |
| `src/autosport/bookmaker_receipt_reconciliation.py` | `572f5941cb1951869e231582a2cc2ab85395ab0d` |
| `src/autosport/real_execution_ledger.py` | `afd8c0047c37dcaa906741993fb8541c74c77a20` |
| `tests/test_bookmaker_account_reconciliation.py` | `ff814a34e074318202a02a10f80be86a1814f311` |
| `tests/test_bookmaker_receipt_reconciliation.py` | `40e9adaf50bb49a6cf3181cf6e54033f23b329f1` |
| `tests/test_continuous_session.py` | `731e55bd4ae78e481147832391386fbb31cf0f58` |
| `tests/test_paper_execution_adoption.py` | `929add21cefc648e873d90164aacc14dfed78311` |
| `tests/test_paper_execution_reality.py` | `718cbf83f885a2c4635e08681b4afc62b0cacbcd` |
| `tests/test_real_execution_ledger.py` | `1934c59b41b25b1f1c703c7c03f80e6240518815` |
| `tests/test_real_execution_ledger_read_view.py` | `c3d4ab825d669c4fec62fe828434fd3b707bedc0` |
| `tests/test_real_execution_reconciliation_identity_replay.py` | `a75875e2c8808360e5b85f9bac42cd9154bcea09` |
| `tests/test_recovery_reconciliation.py` | `752bc205019171c624f8d9f7f588170c397a75f3` |
| `tests/test_settlement_batch_atomicity.py` | `6c78c69fc06d6ceab4785d773c0c8100ffb605db` |

Focused exercised cases include duplicate/conflicting receipts and payout evidence, monetary conservation, cross-currency balance rejection, missing/partial legs, accepted-vs-observed price distinction, UNKNOWN blocking, stale account observation, rollback, monotonic publish/restart, correction, no double-count and scenario/Risk fail-closed. The successfully executed full CI also covers the already-closed portfolio/scenario suite. No new full CI on registry-only documentation commits is claimed; local pytest is not claimed because this isolated runtime cannot resolve github.com.

**Terminal acceptance:** all repository-controllable Section 7 responsibilities are already implemented in the canonical source and positively qualified by the exact-source gates above; no new ledger, financial permission or source rewrite is justified. Mark `DONE` in the live multi-plan registry and canonical Drive plan after committing this evidence, then select Section 8. `PAPER_ONLY=true`; `ACK_IS_NOT_FILL=true`; `REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.
