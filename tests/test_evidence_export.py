from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

import autosport.evidence_export as evidence_export
from autosport.evidence_export import (
    export_evidence_manifest,
    main,
    verify_evidence_manifest,
    verify_main,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_hash(report: dict[str, object]) -> str:
    payload = dict(report)
    payload.pop("manifest_sha256")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def test_export_is_deterministic_metadata_only_secret_safe_and_verifiable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace with ünicode"
    workspace.mkdir()
    run_name = f"run-{uuid.uuid4()}.json"
    canonical = {
        "decisions.jsonl": b'{"decision":"paper-only"}\n',
        "paper_book.json": b'{"balance":"10000"}\n',
        "run_registry.json": b'{"schema_version":1,"runs":{}}\n',
        "source_health.json": b'{"schema_version":1,"sources":{}}\n',
        run_name: b'{"schema_version":2,"real_money_execution":false}\n',
    }
    for name, content in canonical.items():
        (workspace / name).write_bytes(content)

    # These bytes may contain credentials, raw/licensed data or arbitrary user data.
    # A V1 evidence manifest must never include their names, contents or digests.
    (workspace / "market.db").write_bytes(b"raw market bytes")
    (workspace / ".env").write_text("API_KEY=do-not-export\n", encoding="utf-8")
    (workspace / "token.json").write_text('{"token":"secret"}\n', encoding="utf-8")
    (workspace / "run-not-a-canonical-uuid.json").write_text("secret\n", encoding="utf-8")
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "paper_book.json").write_text("not-root-evidence\n", encoding="utf-8")

    first_output = tmp_path / "first.json"
    second_output = tmp_path / "second.json"
    first = export_evidence_manifest(workspace, first_output)
    second = export_evidence_manifest(workspace, second_output)

    assert first == second
    assert json.loads(first_output.read_text(encoding="utf-8")) == first
    assert json.loads(second_output.read_text(encoding="utf-8")) == second
    assert verify_evidence_manifest(first_output, workspace) == first
    assert first["manifest_sha256"] == _manifest_hash(first)
    assert first["expected_fixed_evidence_paths"] == [
        "decisions.jsonl",
        "paper_book.json",
        "run_registry.json",
        "source_health.json",
    ]
    assert first["missing_fixed_evidence_paths"] == []
    assert first["fixed_evidence_set_complete"] is True
    assert first["run_summary_count"] == 1
    assert first["file_contents_included"] is False
    assert first["market_database_included"] is False
    assert first["raw_historical_or_provider_bytes_included"] is False
    assert first["environment_or_credential_values_included"] is False
    assert first["arbitrary_workspace_files_included"] is False
    assert first["real_money_execution"] is False

    expected_names = sorted(canonical)
    assert [item["path"] for item in first["files"]] == expected_names
    assert first["file_count"] == len(expected_names)
    for item in first["files"]:
        source = workspace / item["path"]
        assert item["size_bytes"] == source.stat().st_size
        assert item["sha256"] == _sha256(source)

    exported_text = first_output.read_text(encoding="utf-8")
    for forbidden in ("market.db", ".env", "token.json", "API_KEY", "do-not-export", "secret"):
        assert forbidden not in exported_text


def test_partial_fixed_evidence_is_truthfully_labeled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_text('{"balance":"100"}\n', encoding="utf-8")

    report = export_evidence_manifest(workspace, tmp_path / "manifest.json")

    assert report["fixed_evidence_set_complete"] is False
    assert report["missing_fixed_evidence_paths"] == [
        "decisions.jsonl",
        "run_registry.json",
        "source_health.json",
    ]
    assert report["run_summary_count"] == 0


def test_export_rejects_empty_workspace_without_creating_lock_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "empty"
    workspace.mkdir()
    output = tmp_path / "manifest.json"

    with pytest.raises(ValueError, match="no canonical exportable evidence"):
        export_evidence_manifest(workspace, output)

    assert not output.exists()
    assert not (workspace / ".economic-run.lock").exists()


def test_export_rejects_output_that_would_overwrite_canonical_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paper = workspace / "paper_book.json"
    original = b'{"balance":"100"}\n'
    paper.write_bytes(original)

    with pytest.raises(ValueError, match="must not overwrite canonical workspace evidence"):
        export_evidence_manifest(workspace, paper)

    assert paper.read_bytes() == original


def test_export_rejects_non_regular_canonical_entry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").mkdir()
    output = tmp_path / "manifest.json"

    with pytest.raises(ValueError, match="not a regular file"):
        export_evidence_manifest(workspace, output)

    assert not output.exists()


