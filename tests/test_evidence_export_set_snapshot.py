from __future__ import annotations

import uuid
from pathlib import Path

import pytest

import autosport.evidence_export as evidence_export


def test_export_rejects_canonical_file_set_change_during_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_bytes(b"paper-state")
    output = tmp_path / "manifest.json"
    injected = workspace / f"run-{uuid.uuid4()}.json"
    real_hash = evidence_export._open_and_hash_regular_file
    mutation_injected = False

    def mutating_hash(path: Path) -> tuple[int, str]:
        nonlocal mutation_injected
        result = real_hash(path)
        if not mutation_injected:
            mutation_injected = True
            injected.write_text(
                '{"real_money_execution":false}\n',
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(
        evidence_export,
        "_open_and_hash_regular_file",
        mutating_hash,
    )

    with pytest.raises(
        ValueError,
        match="canonical evidence set changed during snapshot",
    ):
        evidence_export.export_evidence_manifest(workspace, output)

    assert mutation_injected is True
    assert injected.exists()
    assert not output.exists()


def test_export_rejects_already_hashed_member_mutation_during_later_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paper = workspace / "paper_book.json"
    paper.write_bytes(b"paper-state-v1")
    (workspace / "run_registry.json").write_bytes(b"registry-state")
    output = tmp_path / "manifest.json"
    real_hash = evidence_export._open_and_hash_regular_file
    hash_count = 0
    mutation_injected = False

    def mutating_hash(path: Path) -> tuple[int, str]:
        nonlocal hash_count, mutation_injected
        result = real_hash(path)
        hash_count += 1
        if hash_count == 2:
            paper.write_bytes(b"paper-state-v2-longer")
            mutation_injected = True
        return result

    monkeypatch.setattr(
        evidence_export,
        "_open_and_hash_regular_file",
        mutating_hash,
    )

    with pytest.raises(
        ValueError,
        match="canonical evidence file mutated during snapshot: paper_book.json",
    ):
        evidence_export.export_evidence_manifest(workspace, output)

    assert mutation_injected is True
    assert hash_count == 2
    assert not output.exists()
