from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from autosport.monotonic_workspace_binding import WorkspaceIdentityBinding
from autosport.workspace_bootstrap import (
    WorkspaceBootstrapError,
    bootstrap_workspace,
)


def _authority_root(tmp_path: Path) -> Path:
    return tmp_path / "machine-authority"


def test_first_run_and_restart_reuse_one_workspace_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "user data" / "Автоспорт"
    authority_root = _authority_root(tmp_path)

    first = bootstrap_workspace(workspace, authority_root=authority_root)
    second = bootstrap_workspace(workspace, authority_root=authority_root)

    assert first.workspace_instance_id == second.workspace_instance_id
    assert first.workspace_marker_path.is_file()
    assert first.path_binding_path.is_file()
    assert first == second


def test_relative_workspace_is_rejected_before_state_creation(
    tmp_path: Path,
) -> None:
    authority_root = _authority_root(tmp_path)

    with pytest.raises(WorkspaceBootstrapError, match="absolute"):
        bootstrap_workspace(Path("relative/workspace"), authority_root=authority_root)

    assert not authority_root.exists()


def test_interrupted_after_machine_binding_reuses_same_identity_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    authority_root = _authority_root(tmp_path)
    original = WorkspaceIdentityBinding._ensure_workspace_marker
    observed: dict[str, str] = {}

    def fail_after_machine_binding(self: WorkspaceIdentityBinding) -> None:
        observed["workspace_instance_id"] = self.workspace_instance_id
        raise OSError("injected crash before local marker publication")

    monkeypatch.setattr(
        WorkspaceIdentityBinding,
        "_ensure_workspace_marker",
        fail_after_machine_binding,
    )
    with pytest.raises(WorkspaceBootstrapError):
        bootstrap_workspace(workspace, authority_root=authority_root)

    monkeypatch.setattr(
        WorkspaceIdentityBinding,
        "_ensure_workspace_marker",
        original,
    )
    recovered = bootstrap_workspace(workspace, authority_root=authority_root)

    assert recovered.workspace_instance_id == observed["workspace_instance_id"]
    assert recovered.workspace_marker_path.is_file()
    assert recovered.path_binding_path.is_file()


def test_deleted_workspace_marker_is_repaired_from_machine_path_binding(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    authority_root = _authority_root(tmp_path)
    first = bootstrap_workspace(workspace, authority_root=authority_root)

    first.workspace_marker_path.unlink()
    repaired = bootstrap_workspace(workspace, authority_root=authority_root)

    assert repaired.workspace_instance_id == first.workspace_instance_id
    assert repaired.workspace_marker_path.is_file()


def test_deleted_workspace_tree_does_not_remint_identity_at_reserved_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    authority_root = _authority_root(tmp_path)
    first = bootstrap_workspace(workspace, authority_root=authority_root)

    marker_parent = first.workspace_marker_path.parent
    first.workspace_marker_path.unlink()
    marker_parent.rmdir()
    workspace.rmdir()

    repaired = bootstrap_workspace(workspace, authority_root=authority_root)

    assert repaired.workspace_instance_id == first.workspace_instance_id
    assert repaired.workspace_marker_path.is_file()


def test_lexical_alias_of_same_root_cannot_mint_second_identity(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    authority_root = _authority_root(tmp_path)
    first = bootstrap_workspace(workspace, authority_root=authority_root)

    detour = tmp_path / "detour"
    detour.mkdir()
    alias = detour / ".." / workspace.name
    second = bootstrap_workspace(alias, authority_root=authority_root)

    assert second.workspace_instance_id == first.workspace_instance_id


def test_concurrent_first_run_converges_or_fails_explicitly_then_retries(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    authority_root = _authority_root(tmp_path)

    def initialize() -> tuple[str, str]:
        try:
            result = bootstrap_workspace(workspace, authority_root=authority_root)
        except WorkspaceBootstrapError:
            return ("ERROR", "")
        return ("OK", result.workspace_instance_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: initialize(), range(24)))

    successes = {identity for status, identity in results if status == "OK"}
    assert len(successes) == 1
    assert all(status in {"OK", "ERROR"} for status, _ in results)

    stable = bootstrap_workspace(workspace, authority_root=authority_root)
    assert stable.workspace_instance_id == next(iter(successes))


def test_corrupt_workspace_marker_fails_closed_instead_of_reminting(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    authority_root = _authority_root(tmp_path)
    first = bootstrap_workspace(workspace, authority_root=authority_root)

    payload = json.loads(first.workspace_marker_path.read_text(encoding="utf-8"))
    payload["workspace_instance_id"] = "caller-minted-replacement"
    first.workspace_marker_path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceBootstrapError):
        bootstrap_workspace(workspace, authority_root=authority_root)


def test_different_workspace_root_gets_distinct_identity(tmp_path: Path) -> None:
    authority_root = _authority_root(tmp_path)
    left = bootstrap_workspace(tmp_path / "left", authority_root=authority_root)
    right = bootstrap_workspace(tmp_path / "right", authority_root=authority_root)

    assert left.workspace_instance_id != right.workspace_instance_id
