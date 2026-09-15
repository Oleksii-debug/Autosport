from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import autosport.evidence_export as evidence_export
import autosport.evidence_snapshot_lock as evidence_snapshot_lock


pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows V1 evidence snapshot object/handoff boundary",
)


def _install_pre_watch_root_replacement_attack(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    moved_workspace: Path,
) -> list[bool]:
    real_acquire = evidence_snapshot_lock._WorkspaceEconomicLock.acquire
    attempted = [False]

    def acquire_after_replacement_attempt(self: object) -> None:
        if not attempted[0]:
            attempted[0] = True
            # The evidence wrapper must already own a no-DELETE-share handle to the
            # caller-selected directory object before canonical lock acquisition.
            # Moving that object away would reopen the old resolved-path/decoy race.
            with pytest.raises(OSError):
                os.replace(workspace, moved_workspace)
        real_acquire(self)  # type: ignore[arg-type]

    monkeypatch.setattr(
        evidence_snapshot_lock._WorkspaceEconomicLock,
        "acquire",
        acquire_after_replacement_attempt,
    )
    return attempted


def _install_retained_source_handoff_attack(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    replacement: Path,
) -> list[bool]:
    real_linearize = evidence_snapshot_lock.WorkspaceEconomicLock.linearize
    attempted = [False]

    def attack_then_linearize(
        self: evidence_snapshot_lock.WorkspaceEconomicLock,
    ) -> None:
        # __exit__ invokes linearize() defensively after the explicit evidence call.
        # Attack only the first call: that is the boundary that must still retain all
        # canonical source handles and their WRITE/DELETE exclusion.
        if getattr(self, "_linearized", False):
            real_linearize(self)
            return

        attempted[0] = True
        original = source.read_bytes()
        replacement_bytes = replacement.read_bytes()

        # This models the same-length in-place write called out by the source review.
        # It must be denied by the still-open retained source handle, not detected
        # later via cached LAST_WRITE notification timing.
        with pytest.raises(OSError):
            source.write_bytes(b"x" * len(original))
        assert source.read_bytes() == original

        with pytest.raises(OSError):
            source.unlink()
        assert source.read_bytes() == original

        with pytest.raises(OSError):
            os.replace(replacement, source)
        assert source.read_bytes() == original
        assert replacement.read_bytes() == replacement_bytes

        real_linearize(self)

    monkeypatch.setattr(
        evidence_snapshot_lock.WorkspaceEconomicLock,
        "linearize",
        attack_then_linearize,
    )
    return attempted


def test_windows_export_pins_workspace_object_before_canonical_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paper = b"authoritative-paper"
    (workspace / "paper_book.json").write_bytes(paper)
    moved_workspace = tmp_path / "moved-workspace"
    output = tmp_path / "manifest.json"

    attempted = _install_pre_watch_root_replacement_attack(
        monkeypatch,
        workspace,
        moved_workspace,
    )

    report = evidence_export.export_evidence_manifest(workspace, output)

    assert attempted == [True]
    assert workspace.is_dir()
    assert not moved_workspace.exists()
    assert report["files"] == [
        {
            "path": "paper_book.json",
            "size_bytes": len(paper),
            "sha256": hashlib.sha256(paper).hexdigest(),
        }
    ]
    assert output.exists()


def test_windows_verify_cannot_rebind_workspace_to_matching_decoy_before_watch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authoritative = tmp_path / "authoritative"
    decoy = tmp_path / "decoy"
    authoritative.mkdir()
    decoy.mkdir()
    (authoritative / "paper_book.json").write_bytes(b"authoritative-paper")
    (decoy / "paper_book.json").write_bytes(b"decoy-paper")

    manifest = tmp_path / "manifest.json"
    evidence_export.export_evidence_manifest(decoy, manifest)

    moved_workspace = tmp_path / "moved-authoritative"
    attempted = _install_pre_watch_root_replacement_attack(
        monkeypatch,
        authoritative,
        moved_workspace,
    )

    with pytest.raises(
        ValueError,
        match="workspace evidence does not match manifest: paper_book.json",
    ):
        evidence_export.verify_evidence_manifest(manifest, authoritative)

    assert attempted == [True]
    assert authoritative.is_dir()
    assert not moved_workspace.exists()


def test_windows_export_retains_source_exclusion_through_positive_linearization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "paper_book.json"
    source.write_bytes(b"paper-state-original")
    replacement = workspace / "replacement.tmp"
    replacement.write_bytes(b"replacement-must-survive")
    output = tmp_path / "manifest.json"

    attempted = _install_retained_source_handoff_attack(
        monkeypatch,
        source,
        replacement,
    )

    report = evidence_export.export_evidence_manifest(workspace, output)

    assert attempted == [True]
    assert report["file_count"] == 1
    assert source.read_bytes() == b"paper-state-original"
    assert replacement.read_bytes() == b"replacement-must-survive"
    assert output.exists()
    assert not list(workspace.glob("*.asv"))


def test_windows_verify_retains_source_exclusion_through_positive_linearization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "paper_book.json"
    source.write_bytes(b"paper-state-original")
    replacement = workspace / "replacement.tmp"
    replacement.write_bytes(b"replacement-must-survive")
    manifest = tmp_path / "manifest.json"
    evidence_export.export_evidence_manifest(workspace, manifest)

    attempted = _install_retained_source_handoff_attack(
        monkeypatch,
        source,
        replacement,
    )

    report = evidence_export.verify_evidence_manifest(manifest, workspace)

    assert attempted == [True]
    assert report["file_count"] == 1
    assert source.read_bytes() == b"paper-state-original"
    assert replacement.read_bytes() == b"replacement-must-survive"
    assert not list(workspace.glob("*.asv"))
