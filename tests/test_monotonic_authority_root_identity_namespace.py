from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import autosport.monotonic_workspace_binding as workspace_binding
from autosport.monotonic_workspace_authority import (
    MonotonicAuthorityConfigurationError,
    MonotonicWorkspaceAuthority,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _commit(authority: MonotonicWorkspaceAuthority, *, label: str) -> str:
    state = _sha(f"state:{label}")
    binding = _sha(f"binding:{label}")
    authority.prepare(
        tx_id=f"tx:{label}",
        observed_state_sha256=None,
        intended_state_sha256=state,
        semantic_binding_sha256=binding,
    )
    authority.commit(
        tx_id=f"tx:{label}",
        observed_state_sha256=state,
        semantic_binding_sha256=binding,
    )
    return state


def test_same_explicit_instance_id_in_independent_roots_does_not_globally_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine_state_home = tmp_path / "stable-machine-state"
    monkeypatch.setattr(
        workspace_binding,
        "_native_machine_state_base",
        lambda: machine_state_home,
    )

    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    root_a = tmp_path / "authority-a"
    root_b = tmp_path / "authority-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    root_a.mkdir()
    root_b.mkdir()

    authority_a = MonotonicWorkspaceAuthority(
        workspace=workspace_a,
        workspace_instance_id="shared-explicit-test-id",
        domain="test-domain",
        key="test-key",
        authority_root=root_a,
    )
    state_a = _commit(authority_a, label="a")

    # Reusing an explicit label in a different workspace and a genuinely
    # independent machine-authority root is not a claim that both roots are one
    # logical workspace. The stable moved-workspace identity index must therefore
    # be root-scoped rather than machine-global by the label alone.
    authority_b = MonotonicWorkspaceAuthority(
        workspace=workspace_b,
        workspace_instance_id="shared-explicit-test-id",
        domain="test-domain",
        key="test-key",
        authority_root=root_b,
    )
    state_b = _commit(authority_b, label="b")

    assert authority_a.workspace_binding.machine_binding_root == (
        authority_b.workspace_binding.machine_binding_root
    )
    assert authority_a.workspace_binding.machine_identity_binding_path != (
        authority_b.workspace_binding.machine_identity_binding_path
    )
    assert authority_a.recover(observed_state_sha256=state_a).committed_generation == 1
    assert authority_b.recover(observed_state_sha256=state_b).committed_generation == 1

    # Root-scoping the moved-workspace identity index must not weaken the original
    # rollback/rebind fence. Same-path selection of B's physical root is rejected
    # by A's stable path anchor before B can become candidate authority.
    with pytest.raises(
        MonotonicAuthorityConfigurationError,
        match="stable machine root binding",
    ):
        MonotonicWorkspaceAuthority(
            workspace=workspace_a,
            workspace_instance_id=None,
            domain="test-domain",
            key="test-key",
            authority_root=root_b,
        )
