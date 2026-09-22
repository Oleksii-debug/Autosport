from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betfair_replace_saga import (
    BetfairReplaceSagaIntegrityError,
    BetfairReplaceSagaStore,
    OriginalOrderState,
    ReplaceInstruction,
    ReplaceInstructionReconciliation,
    ReplaceIntent,
    ReplaceReconciliationEvidence,
    ReplaceSagaState,
)


PREPARED = "2026-09-22T02:20:00+00:00"
SUBMITTED = "2026-09-22T02:20:01+00:00"
UNKNOWN = "2026-09-22T02:20:02+00:00"
RECONCILED = "2026-09-22T02:20:03+00:00"
AUTHORITY_SHA = "a" * 64
RECONCILIATION_SHA = "b" * 64
LINKAGE_SHA = "c" * 64
SAGA_ID = "replace-monotonic-falsifier-1"


def _intent() -> ReplaceIntent:
    return ReplaceIntent(
        saga_id=SAGA_ID,
        account_id="acct-1",
        environment="supervised-live",
        market_id="1.234567",
        authority_ref="owner-goal:goal-1:revision:11",
        authority_sha256=AUTHORITY_SHA,
        prepared_at=PREPARED,
        market_version=42,
        instructions=(ReplaceInstruction("old-bet-1", Decimal("2.50")),),
    )


def _conflicting_reconciliation() -> ReplaceReconciliationEvidence:
    return ReplaceReconciliationEvidence(
        evidence_id=RECONCILIATION_SHA,
        observed_at=RECONCILED,
        source="betfair:canonical-current+cleared-readback",
        results=(
            ReplaceInstructionReconciliation(
                bet_id="old-bet-1",
                original_state=OriginalOrderState.EXECUTABLE,
                new_bet_id="new-bet-1",
                linkage_sha256=LINKAGE_SHA,
            ),
        ),
    )


def _prepared_store(tmp_path) -> BetfairReplaceSagaStore:
    store = BetfairReplaceSagaStore(tmp_path / "replace-sagas.jsonl")
    snapshot = store.prepare(_intent())
    assert snapshot.state is ReplaceSagaState.PREPARED
    return store


def _submitted_unknown_store(tmp_path) -> BetfairReplaceSagaStore:
    store = _prepared_store(tmp_path)
    store.mark_submitted(SAGA_ID, submitted_at=SUBMITTED)
    store.mark_unknown(
        SAGA_ID,
        reason="replaceOrders response lost",
        observed_at=UNKNOWN,
    )
    return store


def test_valid_prepared_prefix_rollback_after_external_boundary_is_rejected(tmp_path):
    """A self-consistent old prefix cannot erase a crossed provider boundary."""

    store = _prepared_store(tmp_path)
    prepared_prefix = store.path.read_bytes()

    store.mark_submitted(SAGA_ID, submitted_at=SUBMITTED)
    store.mark_unknown(
        SAGA_ID,
        reason="replaceOrders response lost",
        observed_at=UNKNOWN,
    )
    assert store.snapshot(SAGA_ID).state is ReplaceSagaState.SUBMITTED_UNKNOWN

    # The old PREPARED-only journal is internally hash-valid. Local JSONL
    # integrity alone therefore cannot distinguish this rollback.
    store.path.write_bytes(prepared_prefix)

    with pytest.raises(BetfairReplaceSagaIntegrityError):
        BetfairReplaceSagaStore(store.path).snapshot(SAGA_ID)


def test_valid_submitted_prefix_cannot_erase_later_conflict_high_water(tmp_path):
    """Rollback must not erase a durable impossible dual-order incident."""

    store = _prepared_store(tmp_path)
    store.mark_submitted(SAGA_ID, submitted_at=SUBMITTED)
    submitted_prefix = store.path.read_bytes()

    store.mark_unknown(
        SAGA_ID,
        reason="replaceOrders response lost",
        observed_at=UNKNOWN,
    )
    conflict = store.record_reconciliation(
        SAGA_ID,
        _conflicting_reconciliation(),
    )
    assert conflict.state is ReplaceSagaState.CONFLICT

    store.path.write_bytes(submitted_prefix)

    with pytest.raises(BetfairReplaceSagaIntegrityError):
        BetfairReplaceSagaStore(store.path).snapshot(SAGA_ID)


def test_journal_deletion_after_external_boundary_cannot_rebootstrap_pristine(tmp_path):
    """Independent machine authority must survive deletion of local journal bytes."""

    store = _submitted_unknown_store(tmp_path)
    store.path.unlink()

    with pytest.raises(BetfairReplaceSagaIntegrityError):
        BetfairReplaceSagaStore(store.path).snapshot(SAGA_ID)


def test_copying_committed_journal_to_new_key_cannot_mint_fresh_ancestry(tmp_path):
    """A same-workspace filename alias must not become a fresh trusted history."""

    store = _submitted_unknown_store(tmp_path)
    copied_path = tmp_path / "copied-replace-sagas.jsonl"
    copied_path.write_bytes(store.path.read_bytes())

    with pytest.raises(BetfairReplaceSagaIntegrityError):
        BetfairReplaceSagaStore(copied_path).snapshot(SAGA_ID)


def test_copying_committed_journal_to_new_workspace_cannot_mint_fresh_ancestry(
    tmp_path,
):
    """A copied workspace needs its own proven lineage; bytes alone are insufficient."""

    store = _submitted_unknown_store(tmp_path)
    copied_workspace = tmp_path / "copied-workspace"
    copied_workspace.mkdir()
    copied_path = copied_workspace / "replace-sagas.jsonl"
    copied_path.write_bytes(store.path.read_bytes())

    with pytest.raises(BetfairReplaceSagaIntegrityError):
        BetfairReplaceSagaStore(copied_path).snapshot(SAGA_ID)
