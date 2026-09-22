from __future__ import annotations

import shutil
from pathlib import Path

from autosport.product_workspace_initialization import initialize_product_workspace


def test_deleted_workspace_recovers_identity_from_surviving_path_receipt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "reserved-workspace"
    authority_root = tmp_path / "machine-state"

    first = initialize_product_workspace(workspace, authority_root=authority_root)
    original_id = first.workspace_instance_id
    original_path_binding = first.path_binding_path.read_bytes()

    shutil.rmtree(workspace)
    assert not workspace.exists()
    assert first.path_binding_path.is_file()
    assert first.path_binding_path.read_bytes() == original_path_binding

    recovered = initialize_product_workspace(workspace, authority_root=authority_root)

    assert recovered.workspace_instance_id == original_id
    assert recovered.workspace_marker_path.is_file()
    assert recovered.path_binding_path == first.path_binding_path
    assert recovered.path_binding_path.read_bytes() == original_path_binding


def test_distinct_workspace_roots_under_one_authority_get_distinct_identities(
    tmp_path: Path,
) -> None:
    authority_root = tmp_path / "machine-state"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"

    initialized_a = initialize_product_workspace(
        workspace_a,
        authority_root=authority_root,
    )
    initialized_b = initialize_product_workspace(
        workspace_b,
        authority_root=authority_root,
    )

    assert initialized_a.workspace_instance_id != initialized_b.workspace_instance_id
    assert initialized_a.workspace_marker_path != initialized_b.workspace_marker_path
    assert initialized_a.path_binding_path != initialized_b.path_binding_path
    assert initialized_a.path_binding_path.is_file()
    assert initialized_b.path_binding_path.is_file()
