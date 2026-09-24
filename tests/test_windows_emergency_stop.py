from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from autosport.execution_stop_authority import (
    ExecutionAuthorityMode,
    ExecutionStopAuthority,
)
from autosport.windows_emergency_stop import (
    EXECUTION_STOP_JOURNAL_FILENAME,
    WindowsEmergencyStopBridge,
    execution_stop_path,
)


def _armed_authority(tmp_path) -> ExecutionStopAuthority:
    authority = ExecutionStopAuthority(execution_stop_path(tmp_path))
    stopped = authority.initialize_stopped(
        operator_id="test-operator",
        reason="initial-safe-state",
    )
    authority.arm(
        operator_id="test-operator",
        reason="test-arm",
        confirmation_id="test-confirmation",
        expected_revision=stopped.revision,
    )
    return authority


def test_execution_stop_path_is_bound_to_canonical_workspace(tmp_path) -> None:
    assert execution_stop_path(tmp_path) == tmp_path / EXECUTION_STOP_JOURNAL_FILENAME


def test_windows_stop_mutates_provider_admission_authority_without_legacy_second_journal(
    tmp_path,
) -> None:
    provider_authority = ExecutionStopAuthority(tmp_path / "execution-stop.jsonl")
    stopped = provider_authority.initialize_stopped(
        operator_id="provider-owner",
        reason="safe initialization",
        command_id="windows-provider-path-init",
    )
    provider_authority.arm(
        operator_id="provider-owner",
        reason="supervised execution explicitly armed",
        confirmation_id="windows-provider-path-confirmation",
        expected_revision=stopped.revision,
        command_id="windows-provider-path-arm",
    )
    assert provider_authority.current().mode is ExecutionAuthorityMode.ARMED

    result = WindowsEmergencyStopBridge.for_workspace(tmp_path).activate()

    assert result.stopped is True
    assert provider_authority.current().mode is ExecutionAuthorityMode.STOPPED
    assert not (tmp_path / "execution-stop-authority.jsonl").exists()


def test_bridge_initializes_missing_authority_directly_stopped(tmp_path) -> None:
    authority = ExecutionStopAuthority(execution_stop_path(tmp_path))
    bridge = WindowsEmergencyStopBridge(authority)

    result = bridge.activate()

    assert result.stopped is True
    assert result.already_stopped is False
    assert result.revision == 1
    assert result.runtime_cooperation_verified is False
    assert authority.current().mode is ExecutionAuthorityMode.STOPPED
    assert "Аварійний STOP" in result.message_uk
    assert "Emergency STOP" in result.message_en


def test_bridge_stops_armed_authority_and_confirms_durable_state(tmp_path) -> None:
    authority = _armed_authority(tmp_path)
    bridge = WindowsEmergencyStopBridge(authority)

    result = bridge.activate()
    confirmed = authority.current()

    assert result.stopped is True
    assert result.already_stopped is False
    assert confirmed.mode is ExecutionAuthorityMode.STOPPED
    assert result.revision == confirmed.revision == 3
    assert result.command_id == confirmed.command_id
    assert result.runtime_cooperation_verified is False


def test_repeated_stop_activation_is_idempotent(tmp_path) -> None:
    authority = _armed_authority(tmp_path)
    bridge = WindowsEmergencyStopBridge(authority)

    first = bridge.activate()
    second = bridge.activate()
    confirmed = authority.current()

    assert first.stopped is True
    assert second.stopped is True
    assert second.already_stopped is True
    assert first.revision == second.revision == confirmed.revision == 3
    assert first.command_id == second.command_id == confirmed.command_id
    assert len(authority.path.read_text(encoding="utf-8").splitlines()) == 3


def test_concurrent_keyboard_and_button_attempts_collapse_to_one_stop(tmp_path) -> None:
    authority = _armed_authority(tmp_path)
    bridge = WindowsEmergencyStopBridge(authority)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: bridge.activate(), range(24)))

    confirmed = authority.current()
    assert confirmed.mode is ExecutionAuthorityMode.STOPPED
    assert confirmed.revision == 3
    assert all(result.stopped for result in results)
    assert {result.revision for result in results} == {3}
    assert {result.command_id for result in results} == {confirmed.command_id}
    assert len(authority.path.read_text(encoding="utf-8").splitlines()) == 3


def test_missing_bridge_authority_is_explicit_fail_closed() -> None:
    result = WindowsEmergencyStopBridge(None).activate()

    assert result.stopped is False
    assert result.revision is None
    assert result.command_id is None
    assert result.runtime_cooperation_verified is False
    assert "НЕ ПІДТВЕРДЖЕНО" in result.message_uk
    assert "NOT CONFIRMED" in result.message_en
    assert "unavailable" in (result.error or "")


def test_corrupt_authority_never_gets_overwritten_or_reported_stopped(tmp_path) -> None:
    authority = ExecutionStopAuthority(execution_stop_path(tmp_path))
    authority.path.write_text("corrupt\n", encoding="utf-8")
    before = authority.path.read_bytes()

    result = WindowsEmergencyStopBridge(authority).activate()

    assert result.stopped is False
    assert result.revision is None
    assert result.runtime_cooperation_verified is False
    assert authority.path.read_bytes() == before
    assert not authority.anchor_path.exists()


def test_success_text_never_claims_runtime_worker_or_feed_drain(tmp_path) -> None:
    result = WindowsEmergencyStopBridge(_armed_authority(tmp_path)).activate()

    assert result.stopped is True
    assert result.runtime_cooperation_verified is False
    assert "не є доказом" in result.message_uk
    assert "does not prove" in result.message_en
