from autosport.execution_stop_authority import (
    ExecutionAuthorityMode,
    ExecutionStopAuthority,
)
from autosport.windows_emergency_stop import WindowsEmergencyStopBridge


CANONICAL_STOP_FILENAME = "execution-stop.jsonl"


def test_windows_emergency_stop_mutates_provider_admission_stop_authority(
    tmp_path,
) -> None:
    """Windows STOP and provider admission must resolve one durable authority."""
    authority = ExecutionStopAuthority(tmp_path / CANONICAL_STOP_FILENAME)
    stopped = authority.initialize_stopped(
        operator_id="owner",
        reason="safe initialization",
        command_id="windows-stop-path-init",
    )
    authority.arm(
        operator_id="owner",
        reason="supervised execution explicitly armed",
        confirmation_id="windows-stop-path-confirmation",
        expected_revision=stopped.revision,
        command_id="windows-stop-path-arm",
    )
    assert authority.current().mode is ExecutionAuthorityMode.ARMED

    result = WindowsEmergencyStopBridge.for_workspace(tmp_path).activate()

    assert result.stopped is True
    assert authority.current().mode is ExecutionAuthorityMode.STOPPED
    assert not (tmp_path / "execution-stop-authority.jsonl").exists()
