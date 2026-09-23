from __future__ import annotations

from pathlib import Path

import pytest

import autosport.external_validity_policy_issuance as issuance_module
from autosport.external_validity_policy_issuance import (
    ProductPolicyEvaluationIssuanceError,
    ProductPolicyEvaluationWorkspace,
)
from autosport.monotonic_workspace_authority import (
    resolve_monotonic_authority_root,
)
from autosport.monotonic_workspace_binding import WorkspaceIdentityBinding


def _bound_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, WorkspaceIdentityBinding]:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    authority_root = (tmp_path / "machine-authority").resolve()
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root),
    )
    resolved_authority_root = resolve_monotonic_authority_root(workspace)
    binding = WorkspaceIdentityBinding.resolve(
        workspace=workspace,
        authority_root=resolved_authority_root,
        requested_workspace_instance_id="workspace-alpha",
    )
    binding.ensure_bound()
    return workspace, binding


def test_exact_bound_workspace_still_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, binding = _bound_workspace(tmp_path, monkeypatch)

    authority = ProductPolicyEvaluationWorkspace.open(
        workspace,
        expected_workspace_instance_id=binding.workspace_instance_id,
    )

    assert authority.workspace == workspace
    assert authority.workspace_instance_id == binding.workspace_instance_id
    assert authority.workspace_locator_sha256 == binding.workspace_locator_sha256


def test_workspace_binding_class_rebind_fails_before_redirected_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, binding = _bound_workspace(tmp_path, monkeypatch)
    called = False

    class RedirectedBinding:
        @classmethod
        def resolve(cls, **_kwargs):
            nonlocal called
            called = True
            raise AssertionError("redirected workspace resolver executed")

    monkeypatch.setattr(
        issuance_module,
        "WorkspaceIdentityBinding",
        RedirectedBinding,
    )

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="workspace identity binding authority was rebound",
    ):
        ProductPolicyEvaluationWorkspace.open(
            workspace,
            expected_workspace_instance_id=binding.workspace_instance_id,
        )
    assert called is False


def test_workspace_authority_root_resolver_rebind_fails_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, binding = _bound_workspace(tmp_path, monkeypatch)
    called = False

    def redirected_root(_workspace):
        nonlocal called
        called = True
        raise AssertionError("redirected authority-root resolver executed")

    monkeypatch.setattr(
        issuance_module,
        "resolve_monotonic_authority_root",
        redirected_root,
    )

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="workspace authority-root resolver was rebound",
    ):
        ProductPolicyEvaluationWorkspace.open(
            workspace,
            expected_workspace_instance_id=binding.workspace_instance_id,
        )
    assert called is False


def test_workspace_authority_root_helper_rebind_fails_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, binding = _bound_workspace(tmp_path, monkeypatch)
    resolver_globals = resolve_monotonic_authority_root.__globals__
    called = False

    def redirected_absolute_path(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("redirected root helper executed")

    monkeypatch.setitem(
        resolver_globals,
        "_absolute_path",
        redirected_absolute_path,
    )

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="workspace authority-root resolver was rebound",
    ):
        ProductPolicyEvaluationWorkspace.open(
            workspace,
            expected_workspace_instance_id=binding.workspace_instance_id,
        )
    assert called is False


def test_workspace_validate_existing_rebind_cannot_mint_bound_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, binding = _bound_workspace(tmp_path, monkeypatch)
    called = False

    def forged_validate(self, *, register_moved_or_copied_path=True):
        nonlocal called
        called = True
        return True, True

    monkeypatch.setattr(
        WorkspaceIdentityBinding,
        "validate_existing",
        forged_validate,
    )

    with pytest.raises(
        ProductPolicyEvaluationIssuanceError,
        match="workspace identity binding executable authority was rebound",
    ):
        ProductPolicyEvaluationWorkspace.open(
            workspace,
            expected_workspace_instance_id=binding.workspace_instance_id,
        )
    assert called is False
