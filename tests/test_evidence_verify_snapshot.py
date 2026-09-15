from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

import autosport.evidence_export as evidence_export


def test_verify_rejects_earlier_member_mutation_while_later_member_is_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paper = workspace / "paper_book.json"
    original = b"paper-state-v1"
    paper.write_bytes(original)
    (workspace / "run_registry.json").write_bytes(b"registry-state")
    manifest = tmp_path / "manifest.json"
    evidence_export.export_evidence_manifest(workspace, manifest)

    real_hash = evidence_export._open_and_hash_regular_file
    mutation_attempted = False

    def mutating_hash(path: Path) -> tuple[int, str]:
        nonlocal mutation_attempted
        result = real_hash(path)
        if path.name == "run_registry.json" and not mutation_attempted:
            mutation_attempted = True
            paper.write_bytes(b"paper-state-v2-longer")
        return result

    monkeypatch.setattr(
        evidence_export,
        "_open_and_hash_regular_file",
        mutating_hash,
    )

    if os.name == "nt":
        with pytest.raises(OSError):
            evidence_export.verify_evidence_manifest(manifest, workspace)
        assert paper.read_bytes() == original
    else:
        with pytest.raises(
            ValueError,
            match="canonical evidence file mutated during snapshot: paper_book.json",
        ):
            evidence_export.verify_evidence_manifest(manifest, workspace)

    assert mutation_attempted is True


@pytest.mark.skipif(os.name != "nt", reason="Windows V1 share-mode exclusion contract")
def test_windows_verify_retained_handle_denies_earlier_member_same_name_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paper = workspace / "paper_book.json"
    original = b"paper-state-v1"
    paper.write_bytes(original)
    (workspace / "run_registry.json").write_bytes(b"registry-state")
    manifest = tmp_path / "manifest.json"
    evidence_export.export_evidence_manifest(workspace, manifest)
    replacement = workspace / "replacement.tmp"
    replacement.write_bytes(b"replacement-state")

    real_hash = evidence_export._open_and_hash_regular_file
    replacement_attempted = False

    def replacing_hash(path: Path) -> tuple[int, str]:
        nonlocal replacement_attempted
        result = real_hash(path)
        if path.name == "run_registry.json" and not replacement_attempted:
            replacement_attempted = True
            os.replace(replacement, paper)
        return result

    monkeypatch.setattr(
        evidence_export,
        "_open_and_hash_regular_file",
        replacing_hash,
    )

    with pytest.raises(OSError):
        evidence_export.verify_evidence_manifest(manifest, workspace)

    assert replacement_attempted is True
    assert paper.read_bytes() == original
    assert replacement.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows V1 namespace snapshot boundary")
def test_windows_verify_rejects_canonical_addition_after_membership_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_bytes(b"paper-state-v1")
    (workspace / "run_registry.json").write_bytes(b"registry-state")
    manifest = tmp_path / "manifest.json"
    evidence_export.export_evidence_manifest(workspace, manifest)
    injected = workspace / f"run-{uuid.uuid4()}.json"
    real_reproof = evidence_export._reprove_retained_source_path
    reproof_count = 0

    def adding_reproof(snapshot: tuple[Path, int, os.stat_result, os.stat_result, int, str]) -> None:
        nonlocal reproof_count
        real_reproof(snapshot)
        reproof_count += 1
        if reproof_count == 1:
            injected.write_text(
                '{"real_money_execution":false}\n',
                encoding="utf-8",
            )

    monkeypatch.setattr(
        evidence_export,
        "_reprove_retained_source_path",
        adding_reproof,
    )

    with pytest.raises(
        ValueError,
        match="workspace changed during evidence snapshot namespace boundary",
    ):
        evidence_export.verify_evidence_manifest(manifest, workspace)

    assert reproof_count == 2
    assert injected.exists()
