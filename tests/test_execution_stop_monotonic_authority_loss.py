from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from autosport.execution_stop_authority import (
    ExecutionAuthorityMode,
    ExecutionStopAuthority,
    ExecutionStopIntegrityError,
)


def _erase_per_key_machine_authority(authority: ExecutionStopAuthority) -> None:
    machine = authority._monotonic_authority()
    if machine.journal_dir.exists():
        shutil.rmtree(machine.journal_dir)
    if machine.namespace_marker_path.exists():
        machine.namespace_marker_path.unlink()


def test_bound_stop_state_cannot_readopt_after_machine_history_loss(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    authority = ExecutionStopAuthority(path)
    state = authority.initialize_stopped(
        operator_id="owner",
        reason="initial safe state",
        command_id="stop-r1",
    )
    assert state.mode is ExecutionAuthorityMode.STOPPED

    machine = authority._monotonic_authority()
    receipt_path = authority._monotonic_receipt_path(machine)
    assert receipt_path.exists()
    assert machine.read_history()

    # Simulate selective loss of the per-key monotonic journal and namespace
    # marker while the independent machine root and STOP binding receipt survive.
    _erase_per_key_machine_authority(authority)

    restarted = ExecutionStopAuthority(path)
    decision = restarted.decision()
    assert decision.allowed is False
    assert decision.mode is ExecutionAuthorityMode.STOPPED
    assert decision.revision is None
    assert "missing after prior binding" in decision.reason

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="history is missing after prior binding",
    ):
        restarted.current()

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="history is missing after prior binding",
    ):
        restarted.stop(
            operator_id="owner",
            reason="must not recreate lost independent authority",
            expected_revision=1,
            command_id="stop-r2",
        )


def test_legacy_pair_migrates_once_but_cannot_be_readopted_after_bind_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "execution-stop.jsonl"

    # Build an exact valid pre-monotonic STOP journal+anchor without any
    # independent machine authority. This models an upgrade from the legacy
    # workspace format rather than deleting current authority evidence.
    with monkeypatch.context() as patch:
        patch.setattr(
            ExecutionStopAuthority,
            "_prepare_monotonic_transition_unlocked",
            lambda self, *, records, record: (
                object(),
                "legacy-noop-tx",
                "0" * 64,
                "1" * 64,
            ),
        )
        patch.setattr(
            ExecutionStopAuthority,
            "_commit_monotonic_transition_unlocked",
            lambda self, **kwargs: None,
        )
        legacy = ExecutionStopAuthority(path)
        legacy.initialize_stopped(
            operator_id="owner",
            reason="pre-monotonic legacy safe state",
            command_id="legacy-stop-r1",
        )

    migrated = ExecutionStopAuthority(path)
    state = migrated.current()
    assert state.revision == 1
    assert state.mode is ExecutionAuthorityMode.STOPPED
    assert state.command_id == "legacy-stop-r1"

    machine = migrated._monotonic_authority()
    receipt_path = migrated._monotonic_receipt_path(machine)
    assert receipt_path.exists()
    assert machine.read_history()

    # Once the migration has established independent authority, losing that
    # authority is not another migration opportunity.
    _erase_per_key_machine_authority(migrated)

    restarted = ExecutionStopAuthority(path)
    with pytest.raises(
        ExecutionStopIntegrityError,
        match="history is missing after prior binding",
    ):
        restarted.current()


def test_existing_machine_history_without_receipt_is_backfilled_safely(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    authority = ExecutionStopAuthority(path)
    expected = authority.initialize_stopped(
        operator_id="owner",
        reason="existing pre-receipt monotonic state",
        command_id="stop-r1",
    )

    machine = authority._monotonic_authority()
    receipt_path = authority._monotonic_receipt_path(machine)
    history_before = machine.read_history()
    assert history_before
    receipt_path.unlink()

    restarted = ExecutionStopAuthority(path)
    assert restarted.current() == expected
    assert receipt_path.exists()
    assert restarted._monotonic_authority().read_history() == history_before


def test_tampered_binding_receipt_fails_closed_without_rewriting_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "execution-stop.jsonl"
    authority = ExecutionStopAuthority(path)
    authority.initialize_stopped(
        operator_id="owner",
        reason="initial safe state",
        command_id="stop-r1",
    )

    machine = authority._monotonic_authority()
    receipt_path = authority._monotonic_receipt_path(machine)
    original = receipt_path.read_text(encoding="utf-8")
    tampered = original.replace(
        '"domain":"execution-stop-authority"',
        '"domain":"wrong-authority"',
    )
    assert tampered != original
    receipt_path.write_text(tampered, encoding="utf-8")

    restarted = ExecutionStopAuthority(path)
    with pytest.raises(
        ExecutionStopIntegrityError,
        match="identity or digest mismatch",
    ):
        restarted.current()
    assert receipt_path.read_text(encoding="utf-8") == tampered
