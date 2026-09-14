from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

from autosport.evidence_export import export_evidence_manifest, main


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


def test_export_is_deterministic_metadata_only_and_secret_safe(tmp_path: Path) -> None:
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

    first_output = tmp_path / "exports" / "first.json"
    second_output = tmp_path / "exports" / "second.json"
    first = export_evidence_manifest(workspace, first_output)
    second = export_evidence_manifest(workspace, second_output)

    assert first == second
    assert json.loads(first_output.read_text(encoding="utf-8")) == first
    assert json.loads(second_output.read_text(encoding="utf-8")) == second
    assert first["manifest_sha256"] == _manifest_hash(first)
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


def test_cli_reports_fail_closed_and_success_without_traceback(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "missing"
    failed_output = tmp_path / "failed.json"
    assert main([str(missing), "--output", str(failed_output)]) == 3
    failure_text = capsys.readouterr().out
    assert "evidence_export=FAIL_CLOSED" in failure_text
    assert not failed_output.exists()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_text('{"balance":"10000"}\n', encoding="utf-8")
    output = tmp_path / "ok.json"
    assert main([str(workspace), "--output", str(output)]) == 0
    success_text = capsys.readouterr().out
    assert "evidence_export=PASS" in success_text
    assert "file_contents_included=false" in success_text
    assert "environment_or_credential_values_included=false" in success_text
    assert "real_money_execution=false" in success_text
    assert output.exists()
