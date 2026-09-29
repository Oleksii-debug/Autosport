from __future__ import annotations

from pathlib import Path

from autosport.monotonic_workspace_authority import (
    MonotonicWorkspaceAuthority,
    RecoveryDisposition,
)


def test_repeated_pristine_recovery_does_not_initialize_authority_namespace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority_root = tmp_path / "machine-state"
    authority_root.mkdir()
    authority = MonotonicWorkspaceAuthority(
        workspace=workspace,
        workspace_instance_id="workspace-instance-1",
        domain="test-domain",
        key="test-key",
        authority_root=authority_root,
    )

    first = authority.recover(observed_state_sha256=None)
    second = authority.recover(observed_state_sha256=None)

    assert first.disposition is RecoveryDisposition.PRISTINE
    assert second.disposition is RecoveryDisposition.PRISTINE
    assert not authority.namespace_marker_path.exists()
    assert not authority.records_dir.exists()
    assert not authority.authority_root_binding_path.exists()
    assert not authority.authority_root_activation_path.exists()
