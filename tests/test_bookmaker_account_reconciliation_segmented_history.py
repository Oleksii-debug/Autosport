from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

import autosport.bookmaker_account_reconciliation as reconciliation_module
from autosport.bookmaker_account_reconciliation import (
    AccountReconciliationIntegrityError,
    BookmakerAccountReconciliationStore,
    ReconciledPositionState,
    snapshot_fingerprint,
)
from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    BookmakerPositionObservation,
    BookmakerPositionState,
)


_HASH = "a" * 64
_START = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _time(index: int) -> str:
    return (_START + timedelta(minutes=index)).isoformat()


def _profile(
    *capabilities: BookmakerCapability,
) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1",
        profile_version=1,
        facts=tuple(
            BookmakerCapabilityFact(
                capability=capability,
                state=BookmakerCapabilityState.SUPPORTED,
            )
            for capability in capabilities
        ),
        observed_at=_time(0),
        source_ref="capability-probe",
        source_payload_sha256=_HASH,
    )


def _balance(index: int) -> BookmakerBalanceObservation:
    return BookmakerBalanceObservation(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        observation_id=f"balance-{index}",
        currency="EUR",
        available_balance=Decimal(1000 + index),
        observed_at=_time(index),
        source_payload_sha256=_HASH,
    )


def _position(
    state: BookmakerPositionState,
    observed_index: int,
    *,
    observation_id: str,
    provider_amount: Decimal = Decimal("10"),
) -> BookmakerPositionObservation:
    return BookmakerPositionObservation(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        observation_id=observation_id,
        external_position_id="pos-1",
        state=state,
        currency="EUR",
        observed_at=_time(observed_index),
        source_payload_sha256=_HASH,
        provider_amount=provider_amount,
        provider_amount_semantics="backer_stake",
        decimal_odds=Decimal("2.0"),
    )


def _snapshot(
    index: int,
    *,
    balance: bool = True,
    open_positions: tuple[BookmakerPositionObservation, ...] = (),
    settled_positions: tuple[BookmakerPositionObservation, ...] = (),
) -> BookmakerAccountSnapshot:
    capabilities: list[BookmakerCapability] = []
    if balance:
        capabilities.append(BookmakerCapability.BALANCE_READ)
    if open_positions:
        capabilities.append(BookmakerCapability.OPEN_POSITIONS_READ)
    if settled_positions:
        capabilities.append(BookmakerCapability.SETTLED_POSITIONS_READ)
    observed = frozenset(capabilities)
    return BookmakerAccountSnapshot(
        profile=_profile(*capabilities),
        observed_capabilities=observed,
        observed_at=_time(index),
        balance=_balance(index) if balance else None,
        open_positions=open_positions,
        settled_positions=settled_positions,
    )


def _store(path: Path, authority_root: Path) -> BookmakerAccountReconciliationStore:
    return BookmakerAccountReconciliationStore(
        path,
        authority_root=authority_root,
    )


def _force_segmented(
    store: BookmakerAccountReconciliationStore,
    path: Path,
) -> tuple[BookmakerAccountSnapshot, ...]:
    history = store.history()
    store._write_segmented_history(
        history,
        previous_root_bytes=path.read_bytes(),
    )
    return history


def test_auto_migration_keeps_long_history_valid_and_root_bounded(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)

    snapshots = tuple(_snapshot(index) for index in range(1, 67))
    for snapshot in snapshots:
        assert store.append_snapshot(snapshot) is True

    root = json.loads(path.read_text(encoding="utf-8"))
    assert root["schema_version"] == 2
    assert root["segmented_history"]["snapshot_count"] == len(snapshots)
    assert len(path.read_bytes()) < 4096

    assert len(tuple(tmp_path.glob(".account.json.segment-*.json"))) == len(snapshots)

    restarted = _store(path, authority_root)
    assert tuple(snapshot_fingerprint(item) for item in restarted.history()) == tuple(
        snapshot_fingerprint(item) for item in snapshots
    )
    state = restarted.latest_state()
    assert state is not None
    assert state.snapshot_id == snapshot_fingerprint(snapshots[-1])
    assert state.latest_balance_observation == snapshots[-1].balance
    assert state.unexplained_balance_delta is not None
    assert state.unexplained_balance_delta.amount == Decimal("1")


