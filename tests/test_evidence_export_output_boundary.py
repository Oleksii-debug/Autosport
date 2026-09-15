from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import autosport.evidence_export as evidence_export
from autosport.evidence_export import export_evidence_manifest


def _workspace_with_evidence(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_bytes(b'{"balance":"100"}\n')
    return workspace


@pytest.mark.parametrize(
    ("relative_path", "original"),
    [
        (Path("market.db"), b"sqlite-product-state"),
        (Path(".economic-run.lock"), b"existing-lock-metadata"),
        (Path("source_health.json.lock"), b"source-health-lock-metadata"),
        (Path("token.json"), b'{"token":"must-survive"}\n'),
        (Path("arbitrary-product-state.bin"), b"arbitrary-product-state"),
        (Path(".run-transactions") / "run-1" / "manifest.json", b"transaction-evidence"),
    ],
)
def test_export_rejects_existing_workspace_destination_without_modifying_bytes(
    tmp_path: Path,
    relative_path: Path,
    original: bytes,
) -> None:
    workspace = _workspace_with_evidence(tmp_path)
    destination = workspace / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(original)

    with pytest.raises(ValueError, match="outside the Autosport workspace"):
        export_evidence_manifest(workspace, destination)

    assert destination.read_bytes() == original
    assert (workspace / "paper_book.json").read_bytes() == b'{"balance":"100"}\n'


def test_export_rejects_new_destination_anywhere_inside_workspace(tmp_path: Path) -> None:
    workspace = _workspace_with_evidence(tmp_path)
    destination = workspace / "exports" / "manifest.json"

    with pytest.raises(ValueError, match="outside the Autosport workspace"):
        export_evidence_manifest(workspace, destination)

    assert not destination.exists()
    assert not destination.parent.exists()


def test_export_rejects_external_path_that_resolves_back_into_workspace(tmp_path: Path) -> None:
    workspace = _workspace_with_evidence(tmp_path)
    alias = tmp_path / "workspace-alias"
    try:
        os.symlink(workspace, alias, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable in this environment")

    destination = alias / "market.db"
    with pytest.raises(ValueError, match="outside the Autosport workspace"):
        export_evidence_manifest(workspace, destination)

    assert not (workspace / "market.db").exists()


def test_export_uses_resolved_external_target_without_replacing_workspace_symlink(
    tmp_path: Path,
) -> None:
    workspace = _workspace_with_evidence(tmp_path)
    external_target = tmp_path / "external-manifest.json"
    external_target.write_text("old-external-bytes\n", encoding="utf-8")
    link = workspace / "export-link.json"
    try:
        os.symlink(external_target, link)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks unavailable in this environment")

    report = export_evidence_manifest(workspace, link)

    assert link.is_symlink()
    assert link.resolve() == external_target.resolve()
    assert json.loads(external_target.read_text(encoding="utf-8")) == report
    assert not (workspace / "export-link.json").is_file() or link.is_symlink()


def test_export_rechecks_destination_after_snapshot_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace_with_evidence(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "destination-alias"
    try:
        os.symlink(outside, alias, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable in this environment")

    destination = alias / "manifest.json"
    real_hash = evidence_export._open_and_hash_regular_file
    redirected = False

    def hash_then_redirect(path: Path) -> tuple[int, str]:
        nonlocal redirected
        result = real_hash(path)
        if not redirected:
            alias.unlink()
            os.symlink(workspace, alias, target_is_directory=True)
            redirected = True
        return result

    monkeypatch.setattr(evidence_export, "_open_and_hash_regular_file", hash_then_redirect)

    with pytest.raises(ValueError, match="outside the Autosport workspace"):
        export_evidence_manifest(workspace, destination)

    assert redirected is True
    assert not (workspace / "manifest.json").exists()
    assert not (outside / "manifest.json").exists()


def test_export_binds_new_output_parent_before_atomic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-boundary parent substitution cannot redirect or produce a false PASS."""

    workspace = _workspace_with_evidence(tmp_path)
    capture = workspace / "capture"
    capture.mkdir()
    future_parent = tmp_path / "future-parent"
    moved_parent = tmp_path / "future-parent-original"
    destination = future_parent / "manifest.json"
    real_writer = evidence_export.atomic_write_json
    attack_attempted = False
    rename_blocked = False

    def redirect_parent_then_publish(path: Path, payload: dict[str, object]) -> None:
        nonlocal attack_attempted, rename_blocked
        attack_attempted = True
        try:
            future_parent.rename(moved_parent)
        except OSError:
            rename_blocked = True
        else:
            try:
                os.symlink(capture, future_parent, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                moved_parent.rename(future_parent)
                pytest.fail(f"output parent was renameable but attack symlink failed: {exc}")
        real_writer(path, payload)

    monkeypatch.setattr(evidence_export, "atomic_write_json", redirect_parent_then_publish)

    try:
        report = export_evidence_manifest(workspace, destination)
    except ValueError:
        assert attack_attempted is True
        assert rename_blocked is False
        assert not (capture / "manifest.json").exists()
        assert future_parent.is_symlink()
        assert json.loads((moved_parent / "manifest.json").read_text(encoding="utf-8"))[
            "kind"
        ] == "autosport-workspace-evidence-manifest"
    else:
        assert attack_attempted is True
        assert rename_blocked is True
        assert json.loads(destination.read_text(encoding="utf-8")) == report
        assert not moved_parent.exists()


def test_export_fails_closed_if_bound_parent_moves_and_original_path_is_recreated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identical decoy bytes at the old pathname cannot turn moved publication into PASS."""

    workspace = _workspace_with_evidence(tmp_path)
    future_parent = tmp_path / "future-parent"
    moved_parent = tmp_path / "future-parent-original"
    destination = future_parent / "manifest.json"
    real_writer = evidence_export.atomic_write_json
    attack_attempted = False
    expected_payload: dict[str, object] | None = None

    def move_recreate_and_publish(path: Path, payload: dict[str, object]) -> None:
        nonlocal attack_attempted, expected_payload
        attack_attempted = True
        expected_payload = payload
        try:
            future_parent.rename(moved_parent)
        except OSError:
            pytest.skip("platform prevents renaming the bound output parent")
        future_parent.mkdir()
        destination.write_bytes(evidence_export._manifest_file_bytes(payload))
        real_writer(path, payload)

    monkeypatch.setattr(evidence_export, "atomic_write_json", move_recreate_and_publish)

    with pytest.raises(ValueError, match="caller-visible evidence output parent changed before PASS"):
        export_evidence_manifest(workspace, destination)

    assert attack_attempted is True
    assert expected_payload is not None
    assert json.loads(destination.read_text(encoding="utf-8")) == expected_payload
    assert json.loads((moved_parent / "manifest.json").read_text(encoding="utf-8")) == expected_payload


def test_export_rejects_bound_parent_reparented_under_workspace_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound external directory cannot become a workspace write target."""

    workspace = _workspace_with_evidence(tmp_path)
    future_parent = tmp_path / "future-parent"
    captured_parent = workspace / "captured-parent"
    destination = future_parent / "manifest.json"
    real_writer = evidence_export.atomic_write_json
    attack_attempted = False
    rename_blocked = False

    def reparent_under_workspace_then_publish(
        path: Path,
        payload: dict[str, object],
    ) -> None:
        nonlocal attack_attempted, rename_blocked
        attack_attempted = True
        try:
            future_parent.rename(captured_parent)
        except OSError:
            rename_blocked = True
        real_writer(path, payload)

    monkeypatch.setattr(
        evidence_export,
        "atomic_write_json",
        reparent_under_workspace_then_publish,
    )

    try:
        report = export_evidence_manifest(workspace, destination)
    except ValueError as exc:
        assert attack_attempted is True
        assert rename_blocked is False
        assert "moved into Autosport workspace before publication" in str(exc)
        assert captured_parent.is_dir()
        assert list(captured_parent.iterdir()) == []
        assert not future_parent.exists()
    else:
        assert attack_attempted is True
        assert rename_blocked is True
        assert json.loads(destination.read_text(encoding="utf-8")) == report
        assert not captured_parent.exists()
