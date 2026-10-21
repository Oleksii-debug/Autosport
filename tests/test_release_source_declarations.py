"""Plan 6 §5Б: exact source declarations are published without false SBOM/license claims."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport.release_package import build_windows_package, verify_windows_package
from scripts import package_windows


SHA = "a" * 40


def _source_bytes(*args: str) -> bytes:
    assert args[0] == "show"
    revision, sep, path = args[1].partition(":")
    assert sep and revision == SHA
    assert path in package_windows._RELEASE_SOURCE_INPUTS
    if path == "pyproject.toml":
        return b"""[project]
dependencies = ["tk-uia==0.8.0", "APScheduler==3.11.3"]
[project.optional-dependencies]
build = ["pyinstaller==6.22.3"]
"""
    return ("exact tracked bytes for " + path).encode("utf-8")


def _source_manifest() -> dict[str, object]:
    with patch.object(package_windows, "_exact_git_bytes", side_effect=lambda _, *args: _source_bytes(*args)):
        return package_windows._source_declaration_manifest(Path("."), SHA)


def _fixture(root: Path, manifest: dict | None) -> Path:
    exe = root / "Autosport.exe"
    start = root / "WINDOWS_START_HERE.txt"
    example = root / "example"
    example.mkdir()
    (example / "market.jsonl").write_text("{}\\n", encoding="utf-8")
    exe.write_bytes(b"fake-executable")
    start.write_text("start\\n", encoding="utf-8")
    common = {
        "status": "PASS",
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    files = []
    for name in ("diagnostic", "accessibility", "keyboard", "restart"):
        file = root / (name + ".json")
        data = dict(common)
        if name == "restart":
            data.update({
                "session_restart_status": "PASS",
                "transaction_recovery_status": "PASS",
                "recovery_disposition": "aborted_uncommitted",
                "process_kill_relaunch_status": "PASS",
                "process_kill_stage_pid": 101,
                "process_kill_return_code": -15,
                "process_recovery_pid": 202,
                "process_recovery_run_id": "fixture-recovery-run",
                "process_recovery_disposition": "committed",
                "process_recovery_registry_status": "completed",
                "process_recovery_manifest_phase": "completed",
                "process_recovery_base_paper_book_sha256": "1" * 64,
                "process_recovery_base_decision_ledger_sha256": "2" * 64,
                "process_recovery_new_paper_book_sha256": "3" * 64,
                "process_recovery_new_decision_ledger_sha256": "4" * 64,
            })
        file.write_text(json.dumps(data), encoding="utf-8")
        files.append(file)
    archive = root / "release.zip"
    build_windows_package(exe, start, example, *files, archive, SHA, source_manifest=manifest)
    return archive


def test_release_manifest_uses_only_pinned_exact_git_inputs() -> None:
    manifest = _source_manifest()
    assert manifest["schema_version"] == 1
    assert manifest["source_sha"] == SHA
    assert manifest["evidence_class"] == "SOURCE_DECLARATIONS_ONLY"
    assert manifest["declared_runtime_dependencies"] == [
        "APScheduler==3.11.3", "tk-uia==0.8.0"
    ]
    assert manifest["declared_build_dependencies"] == ["pyinstaller==6.22.3"]
    assert set(manifest["source_files_sha256"]) == set(package_windows._RELEASE_SOURCE_INPUTS)
    assert manifest["source_files_sha256"]["pyproject.toml"] == hashlib.sha256(
        _source_bytes("show", f"{SHA}:pyproject.toml")
    ).hexdigest()


def test_release_manifest_rejects_unpinned_runtime_declaration() -> None:
    def bad_blob(_, *args):
        body = _source_bytes(*args)
        if args[-1].endswith(":pyproject.toml"):
            return body.replace(b"tk-uia==0.8.0", b"tk-uia>=0.8")
        return body
    with patch.object(package_windows, "_exact_git_bytes", side_effect=bad_blob):
        with pytest.raises(ValueError, match="not exactly pinned"):
            package_windows._source_declaration_manifest(Path("."), SHA)


def test_release_zip_requires_present_manifest_in_canonical_cli_mode() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = _fixture(Path(directory), None)
        assert verify_windows_package(path, expected_source_sha=SHA)["status"] == "PASS"
        with pytest.raises(ValueError, match="missing SOURCE_MANIFEST"):
            verify_windows_package(path, expected_source_sha=SHA, require_source_manifest=True)


def test_release_manifest_round_trip_and_stable_archive() -> None:
    manifest = _source_manifest()
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        first_zip = _fixture(Path(first), manifest)
        second_zip = _fixture(Path(second), manifest)
        assert first_zip.read_bytes() == second_zip.read_bytes()
        assert verify_windows_package(
            first_zip, expected_source_sha=SHA, require_source_manifest=True
        )["status"] == "PASS"


def test_release_manifest_rejects_false_identity_without_writing_zip() -> None:
    manifest = _source_manifest()
    manifest["source_sha"] = "b" * 40
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        # The validation precedes all file copying and cannot leave a release.
        with pytest.raises(ValueError, match="source SHA"):
            _fixture(root, manifest)
        assert not (root / "release.zip").exists()


def test_release_manifest_rejects_unpinned_or_forged_data_before_zip() -> None:
    for field, value in (
        ("schema_version", True),
        ("declared_build_dependencies", ["pyinstaller>=6.22.3"]),
        ("declared_runtime_dependencies", ["tk-uia==0.8.0", "tk-uia==0.8.0"]),
        ("evidence_class", "INSTALLED_SBOM_VERIFIED"),
    ):
        manifest = _source_manifest()
        manifest[field] = value
        with tempfile.TemporaryDirectory() as directory:
            with pytest.raises(ValueError):
                _fixture(Path(directory), manifest)


@pytest.mark.parametrize(
    "alias",
    ["tk_uia==0.8.0", "TK.UIA==0.8.0", "Tk_uia==0.8.0"],
)
def test_source_manifest_rejects_same_distribution_under_pep503_alias(alias: str) -> None:
    manifest = _source_manifest()
    dependencies = list(manifest["declared_runtime_dependencies"])
    dependencies.append(alias)
    manifest["declared_runtime_dependencies"] = sorted(dependencies)
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(ValueError, match="normalized dependency aliases"):
            _fixture(Path(directory), manifest)
        assert not (Path(directory) / "release.zip").exists()


def test_source_manifest_rejects_cross_scope_conflicting_versions() -> None:
    manifest = _source_manifest()
    manifest["declared_build_dependencies"] = sorted(
        [*manifest["declared_build_dependencies"], "tk_uia==0.9.0"]
    )
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(ValueError, match="conflicting dependency versions"):
            _fixture(Path(directory), manifest)
        assert not (Path(directory) / "release.zip").exists()


def test_source_manifest_allows_same_version_cross_scope_declaration() -> None:
    manifest = _source_manifest()
    manifest["declared_build_dependencies"] = sorted(
        [*manifest["declared_build_dependencies"], "tk_uia==0.8.0"]
    )
    with tempfile.TemporaryDirectory() as directory:
        archive = _fixture(Path(directory), manifest)
        assert verify_windows_package(
            archive, expected_source_sha=SHA, require_source_manifest=True
        )["status"] == "PASS"
