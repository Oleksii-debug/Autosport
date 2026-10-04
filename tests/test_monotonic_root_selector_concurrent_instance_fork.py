from __future__ import annotations

import hashlib
from pathlib import Path

from autosport.monotonic_authority_root_binding import (
    AuthorityRootSelectionBinding,
    AuthorityRootSelectionConflictError,
    _durable_exclusive_json_create,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_same_instance_concurrent_path_registration_cannot_validate_two_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Reproduce the first-registration race without scheduler timing.

    Two callers for the same immutable workspace instance can validate distinct
    lexical paths while the stable selector store is still empty.  The selector
    authority must fail closed even if that race outcome is materialized directly.
    """

    monkeypatch.setenv(
        "LOCALAPPDATA",
        str(tmp_path / "stable-application-state"),
    )
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    root_a = tmp_path / "authority-root-a"
    root_b = tmp_path / "authority-root-b"
    instance_id = "shared-immutable-workspace-instance"

    binding_a = AuthorityRootSelectionBinding.resolve(
        workspace=workspace_a,
        workspace_instance_id=instance_id,
        authority_root=root_a,
    )
    binding_b = AuthorityRootSelectionBinding.resolve(
        workspace=workspace_b,
        workspace_instance_id=instance_id,
        authority_root=root_b,
    )

    assert binding_a.validate_existing() is False
    assert binding_b.validate_existing() is False
    assert binding_a.binding_path != binding_b.binding_path

    _durable_exclusive_json_create(
        binding_a.binding_path,
        binding_a._selection_payload(),
        lineage_boundary=binding_a.store_root,
    )
    _durable_exclusive_json_create(
        binding_b.binding_path,
        binding_b._selection_payload(),
        lineage_boundary=binding_b.store_root,
    )

    assert binding_a.binding_path.is_file()
    assert binding_b.binding_path.is_file()

    validation_results: list[str] = []
    for binding in (binding_a, binding_b):
        try:
            binding.validate_existing()
        except AuthorityRootSelectionConflictError:
            validation_results.append("CONFLICT")
        else:
            validation_results.append("VALID")

    assert validation_results != ["VALID", "VALID"]

    activation_successes = 0
    for binding, namespace in (
        (binding_a, _sha("namespace-a")),
        (binding_b, _sha("namespace-b")),
    ):
        try:
            binding.ensure_namespace_activated(namespace)
        except AuthorityRootSelectionConflictError:
            continue
        else:
            activation_successes += 1

    assert activation_successes <= 1
