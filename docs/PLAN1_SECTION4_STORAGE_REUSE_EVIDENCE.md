# Autosport — План 1, розділ 4: terminal reuse/integration evidence

Дата: 2026-10-11. Scope: лише `1. Перший план` — durable storage, immutable append/history, current projections, checkpoints, crash-safe recovery. У цьому розділі нову authority не створювали: чинний продукційний код і тести вже інтегровані в `main`.

## Acceptance / exact artifact proof

- Plan-1 Section-3 predecessor terminal DONE: PR #2261 exact head `d93d203e867c71aad880f1b2e3d144549e166030`, merge commit `bf25e29329ae7cfbe1e705fb00073dedc414b785`, head and merge trees EXACTLY `57806e09fe21b6a3653c98a0bbc87056f77862a8`, parents accepted main `af2bf0109be4e0909239ad20d2416ac07ff907b2` and tested head. Source identity not reconstructed from a statement: checked Git trees through GitHub Git API.
- Executed exact-head CI: https://github.com/Oleksii-debug/Autosport/actions/runs/38148320018 — `success`, Ubuntu and Windows x Python 3.11/3.12, full `pytest -v tests`, demo; Windows package candidate: https://github.com/Oleksii-debug/Autosport/actions/runs/38148320047 — `success`.
- Earlier accepted unchanged storage/recovery bytes also passed https://github.com/Oleksii-debug/Autosport/actions/runs/37988899103 (four OS/Python jobs SUCCESS), https://github.com/Oleksii-debug/Autosport/actions/runs/37988899104 (Windows SUCCESS) and https://github.com/Oleksii-debug/Autosport/actions/runs/37988899097 (Ubuntu/Windows 20k-event endurance SUCCESS). These are explicitly historical exact-SHA gates, not new test executions.
- Postmerge fresh GitHub readback on `main` after Section-3 registry commit `49817d76bea20ba8b275e7da483bf98c9112fcbb` confirms **7/7 production source blobs** and **14/14 focused test blobs** exactly equal to the previously qualified candidate. No main reconciliation drift in these paths.

### Exact production Git blobs (7/7)
- `src/autosport/storage.py` — `e4c4e9c26b69de47ec9e3d3d5a96e9297ba32ff9`
- `src/autosport/dataset_snapshot_lineage.py` — `8f04db84b76e1c625bb837cb03efa67a490d20af`
- `src/autosport/run_transaction.py` — `94af71706ce176ef1c4cb960cc8fce2555497dcf`
- `src/autosport/run_registry.py` — `7fab3b1c454429d969c5322f6be2bbdcc6867db5`
- `src/autosport/collector_retention.py` — `bf3dbb97d691b395278a46af6d4db6d246938c31`
- `src/autosport/integrity.py` — `8bc32ee82db849d174f8d349a8706a80abb32e2e`
- `src/autosport/historical_snapshot.py` — `4f1c54b6bfb99298feea5fb8e04d32082b110ca1`

### Exact test Git blobs (14/14)
- `tests/test_storage_dedupe_integrity.py` — `bb367bb1cb1ffc416be3d03c9c97b0eda7be6ea9`
- `tests/test_storage_partial_history_loss.py` — `431b44cadfccbb13c1b4e6ceddeb3eb1d72dd85d`
- `tests/test_storage_extra_index_semantics.py` — `a57d01022660f6a4f63bea6ac04e992f3d25b99f`
- `tests/test_dataset_snapshot_lineage.py` — `901303345078c4ae48053ca72a787112f0b12bc0`
- `tests/test_dataset_snapshot_lineage_exact_authority.py` — `0b45df37ad80f23e00bf0b4c8262c13ead375663`
- `tests/test_run_transaction_terminal_completion.py` — `4459a62b0dfc623504a4c3693add3a46373d9e1d`
- `tests/test_run_transaction_canonical_target_indirection.py` — `8479a560e3e3604d02550cb98536bf01083fc297`
- `tests/test_recovery_terminal_history.py` — `1a8770b7206cc68afb076b32e8c33dc4dfb14a2b`
- `tests/test_run_registry_missing_history.py` — `a9bbfe1eed3a721773daf05f9e5ad4c9558f85d5`
- `tests/test_section3_migration_and_bootstrap_regressions.py` — `b2bab4f0af0ec782f6caa2997bbb919f75441c59`
- `tests/test_collector_retention_compaction.py` — `3e3a74f6b66a63bb838a436fb69f91fe5a3d1c16`
- `tests/test_replay_dataset_snapshot_integrity.py` — `a949f9110e8bef48fc2221987539a3ce96fb8543`
- `tests/test_causal_collector.py` — `cf53199711fad0e70f0187f23f17db54f4535cef`
- `tests/test_historical_retention_terms_binding.py` — `e9d838b2145df7e7778cfd440436088acebd4c35`

## Tested boundaries / negative and recovery coverage

- `storage.py`: one canonical SQLite append/history/current-projection authority, duplicate and stale-event admission, provider+market source identity, projection repair only from valid history; no fabricated missing history.
- Immutable history + generation/CAS + monotonic source and availability witnesses; invalid schema, index/trigger drift, altered payload, conflicting duplicate, out-of-range sequence and partial batch fail closed without partial commit.
- Snapshot lineage: versioned ancestry, canonical member digest and causal cutoff, source/time/availability monotonicity, crash windows, delete/reorder/rollback/branch substitution, restart validation and no-future leakage falsifiers.
- Run registry/transaction: atomic staging/precommit/commit, complete-vs-in-progress identity, durable target binding, recovery without replaying side effects, torn/partial/tampered files and mixed-version manifest rejection.
- Collector retention/historical snapshot: bounded compaction and retained lineage, causal ingestion/replay input, legal-retention reference and corrected source evidence, fail-closed incomplete/ambiguous historical membership.
- Focused tests in list above plus full CI testify to existing deterministic implementation. No second persistence or chronology authority, no unnecessary code rewrite, no rerunning already immutable DONE Section 1/2.

## Qualification limits

- This is repository-controllable Plan-1 Section-4 engineering evidence, not a claim of real-bookmaker entitlement, live financial execution, market profitability, signed final whole-product release, or manual NVDA qualification.
- `REAL_MONEY_EXECUTION=false`; `HUMAN_TESTED=false`; `NVDA_VERIFIED=false`.
- Under `AGENTS.md` Simplified Section Closure Protocol v3 and `MULTI_PLAN_CLOSURE_STATE.md`, Section 4 can be marked terminal DONE by reuse+executed CI+integration+postmerge exact-blob readback. No speculative source mutation is required; any demonstrated later regression requires an explicit reopen reason.
