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
    BookmakerCapabilityProfile,
    BookmakerPositionObservation,
    BookmakerPositionState,
)


_HASH = "a" * 64
_START = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _time(index: int) -> str:
    return (_START + timedelta(minutes=index)).isoformat()


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1",
        profile_version=1,
        facts=(),
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
        provider_amount=Decimal("10"),
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
    return BookmakerAccountSnapshot(
        profile=_profile(),
        observed_capabilities=frozenset(),
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

    segment_root = tmp_path / ".account.json.segments-v1"
    assert len(tuple(segment_root.glob("segment-*.json"))) == len(snapshots)

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

    segment = tmp_path / ".account.json.segments-v1" / "segment-00000000000000000001.json"
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

    segment = tmp_path / ".account.json.segments-v1" / "segment-00000000000000000002.json"
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
    _force_segmented(store, path)

    after = tuple(
        (snapshot_fingerprint(item), item.observed_at)
        for item in _store(path, authority_root).history()
    )
    assert after == before


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
    assert restarted.append_snapshot(_snapshot(65)) is True
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2
    assert len(restarted.history()) == 65


def test_segment_root_alias_is_rejected_before_history_read(tmp_path) -> None:
    if not hasattr(Path, "symlink_to"):
        pytest.skip("symlink support unavailable")
    path = tmp_path / "account.json"
    authority_root = tmp_path / "authority"
    store = _store(path, authority_root)
    assert store.append_snapshot(_snapshot(1))
    assert store.append_snapshot(_snapshot(2))
    _force_segmented(store, path)

    segment_root = tmp_path / ".account.json.segments-v1"
    moved = tmp_path / "segments-real"
    segment_root.rename(moved)
    try:
        segment_root.symlink_to(moved, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation unavailable")

    with pytest.raises(
        AccountReconciliationIntegrityError,
        match="segment root must be a direct directory",
    ):
        _store(path, authority_root).latest_state()