def test_export_rejects_canonical_symlink_instead_of_hashing_external_secret(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("do-not-hash-me\n", encoding="utf-8")
    link = workspace / "paper_book.json"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable in this environment")

    output = tmp_path / "manifest.json"
    with pytest.raises(ValueError, match="not a regular file"):
        export_evidence_manifest(workspace, output)

    assert not output.exists()


def test_export_rejects_in_place_mutation_during_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paper = workspace / "paper_book.json"
    paper.write_bytes(b"A" * 128)
    output = tmp_path / "manifest.json"
    real_sha256 = hashlib.sha256
    mutated = False

    class MutatingDigest:
        def __init__(self) -> None:
            self.inner = real_sha256()

        def update(self, chunk: bytes) -> None:
            nonlocal mutated
            if not mutated:
                mutated = True
                # Truncate/rewrite the same pathname in place: inode identity can
                # remain unchanged, so size/mtime/ctime stability must reject it.
                paper.write_bytes(b"B" * 257)
            self.inner.update(chunk)

        def hexdigest(self) -> str:
            return self.inner.hexdigest()

    monkeypatch.setattr(evidence_export.hashlib, "sha256", MutatingDigest)

    with pytest.raises(ValueError, match="mutated during snapshot"):
        export_evidence_manifest(workspace, output)

    assert mutated is True
    assert not output.exists()


def test_manifest_publication_occurs_after_snapshot_lock_is_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_text('{"balance":"100"}\n', encoding="utf-8")
    output = tmp_path / "manifest.json"
    lock_held = False
    publication_observed = False

    class TrackingLock:
        def __init__(self, path: Path) -> None:
            assert Path(path) == workspace

        def __enter__(self):
            nonlocal lock_held
            assert lock_held is False
            lock_held = True
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            nonlocal lock_held
            lock_held = False

    real_writer = evidence_export.atomic_write_json

    def observing_writer(path: Path, payload: dict[str, object]) -> None:
        nonlocal publication_observed
        assert lock_held is False
        publication_observed = True
        real_writer(path, payload)

    monkeypatch.setattr(evidence_export, "WorkspaceEconomicLock", TrackingLock)
    monkeypatch.setattr(evidence_export, "atomic_write_json", observing_writer)

    report = export_evidence_manifest(workspace, output)

    assert publication_observed is True
    assert report["file_count"] == 1
    assert output.exists()


def test_verify_rejects_tampered_manifest_digest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_bytes(b"paper-state")
    manifest = tmp_path / "manifest.json"
    report = export_evidence_manifest(workspace, manifest)
    tampered = json.loads(json.dumps(report))
    tampered["files"][0]["sha256"] = "0" * 64
    _write_manifest(manifest, tampered)

    with pytest.raises(ValueError, match="manifest_sha256 does not match payload"):
        verify_evidence_manifest(manifest, workspace)


def test_verify_rejects_changed_workspace_even_if_manifest_is_internally_valid(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paper = workspace / "paper_book.json"
    paper.write_bytes(b"paper-state-v1")
    manifest = tmp_path / "manifest.json"
    export_evidence_manifest(workspace, manifest)

    paper.write_bytes(b"paper-state-v2")

    with pytest.raises(ValueError, match="workspace evidence does not match manifest"):
        verify_evidence_manifest(manifest, workspace)


def test_verify_rejects_changed_canonical_file_set(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_bytes(b"paper-state")
    manifest = tmp_path / "manifest.json"
    export_evidence_manifest(workspace, manifest)

    new_run = workspace / f"run-{uuid.uuid4()}.json"
    new_run.write_text('{"real_money_execution":false}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="canonical evidence set does not match manifest"):
        verify_evidence_manifest(manifest, workspace)


def test_verify_rejects_noncanonical_path_even_with_recomputed_manifest_hash(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_bytes(b"paper-state")
    manifest = tmp_path / "manifest.json"
    report = export_evidence_manifest(workspace, manifest)
    forged = json.loads(json.dumps(report))
    forged["files"].append(
        {
            "path": "token.json",
            "size_bytes": 6,
            "sha256": hashlib.sha256(b"secret").hexdigest(),
        }
    )
    forged["file_count"] = 2
    forged["manifest_sha256"] = _manifest_hash(forged)
    _write_manifest(manifest, forged)

    with pytest.raises(ValueError, match="noncanonical evidence path"):
        verify_evidence_manifest(manifest, workspace)


def test_verify_rejects_duplicate_json_keys_before_schema_validation(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="duplicate JSON object keys"):
        verify_evidence_manifest(manifest, workspace)


def test_cli_reports_fail_closed_and_success_without_traceback(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing"
    failed_output = tmp_path / "failed.json"
    assert main([str(missing), "--output", str(failed_output)]) == 3
    failure_text = capsys.readouterr().out
    assert "evidence_export=FAIL_CLOSED" in failure_text
    assert not failed_output.exists()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paper = workspace / "paper_book.json"
    paper.write_text('{"balance":"10000"}\n', encoding="utf-8")
    output = tmp_path / "ok.json"
    assert main([str(workspace), "--output", str(output)]) == 0
    success_text = capsys.readouterr().out
    assert "evidence_export=PASS" in success_text
    assert "fixed_evidence_set_complete=false" in success_text
    assert "file_contents_included=false" in success_text
    assert "environment_or_credential_values_included=false" in success_text
    assert "real_money_execution=false" in success_text
    assert output.exists()

    assert verify_main([str(output), "--workspace", str(workspace)]) == 0
    verify_success = capsys.readouterr().out
    assert "evidence_verify=PASS" in verify_success
    assert "workspace_match=true" in verify_success
    assert "real_money_execution=false" in verify_success

    paper.write_text('{"balance":"99999"}\n', encoding="utf-8")
    assert verify_main([str(output), "--workspace", str(workspace)]) == 3
    verify_failure = capsys.readouterr().out
    assert "evidence_verify=FAIL_CLOSED" in verify_failure