def test_segmented_restart_preserves_unresolved_position_then_marks_unknown(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    open_observation = _position(
        BookmakerPositionState.OPEN,
        1,
        observation_id="position-open-1",
    )
    first = _snapshot(1, open_positions=(open_observation,))
    second = _snapshot(2, open_positions=(open_observation,))
    assert store.append_snapshot(first)
    assert store.append_snapshot(second)
    _force_segmented(store, path)

    restarted = _store(path, authority_root)
    assert restarted.append_snapshot(_snapshot(3)) is True
    state = restarted.latest_state()
    assert state is not None
    assert state.position_state("pos-1") is ReconciledPositionState.UNKNOWN
    position = next(item for item in state.positions if item.external_position_id == "pos-1")
    assert position.last_observation_id == open_observation.observation_id
    assert position.last_observed_at == open_observation.observed_at


def test_segmented_history_cannot_forget_settlement_and_accept_open_regression(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    open_observation = _position(
        BookmakerPositionState.OPEN,
        1,
        observation_id="position-open-1",
    )
    settled_observation = _position(
        BookmakerPositionState.SETTLED,
        2,
        observation_id="position-settled-2",
    )
    assert store.append_snapshot(_snapshot(1, open_positions=(open_observation,)))
    assert store.append_snapshot(_snapshot(2, settled_positions=(settled_observation,)))
    _force_segmented(store, path)

    reopened = _position(
        BookmakerPositionState.OPEN,
        3,
        observation_id="position-open-3",
    )
    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="cannot regress to OPEN",
    ):
        _store(path, authority_root).append_snapshot(
            _snapshot(3, open_positions=(reopened,))
        )


def test_segmented_history_rejects_old_segment_tamper(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    assert store.append_snapshot(_snapshot(1))
    assert store.append_snapshot(_snapshot(2))
    _force_segmented(store, path)

    segment = tmp_path / ".account.json.segment-00000000000000000001.json"
    document = json.loads(segment.read_text(encoding="utf-8"))
    document["snapshot"]["observed_at"] = _time(9)
    segment.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(AccountReconciliationIntegrityError):
        _store(path, authority_root).latest_state()


def test_segmented_history_rejects_missing_ancestry(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    assert store.append_snapshot(_snapshot(1))
    assert store.append_snapshot(_snapshot(2))
    assert store.append_snapshot(_snapshot(3))
    _force_segmented(store, path)

    segment = tmp_path / ".account.json.segment-00000000000000000002.json"
    segment.unlink()

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="segment is missing",
    ):
        _store(path, authority_root).latest_snapshot()


def test_segmented_restart_keeps_idempotent_snapshot_identity(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    first = _snapshot(1)
    second = _snapshot(2)
    assert store.append_snapshot(first)
    assert store.append_snapshot(second)
    _force_segmented(store, path)

    restarted = _store(path, authority_root)
    assert restarted.append_snapshot(first) is False
    assert restarted.append_snapshot(second) is False
    assert restarted.history() == (first, second)


def test_v1_migration_preserves_original_snapshot_evidence_exactly(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    snapshots = (_snapshot(1), _snapshot(2), _snapshot(3))
    for snapshot in snapshots:
        assert store.append_snapshot(snapshot)

    before = tuple(
        (snapshot_fingerprint(item), item.observed_at)
        for item in store.history()
    )
    v1_document = json.loads(path.read_text(encoding="utf-8"))
    original_entries = tuple(v1_document["snapshots"])

    _force_segmented(store, path)

    after = tuple(
        (snapshot_fingerprint(item), item.observed_at)
        for item in _store(path, authority_root).history()
    )
    assert after == before

    # Migration changes storage topology only.  Every canonical provider snapshot
    # payload and fingerprint must survive exactly, rather than being re-observed or
    # normalized into newly minted evidence.
    for index, original in enumerate(original_entries, start=1):
        segment_path = tmp_path / f".account.json.segment-{index:020d}.json"
        segment = json.loads(segment_path.read_text(encoding="utf-8"))
        assert segment["snapshot_id"] == original["snapshot_id"]
        assert segment["snapshot"] == original["snapshot"]


def test_failed_root_publish_leaves_authoritative_v1_state_recoverable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    for index in range(1, 65):
        assert store.append_snapshot(_snapshot(index))

    before = path.read_bytes()
    assert json.loads(before)["schema_version"] == 1

    real_replace = reconciliation_module.os.replace

    def fail_only_root_publish(source, destination) -> None:
        if Path(destination) == path:
            raise OSError("simulated root publication failure")
        real_replace(source, destination)

    with monkeypatch.context() as context:
        context.setattr(reconciliation_module.os, "replace", fail_only_root_publish)
        with pytest.raises(
            AccountReconciliationIntegrityError,
            match="failed to durably publish account reconciliation store",
        ):
            store.append_snapshot(_snapshot(65))

    assert path.read_bytes() == before
    restarted = _store(path, authority_root)
    assert len(restarted.history()) == 64

    # The orphaned segment-65 bytes were never authorized by the still-v1 root.
    # A later legitimate observation must be able to replace that abandoned sidecar
    # rather than turning a recoverable PREPARE crash into permanent unavailability.
    replacement = _snapshot(66)
    assert restarted.append_snapshot(replacement) is True
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2
    history = restarted.history()
    assert len(history) == 65
    assert history[-1] == replacement
    assert _snapshot(65) not in history


def test_segment_final_component_alias_is_rejected_before_history_read(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    assert store.append_snapshot(_snapshot(1))
    assert store.append_snapshot(_snapshot(2))
    _force_segmented(store, path)

    segment = tmp_path / ".account.json.segment-00000000000000000001.json"
    moved = tmp_path / "segment-real.json"
    segment.rename(moved)
    try:
        segment.symlink_to(moved)
    except OSError:
        pytest.skip("file symlink creation unavailable")

    with pytest.raises(AccountReconciliationIntegrityError):
        _store(path, authority_root).latest_state()


def test_segmented_history_rejects_reused_balance_id_with_changed_payload(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    first = _snapshot(1)
    second = _snapshot(2, balance=False)
    assert store.append_snapshot(first)
    assert store.append_snapshot(second)
    _force_segmented(store, path)

    assert first.balance is not None
    conflicting_balance = BookmakerBalanceObservation(
        venue_id=first.balance.venue_id,
        account_id=first.balance.account_id,
        adapter_id=first.balance.adapter_id,
        observation_id=first.balance.observation_id,
        currency=first.balance.currency,
        available_balance=first.balance.available_balance + Decimal("5"),
        observed_at=first.balance.observed_at,
        source_payload_sha256=first.balance.source_payload_sha256,
    )
    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="balance observation_id was reused with conflicting content",
    ):
        _store(path, authority_root).append_snapshot(
            BookmakerAccountSnapshot(
                profile=_profile(BookmakerCapability.BALANCE_READ),
                observed_capabilities=frozenset({BookmakerCapability.BALANCE_READ}),
                observed_at=_time(3),
                balance=conflicting_balance,
                open_positions=(),
                settled_positions=(),
            )
        )


def test_segment_schema_module_rebinding_cannot_reinterpret_durable_chain(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    first = _snapshot(1)
    second = _snapshot(2)
    assert store.append_snapshot(first)
    assert store.append_snapshot(second)
    _force_segmented(store, path)

    with monkeypatch.context() as context:
        context.setattr(
            reconciliation_module,
            "_SEGMENT_SCHEMA",
            "attacker-controlled-segment-schema",
        )
        context.setattr(reconciliation_module, "_SEGMENT_SCHEMA_VERSION", 999)
        context.setattr(reconciliation_module, "_SEGMENTED_STORE_SCHEMA_VERSION", 999)
        context.setattr(reconciliation_module, "_SEGMENT_MIGRATION_SNAPSHOT_THRESHOLD", 1)
        context.setattr(reconciliation_module, "_SEGMENT_MIGRATION_BYTE_THRESHOLD", 1)

        restarted = _store(path, authority_root)
        assert restarted.latest_snapshot() == second
        state = restarted.latest_state()
        assert state is not None
        assert state.snapshot_id == snapshot_fingerprint(second)


def test_tampered_huge_snapshot_count_is_rejected_by_root_authority_first(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    assert store.append_snapshot(_snapshot(1))
    assert store.append_snapshot(_snapshot(2))
    _force_segmented(store, path)

    document = json.loads(path.read_text(encoding="utf-8"))
    document["segmented_history"]["snapshot_count"] = 1_000_000_000
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="failed independent monotonic authority validation",
    ):
        _store(path, authority_root).latest_state()


def test_failed_v2_root_publish_allows_different_next_snapshot_after_restart(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    first = _snapshot(1)
    second = _snapshot(2)
    assert store.append_snapshot(first)
    assert store.append_snapshot(second)
    _force_segmented(store, path)

    before = path.read_bytes()
    assert json.loads(before)["schema_version"] == 2
    assert json.loads(before)["segmented_history"]["snapshot_count"] == 2

    real_replace = reconciliation_module.os.replace

    def fail_only_root_publish(source, destination) -> None:
        if Path(destination) == path:
            raise OSError("simulated v2 root publication failure")
        real_replace(source, destination)

    with monkeypatch.context() as context:
        context.setattr(reconciliation_module.os, "replace", fail_only_root_publish)
        with pytest.raises(
            AccountReconciliationIntegrityError,
            match="failed to durably publish account reconciliation store",
        ):
            store.append_snapshot(_snapshot(3))

    assert path.read_bytes() == before
    orphan = tmp_path / ".account.json.segment-00000000000000000003.json"
    assert orphan.exists()

    restarted = _store(path, authority_root)
    replacement = _snapshot(4)
    assert restarted.append_snapshot(replacement) is True

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 2
    assert document["segmented_history"]["snapshot_count"] == 3
    history = restarted.history()
    assert history == (first, second, replacement)
    assert _snapshot(3) not in history


def test_segmented_root_deletion_cannot_pristine_rebootstrap_over_retained_segments(
    tmp_path,
) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    first = _snapshot(1)
    second = _snapshot(2)
    assert store.append_snapshot(first)
    assert store.append_snapshot(second)
    _force_segmented(store, path)
    assert tuple(tmp_path.glob(".account.json.segment-*.json"))

    path.unlink()

    restarted = _store(path, authority_root)
    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="failed independent monotonic authority validation",
    ):
        restarted.latest_state()
    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="failed independent monotonic authority validation",
    ):
        restarted.append_snapshot(_snapshot(3))
    assert not path.exists()


def test_segmented_root_cannot_roll_back_to_pre_migration_v1_bytes(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    first = _snapshot(1)
    second = _snapshot(2)
    assert store.append_snapshot(first)
    assert store.append_snapshot(second)
    v1_bytes = path.read_bytes()
    assert json.loads(v1_bytes)["schema_version"] == 1

    _force_segmented(store, path)
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2

    path.write_bytes(v1_bytes)

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="failed independent monotonic authority validation",
    ):
        _store(path, authority_root).latest_state()


def test_pending_intended_root_does_not_commit_before_segment_semantic_proof(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    assert store.append_snapshot(_snapshot(1))
    assert store.append_snapshot(_snapshot(2))
    _force_segmented(store, path)

    real_replace = reconciliation_module.os.replace
    corrupted = False

    def corrupt_segment_after_root_publish(source, destination) -> None:
        nonlocal corrupted
        real_replace(source, destination)
        if Path(destination) == path and not corrupted:
            corrupted = True
            segment_path = tmp_path / ".account.json.segment-00000000000000000003.json"
            document = json.loads(segment_path.read_text(encoding="utf-8"))
            document["snapshot"]["observed_at"] = _time(99)
            segment_path.write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

    with monkeypatch.context() as context:
        context.setattr(
            reconciliation_module.os,
            "replace",
            corrupt_segment_after_root_publish,
        )
        with pytest.raises(AccountReconciliationIntegrityError):
            store.append_snapshot(_snapshot(3))

    assert corrupted is True
    records = store._authority.read_history()
    assert records[-1].phase.value == "PREPARE"

    restarted = _store(path, authority_root)
    with pytest.raises(AccountReconciliationIntegrityError):
        restarted.latest_state()
    records_after_restart = restarted._authority.read_history()
    assert records_after_restart[-1].phase.value == "PREPARE"


def test_segmented_store_cannot_downgrade_back_to_monolithic_writer(tmp_path) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    first = _snapshot(1)
    second = _snapshot(2)
    assert store.append_snapshot(first)
    assert store.append_snapshot(second)
    history = _force_segmented(store, path)
    before = path.read_bytes()

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="cannot downgrade to monolithic schema",
    ):
        store._write_history(list(history))

    assert path.read_bytes() == before
    assert _store(path, authority_root).history() == history


def test_segmented_history_rejects_reused_position_id_with_changed_payload(
    tmp_path,
) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    original = _position(
        BookmakerPositionState.OPEN,
        1,
        observation_id="position-reused",
        provider_amount=Decimal("10"),
    )
    assert store.append_snapshot(_snapshot(1, open_positions=(original,)))
    assert store.append_snapshot(_snapshot(2, open_positions=(original,)))
    _force_segmented(store, path)

    conflicting = _position(
        BookmakerPositionState.OPEN,
        1,
        observation_id="position-reused",
        provider_amount=Decimal("11"),
    )
    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="position observation_id was reused with conflicting content",
    ):
        _store(path, authority_root).append_snapshot(
            _snapshot(3, open_positions=(conflicting,))
        )


def test_segmented_decode_dispatch_rebind_fails_before_callback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    first = _snapshot(1)
    second = _snapshot(2)
    assert store.append_snapshot(first)
    assert store.append_snapshot(second)
    _force_segmented(store, path)

    canonical_decode = reconciliation_module._decode_snapshot
    callback_reached = False

    def hostile_decode(*args, **kwargs):
        nonlocal callback_reached
        callback_reached = True
        return canonical_decode(*args, **kwargs)

    monkeypatch.setattr(
        reconciliation_module,
        "_decode_snapshot",
        hostile_decode,
    )

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="transitive module dispatch graph changed",
    ):
        _store(path, authority_root).latest_snapshot()

    assert callback_reached is False
