# Late-crash reconciliation

Autosport remains fail-closed after an unresolved replay run. This recovery path does **not** make unresolved experiments freely rerunnable.

## Crash boundary

A normal dataset run persists in this order near completion:

1. paper strategy and settlement finish in memory;
2. canonical `paper_book.json` is atomically saved;
3. SHA-256 of the exact saved PaperBook bytes is computed;
4. evaluation/portfolio summary is written as atomic/fsync run-summary schema version 2;
5. `RunRegistry.complete()` changes the experiment from `in_progress` to `completed`.

A process can crash after step 4 and before step 5. In that narrow case the economic state and durable completion evidence already exist, but the registry still says `in_progress`.

## Evidence required to reconcile

`RunRegistry.reconcile_completed_summary()` will change an `in_progress` run to `completed` only when all of the following are true:

- the summary is inside the same workspace;
- its filename is exactly `run-<registry run_id>.json`;
- the canonical workspace `paper_book.json` exists;
- summary schema version is 2;
- `experiment_key`, `run_id`, market SHA-256, sealed-results SHA-256 and strategy id exactly match the registry;
- `real_money_execution` is explicitly `false`;
- summary contains a 64-character `paper_book_sha256`;
- SHA-256 of the current canonical `paper_book.json` exactly equals that summary hash;
- registry base identity recomputes correctly from market/results/strategy identity.

Only after every check passes is the registry atomically written as completed with `reconciled_from_summary=true`.

## Workspace command

For a Python/source installation:

```text
autosport repair-workspace --workspace PATH
```

The command scans only current `in_progress` registry records.

- a valid durable summary is reconciled;
- no summary means `UNRESOLVED_NO_DURABLE_SUMMARY` and a non-zero exit code;
- a summary/hash/identity mismatch is `FAIL_CLOSED` and a non-zero exit code;
- an empty workspace is a no-op and does not create a registry.

## What this does not repair

An early crash can happen before the durable run summary exists, including while Decision Ledger or in-memory/persistent PaperBook effects are only partially represented. Autosport does not guess whether replaying such a run is safe.

Therefore this V1 recovery path does not:

- delete or abandon an unresolved registry entry;
- rerun an unresolved dataset automatically;
- infer completion from Market Store events alone;
- infer completion from Decision Ledger entries alone;
- overwrite a mismatching PaperBook;
- claim rollback of partial side effects.

Early-crash recovery remains a separate future transactional-commit problem. Until that architecture exists, those states intentionally remain fail-closed.

`REAL_MONEY_EXECUTION=false` remains unchanged.
