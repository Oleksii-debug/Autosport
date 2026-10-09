# Plan 2 — Section 4 — PaperBook / Virtual Bank / deterministic PAPER execution

Status: terminal repository-controllable qualification by REUSE → QUALIFY → INTEGRATE → READBACK. No new paper execution authority created.

## Acceptance and scope

The canonical [Plan 2](https://docs.google.com/document/d/1XQmujyDGfHXe8_ohumnoivS6bSAMNdTKOUOu9wYjKG8/edit) Section 4 requires (4.1) reused PaperBook, paper execution, paper campaign, anti-rollback and risk-generation paths; (4.2) accepted PAPER odds/stakes, partial/void/reject analogues and exact cash/open exposure; (4.3) strict observed-decision quote versus execution/accepted PAPER analogue separation. No real bookmaker credentials, receipt, fill, account cash or physical NVDA evidence is claimed. This does not close downstream Plan 2 Sections 5–8.

## Source and truth contract

- `src/autosport/paper.py`, Git blob `2e1155734d1bd3c8e4c5e9d93451a79a7b9da064`: `PaperBook` stores exact `Decimal` initial virtual bankroll and balance, opens an auditable ticket with provenance/currency and locked legs, refuses insufficient balance/duplicate quote keys, derives committed OPEN stakes, and settles WON/LOST/VOID with a deterministic lifecycle, exact payout, and validated snapshot/save/reload. No provider write interface.
- `src/autosport/paper_execution_reality.py`, blob `9fee46085310f414477dd7837cf646a96fdda7e9`: durable `PaperExecutionLedger`, synthetic and recorded PAPER attempts, explicit accepted execution observations, separate decision quote/accepted odds and stake, atomic completion/reload.
- `src/autosport/paper_execution_adoption.py`, blob `ca617ce0f384f8d7b7e13d8f2fe8e8dbbc1faebe`: identity-bound preparation/adoption and current PaperBook revalidation before materialization, not a real execution authorization.
- `src/autosport/paper_campaign_runtime.py`, blob `e932b4a98b8d486c6ae324c338a2509b39db28fa`: persistent campaign continuation and PAPER ticket binding.
- `src/autosport/_paper_execution_anti_rollback.py`, blob `3eb5af300ce342cf6a716765db549e65abc5523e`; `src/autosport/_paperbook_preload_generation_cas_guard.py`, blob `d8e875e32f798a537b2539609fa8b47f41779e18`: reject rollback and stale writer/generation authority. All six source SHA values match byte-for-byte between candidate and integrated main.

## Test matrix (all referenced file blobs identical at tested head and main)

- `tests/test_paper_execution_reality.py` `718cbf83f885a2c4635e08681b4afc62b0cacbcd`: distinct decision vs execution odds, idempotent trigger; accepted, rejected, partial, UNKNOWN; stale quotes; no blind follow-on; multi-leg recovery; restart; ledger tamper/chain/lock.
- `tests/test_paperbook_snapshot_generation_cas.py` `5b760060000e1b31abef866a3a1e7ee3844c23a0`: competing writers, stale book, CAS, interrupted publication, alias/retarget.
- `tests/test_paperbook_decimal_chronology.py` `31427922e154135dec29b049b2194859b411ee34`: caller Decimal context isolation, nonexact debit rejection, settlement ordering/reversibility and atomic failure.
- `tests/test_paper_book_snapshot_integrity.py` `23cbfa698244c42508d5a2971b716d264f30c5e0`: open/settled round-trip, conservation, duplicate ticket/quote, impossible status/payout, non-finite rejection.
- `tests/test_paperbook_atomic_publication.py` `5a9d601c8b87a8b3e6b8a13a85de1f055ae96f3b`: adversarial temp hardlink/replace, torn-state failure preservation.
- `tests/test_paper_campaign_process_kill_recovery.py` `23ded18039738bbb2f6cf892a758da57f8e6c43b`: kill/restart after settlement/learning/postmortem, exactly-once continuation.
- `tests/test_paper_execution_reality_regressions.py` `5649b325505f292f60b1c6c6e030d6e1d393e122`; `tests/test_paper_execution_append_recovery.py` `c4b839158284259cc2979d4aad2ad592f96c7858`: hostile retry, current/old book identity and append recovery.
- `tests/test_paper_value_lay_economics.py` `ec63493143b303d3bdd07e9d99208ba75ebe67b6`, `tests/test_paper_book_json_integrity.py` `db19bb6a65734a162f114425a9fabb4dabc1c5c1`: LAY commitment and strict malformed serialized input.
- `tests/test_scenario_search.py` `52256bd95417299aeb8030476b066b7d30b5dc4a`, `tests/test_portfolio_scenario_snapshot_integrity.py` `35a878c9a75391b19f12a65ff21c71170d1de8bb`, `tests/test_paper_risk_finite_integrity.py` `7284106961e33167425d64ba4db809426a437c8e`, `tests/test_settlement_batch_atomicity.py` `6c78c69fc06d6ceab4785d773c0c8100ffb605db`: scenario/risk/conservation/settlement integration.

## Executed qualification and integration proof

Candidate `1f81b7daedb180df4bab22e5877ccea998187f3d` on canonical Plan-2 PR [#2270](https://github.com/Oleksii-debug/Autosport/pull/2270) ran [CI 37849986725](https://github.com/Oleksii-debug/Autosport/actions/runs/37849986725) terminal SUCCESS: `python -m pytest -v tests` and demo on Ubuntu and Windows, Python 3.11 and 3.12; four full-test jobs executed (not skipped). [Windows Candidate 37849986798](https://github.com/Oleksii-debug/Autosport/actions/runs/37849986798) terminal SUCCESS: package build, workspace recovery, walk-forward evaluation, research, export/verify and external UIA gate. Exact tested PR head merged with expected-head guard to `main` in `2c7d233a80257e7c2f23714f55057c8948866d1d`. Source and fourteen focused test blobs listed here were independently re-fetched at the tested head and integrated main and found identical (all 20 compared paths MATCH). Section 3 registry closure followed at `ba883f24a4a8a6521549c27dbe7ab9a3a1d25add` without changing the tested financial/PAPER source/test files.

Qualification inference is limited to these byte-identical relevant paths plus the executed full candidate tests: this is NOT a claim that a new `main` full-suite run was executed after registry commits, nor that manual Windows NVDA testing occurred.

## Decision

All repository-controllable Section-4 obligations are satisfied by preserved existing canonical implementation, executed cross-platform tests and verified integrated identical bytes. `PAPER_ONLY=true`; `REAL_MONEY_EXECUTION=false`; `ACK_IS_NOT_FILL=true`; `OWNER_RISK_NON_EXPANSION=true`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`. Section 4 is terminal DONE under Simplified Section Closure Protocol v3, absent demonstrated regression. Section 5 remains next ACTIONABLE.
