from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json
import os

import pytest

import autosport.betfair_replace_saga as replace_saga_module

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
from autosport.monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
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


def test_crash_after_monotonic_prepare_before_local_publish_aborts_then_retry_converges(
    tmp_path,
    monkeypatch,
):
    store = _prepared_store(tmp_path)
    prepared_bytes = store.path.read_bytes()
    original_prepare = MonotonicWorkspaceAuthority.prepare

    def _crash_after_prepare(self, **kwargs):
        original_prepare(self, **kwargs)
        raise MonotonicWorkspaceAuthorityError(
            "simulated crash after monotonic PREPARE"
        )

    monkeypatch.setattr(
        MonotonicWorkspaceAuthority,
        "prepare",
        _crash_after_prepare,
    )
    with pytest.raises(BetfairReplaceSagaIntegrityError, match="monotonic authority"):
        store.mark_submitted(SAGA_ID, submitted_at=SUBMITTED)

    assert store.path.read_bytes() == prepared_bytes

    monkeypatch.setattr(
        MonotonicWorkspaceAuthority,
        "prepare",
        original_prepare,
    )
    recovered = store.mark_submitted(SAGA_ID, submitted_at=SUBMITTED)
    assert recovered.state is ReplaceSagaState.SUBMITTED_UNKNOWN
    assert (
        BetfairReplaceSagaStore(store.path).snapshot(SAGA_ID).state
        is ReplaceSagaState.SUBMITTED_UNKNOWN
    )


def test_crash_after_exact_local_publish_before_monotonic_commit_recovers_commit(
    tmp_path,
    monkeypatch,
):
    store = _prepared_store(tmp_path)
    original_commit = MonotonicWorkspaceAuthority.commit

    def _crash_before_commit(self, **kwargs):
        raise MonotonicWorkspaceAuthorityError(
            "simulated crash before monotonic COMMIT"
        )

    monkeypatch.setattr(
        MonotonicWorkspaceAuthority,
        "commit",
        _crash_before_commit,
    )
    with pytest.raises(BetfairReplaceSagaIntegrityError, match="monotonic authority"):
        store.mark_submitted(SAGA_ID, submitted_at=SUBMITTED)

    assert b'"event_type":"SUBMITTED"' in store.path.read_bytes()

    monkeypatch.setattr(
        MonotonicWorkspaceAuthority,
        "commit",
        original_commit,
    )
    recovered = BetfairReplaceSagaStore(store.path).snapshot(SAGA_ID)
    assert recovered.state is ReplaceSagaState.SUBMITTED_UNKNOWN


def test_locally_valid_manual_append_without_monotonic_commit_is_rejected(tmp_path):
    store = _submitted_unknown_store(tmp_path)
    evidence = _conflicting_reconciliation()

    envelopes = [
        json.loads(line)
        for line in store.path.read_text(encoding="utf-8").splitlines()
    ]
    previous = envelopes[-1]["sha256"]
    event = {
        "schema_version": 1,
        "event_id": "manual-uncommitted-reconciliation",
        "event_type": "RECONCILIATION",
        "recorded_at": evidence.observed_at,
        "saga_id": SAGA_ID,
        "prev_sha256": previous,
        "payload": {"evidence": evidence.to_dict()},
    }
    event_text = json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(event_text.encode("utf-8")).hexdigest()
    envelope = json.dumps(
        {"event": event, "sha256": digest},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    with store.path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(envelope + "\n")

    with pytest.raises(BetfairReplaceSagaIntegrityError, match="monotonic authority"):
        BetfairReplaceSagaStore(store.path).snapshot(SAGA_ID)


def test_public_reads_recover_monotonic_state_only_under_canonical_journal_lock(
    tmp_path,
    monkeypatch,
):
    store = _submitted_unknown_store(tmp_path)
    real_lock = replace_saga_module.durable_path_lock
    real_ensure = BetfairReplaceSagaStore._ensure_monotonic_current
    lock_depth = 0
    ensure_calls = 0

    @contextmanager
    def _tracking_lock(path):
        nonlocal lock_depth
        with real_lock(path):
            lock_depth += 1
            try:
                yield
            finally:
                lock_depth -= 1

    def _assert_locked(self, raw):
        nonlocal ensure_calls
        assert lock_depth > 0
        ensure_calls += 1
        return real_ensure(self, raw)

    monkeypatch.setattr(replace_saga_module, "durable_path_lock", _tracking_lock)
    monkeypatch.setattr(
        BetfairReplaceSagaStore,
        "_ensure_monotonic_current",
        _assert_locked,
    )

    assert store.snapshot(SAGA_ID).state is ReplaceSagaState.SUBMITTED_UNKNOWN
    assert store.verify_integrity() == 3
    assert ensure_calls >= 2


def test_journal_path_identity_preserves_lexical_reparse_location(tmp_path):
    target = tmp_path / "target-workspace"
    target.mkdir()
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlink/junction creation is unavailable")

    journal = alias / "replace-sagas.jsonl"
    store = BetfairReplaceSagaStore(journal)

    assert os.fspath(store._absolute_path()) == os.path.abspath(os.fspath(journal))
    assert os.fspath(store._absolute_path().parent) == os.path.abspath(
        os.fspath(alias)
    )


def test_relative_journal_path_identity_is_frozen_across_cwd_change(
    tmp_path,
    monkeypatch,
):
    first_cwd = tmp_path / "first-cwd"
    second_cwd = tmp_path / "second-cwd"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    store = BetfairReplaceSagaStore("state/replace-sagas.jsonl")
    expected = first_cwd / "state" / "replace-sagas.jsonl"

    monkeypatch.chdir(second_cwd)

    assert store.path == expected
    assert store._absolute_path() == expected
