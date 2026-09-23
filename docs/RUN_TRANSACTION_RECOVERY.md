# Transactional paper-run recovery

Autosport paper dataset runs use a staged copy-on-write commit protocol so a process crash cannot silently leave half-applied economic state.

## Scope

The transaction covers the canonical economic/audit artifacts:

- `paper_book.json`
- `decisions.jsonl`
- `run-<run_id>.json`

The replay Market Store remains append-only/idempotent. Replaying a safely aborted attempt may encounter already persisted market events, but those events do not themselves spend virtual bankroll or create canonical paper decisions.

`REAL_MONEY_EXECUTION=false` remains invariant.

## Protocol

Each dataset run is serialized as the only in-progress economic writer for its workspace.

1. Persist the current canonical PaperBook and ensure the canonical Decision Ledger exists.
2. Record their exact SHA-256 hashes in `RunRegistry.begin(...)`.
3. Create `.run-transactions/<run_id>/manifest.json` in `staging` phase.
4. Run replay and agents against:
   - a copy of the canonical PaperBook;
   - a run-private Decision Ledger.
5. Settlement/evaluation operate on the staged PaperBook only.
6. Write staged full replacement artifacts:
   - `paper_book.next.json`;
   - `decisions.next.jsonl` = canonical ledger bytes + this run's private ledger;
   - `run-summary.next.json`.
7. Durably write a `precommitted` manifest containing exact BASE and NEW hashes.
8. Promote the three canonical artifacts with `os.replace`, verifying every target hash.
9. Mark the manifest `canonical_committed`.
10. Mark the RunRegistry entry completed.

No canonical PaperBook or Decision Ledger mutation occurs before durable PRECOMMIT.

## Recovery truth table

For a transaction-aware `in_progress` registry entry:

### No transaction manifest yet

If the registry has BASE hashes and both canonical economic files still equal those hashes, the run is proven to have never committed economic effects. Recovery marks that attempt `aborted`; a later retry uses a new audit key.

Any mismatch fails closed.

### `staging`

Both canonical economic files must still equal BASE and there must be no canonical run summary. Recovery marks the attempt `aborted`.

Any unexpected canonical mutation fails closed.

### `precommitted` or `canonical_committed`

For PaperBook and Decision Ledger each canonical target must be exactly one of:

- BASE hash; or
- NEW hash.

If BASE, recovery requires the corresponding staged artifact to exist with the declared NEW hash, then promotes it. If already NEW, promotion is idempotently skipped.

The run summary must be absent with a valid staged copy, or already present with its declared hash.

After all canonical artifacts are NEW, existing schema-v2 summary reconciliation verifies run identity and PaperBook/Decision-Ledger hashes before completing the registry entry.

Any third hash, missing required staged artifact, path mismatch, identity mismatch, malformed manifest or truth-boundary violation fails closed.

## Teardown safety

`AutosportSession.close()` does not save its in-memory PaperBook while any registry entry is `in_progress`. This prevents a `finally` block from overwriting a partially promoted NEW PaperBook with stale BASE state.

## Legacy recovery

PR #20 schema-v2 late-crash recovery remains supported for old registry entries that predate transaction BASE hashes. Such runs are only reconciled when their durable summary and canonical PaperBook hash prove completion.

## Non-claims

This protocol does not make multiple processes safe to write the same workspace concurrently; product operation still uses one economic writer per workspace. It also does not make market/provider acquisition transactional with paper economics. Its purpose is narrower: canonical paper bankroll/tickets, canonical decision audit history and run completion cannot be silently duplicated or half-applied after a crash.
