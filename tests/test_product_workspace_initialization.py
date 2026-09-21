from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

from autosport.product_workspace_initialization import (
    PRODUCT_WORKSPACE_BINDING_SCHEMA_VERSION,
    ProductWorkspaceInitializationError,
    initialize_product_workspace,
)


@pytest.fixture(autouse=True)
def _isolate_machine_identity_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep current and successor machine-binding roots inside each test sandbox."""

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "os-application-state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.delenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", raising=False)


def _worker(workspace: str, authority_root: str, start, results) -> None:
    try:
        if not start.wait(20):
            raise RuntimeError("timed out waiting for concurrent first-run start")
        initialized = initialize_product_workspace(
            Path(workspace),
            authority_root=Path(authority_root),
        )
        results.put(("ok", initialized.workspace_instance_id))
    except ProductWorkspaceInitializationError as exc:
        results.put(("expected-error", type(exc).__name__, str(exc)))
    except BaseException as exc:
        results.put(("unexpected-error", type(exc).__name__, str(exc)))
        raise


def _join_cleanly(process: multiprocessing.Process) -> None:
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(10)
        pytest.fail("spawned first-run initializer did not exit")
    assert process.exitcode == 0


def _rewrite_self_consistent_workspace_id(path: Path, workspace_instance_id: str) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["workspace_instance_id"] = workspace_instance_id
    unhashed = dict(raw)
    unhashed.pop("binding_sha256")
    encoded = json.dumps(
        unhashed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    raw["binding_sha256"] = hashlib.sha256(encoded).hexdigest()
    path.write_text(
        json.dumps(
            raw,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_first_run_publishes_one_versioned_workspace_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "дані Autosport з пробілами"
    authority_root = tmp_path / "machine-state"

    initialized = initialize_product_workspace(
        workspace,
        authority_root=authority_root,
    )

    assert initialized.workspace == workspace
    assert len(initialized.workspace_instance_id) == 32
    assert initialized.binding_schema_version == PRODUCT_WORKSPACE_BINDING_SCHEMA_VERSION
    assert initialized.workspace_marker_path.is_file()
    assert initialized.path_binding_path.is_file()
    assert initialized.workspace_marker_path.is_relative_to(workspace)
    assert initialized.path_binding_path.is_relative_to(authority_root)


def test_restart_reopens_exact_same_workspace_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "machine-state"

    first = initialize_product_workspace(workspace, authority_root=authority_root)
    marker_before = first.workspace_marker_path.read_bytes()
    path_binding_before = first.path_binding_path.read_bytes()

    second = initialize_product_workspace(workspace, authority_root=authority_root)

    assert second.workspace_instance_id == first.workspace_instance_id
    assert second.workspace_marker_path.read_bytes() == marker_before
    assert second.path_binding_path.read_bytes() == path_binding_before


def test_interrupted_path_binding_prefix_resumes_same_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "machine-state"
    first = initialize_product_workspace(workspace, authority_root=authority_root)

    first.workspace_marker_path.unlink()
    assert first.path_binding_path.is_file()
    assert not first.workspace_marker_path.exists()

    recovered = initialize_product_workspace(workspace, authority_root=authority_root)

    assert recovered.workspace_instance_id == first.workspace_instance_id
    assert recovered.workspace_marker_path.is_file()
    assert recovered.path_binding_path.read_bytes() == (
        first.path_binding_path.read_bytes()
    )


def test_two_concurrent_first_runs_converge_to_one_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "machine-state"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(
            target=_worker,
            args=(str(workspace), str(authority_root), start, results),
        )
        for _ in range(2)
    ]

    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        _join_cleanly(worker)

    messages = [results.get(timeout=5), results.get(timeout=5)]
    assert all(message[0] in {"ok", "expected-error"} for message in messages)
    successful_ids = [message[1] for message in messages if message[0] == "ok"]
    assert successful_ids
    assert len(set(successful_ids)) == 1

    reopened = initialize_product_workspace(workspace, authority_root=authority_root)
    assert reopened.workspace_instance_id == successful_ids[0]


def test_self_consistent_workspace_vs_machine_identity_conflict_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "machine-state"
    initialized = initialize_product_workspace(workspace, authority_root=authority_root)
    path_binding_before = initialized.path_binding_path.read_bytes()

    _rewrite_self_consistent_workspace_id(
        initialized.workspace_marker_path,
        "conflicting-workspace-instance",
    )
    workspace_marker_after_tamper = initialized.workspace_marker_path.read_bytes()

    with pytest.raises(
        ProductWorkspaceInitializationError,
        match="durable product workspace identity evidence conflicts",
    ):
        initialize_product_workspace(workspace, authority_root=authority_root)

    assert initialized.workspace_marker_path.read_bytes() == workspace_marker_after_tamper
    assert initialized.path_binding_path.read_bytes() == path_binding_before


def test_corrupt_binding_digest_fails_without_reinitialization(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "machine-state"
    initialized = initialize_product_workspace(workspace, authority_root=authority_root)

    raw = json.loads(initialized.workspace_marker_path.read_text(encoding="utf-8"))
    raw["binding_sha256"] = "0" * 64
    initialized.workspace_marker_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    corrupt_bytes = initialized.workspace_marker_path.read_bytes()
    path_binding_before = initialized.path_binding_path.read_bytes()

    with pytest.raises(
        ProductWorkspaceInitializationError,
        match="cannot verify durable product workspace identity",
    ):
        initialize_product_workspace(workspace, authority_root=authority_root)

    assert initialized.workspace_marker_path.read_bytes() == corrupt_bytes
    assert initialized.path_binding_path.read_bytes() == path_binding_before


def test_relative_workspace_is_rejected_before_publication(tmp_path: Path) -> None:
    authority_root = tmp_path / "machine-state"

    with pytest.raises(
        ProductWorkspaceInitializationError,
        match="product workspace must be an absolute path",
    ):
        initialize_product_workspace(
            Path("relative-workspace"),
            authority_root=authority_root,
        )

    assert not authority_root.exists()


def test_authority_root_inside_workspace_is_rejected_without_identity_publication(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"

    with pytest.raises(
        ProductWorkspaceInitializationError,
        match="cannot be configured safely",
    ):
        initialize_product_workspace(
            workspace,
            authority_root=workspace / "machine-state",
        )

    assert not (workspace / ".autosport").exists()


def test_alias_to_same_workspace_reuses_identity_when_supported(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "machine-state"
    first = initialize_product_workspace(workspace, authority_root=authority_root)
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(workspace, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this runner")

    via_alias = initialize_product_workspace(alias, authority_root=authority_root)

    assert via_alias.workspace_instance_id == first.workspace_instance_id
    assert via_alias.workspace_marker_path.read_bytes() == first.workspace_marker_path.read_bytes()
    assert via_alias.path_binding_path.is_file()
    assert via_alias.path_binding_path != first.path_binding_path
