"""Plan 6 §5Б: bounded build-host requirement graph, NOT executable SBOM."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import json

import pytest

from autosport.release_dependency_graph import (
    capture_build_host_dependency_graph,
    validate_build_host_dependency_graph,
)
from autosport.release_package import (
    _SOURCE_MANIFEST_INPUTS,
    build_windows_package,
    verify_windows_package,
)
from autosport.release_sbom import capture_direct_installed_dependencies

SHA = "a" * 40
SOURCE = {
    "schema_version": 1,
    "source_sha": SHA,
    "evidence_class": "SOURCE_DECLARATIONS_ONLY",
    "source_files_sha256": {name: "0" * 64 for name in _SOURCE_MANIFEST_INPUTS},
    "declared_runtime_dependencies": ["APScheduler==3.11.3", "tk-uia==0.8.0"],
    "declared_build_dependencies": ["pyinstaller==6.22.3"],
}

DISTS = {
    "apscheduler": ("APScheduler", "3.11.3", [
        "tzlocal>=3.0", "not-selected>=1; extra == 'unused'"
    ], "MIT"),
    "tk-uia": ("tk_uia", "0.8.0", [], None),
    "pyinstaller": ("PyInstaller", "6.22.3", ["packaging>=22.0"], "GPL-2.0-or-later"),
    "tzlocal": ("tzlocal", "5.2", [], None),
    "packaging": ("packaging", "24.0", [], "BSD-2-Clause"),
}


def _distribution(name):
    if name not in DISTS:
        raise AssertionError("unexpected attempted dependency lookup: " + name)
    literal_name, version, requires, license_expression = DISTS[name]
    return SimpleNamespace(
        version=version, requires=requires,
        metadata={"Name": literal_name, "License-Expression": license_expression},
    )


def _captured():
    with patch("autosport.release_dependency_graph.metadata.distribution", side_effect=_distribution):
        return capture_build_host_dependency_graph(SOURCE)


def _direct():
    with patch("autosport.release_sbom.metadata.distribution", side_effect=_distribution):
        return capture_direct_installed_dependencies(SOURCE)


def test_transitive_graph_is_deterministic_scope_bounded_and_honest():
    first, second = _captured(), _captured()
    assert first == second
    assert first["source_sha"] == SHA
    assert first["evidence_class"] == "BUILD_HOST_REQUIREMENT_GRAPH_ONLY"
    assert [entry["name"] for entry in first["components"]] == [
        "apscheduler", "packaging", "pyinstaller", "tk-uia", "tzlocal"
    ]
    lookup = {entry["name"]: entry for entry in first["components"]}
    assert lookup["apscheduler"]["requires"] == ["tzlocal"]
    assert lookup["pyinstaller"]["requires"] == ["packaging"]
    assert lookup["packaging"]["direct_roles"] == []
    assert lookup["tk-uia"]["direct_roles"] == ["runtime"]
    assert lookup["pyinstaller"]["direct_roles"] == ["build"]
    assert lookup["tk-uia"]["license_evidence"] == "NOT_PROVIDED"
    assert lookup["packaging"]["license_evidence"] == "DISTRIBUTION_METADATA_UNVERIFIED"
    assert "not-selected" not in lookup
    validate_build_host_dependency_graph(first, SOURCE)


@pytest.mark.parametrize("tamper", [
    lambda v: v.update(source_sha="b" * 40),
    lambda v: v.update(evidence_class="BUNDLED_EXE_SBOM_VERIFIED"),
    lambda v: v["components"].pop(0),
    lambda v: v["components"][0].update(direct_roles=[]),
    lambda v: v["components"][2].update(version="6.00"),
    lambda v: v["components"][0].update(requires=["not-recorded"]),
    lambda v: v["components"][0].update(license_evidence="LICENSE_CLEARED"),
    lambda v: v["components"][0].update(license_expression="../../secret.txt"),
    lambda v: v["components"].reverse(),
    lambda v: v["components"].append({
        "name": "irrelevant", "version": "1.0", "direct_roles": [],
        "requires": [], "license_expression": None, "license_evidence": "NOT_PROVIDED"
    }),
    lambda v: v["components"][0].update(requires=[["unhashable"]]),
])
def test_tampered_graph_fails_closed(tamper):
    modified = deepcopy(_captured())
    tamper(modified)
    with pytest.raises(ValueError):
        validate_build_host_dependency_graph(modified, SOURCE)


def test_missing_or_mismatched_transitive_distribution_fails_closed():
    def missing(name):
        if name == "tzlocal":
            from importlib.metadata import PackageNotFoundError
            raise PackageNotFoundError(name)
        return _distribution(name)
    with patch("autosport.release_dependency_graph.metadata.distribution", side_effect=missing):
        with pytest.raises(ValueError, match="transitive requirement is missing"):
            capture_build_host_dependency_graph(SOURCE)
    def wrong(name):
        if name == "packaging":
            obj = _distribution(name)
            obj.version = "1.0"
            return obj
        return _distribution(name)
    with patch("autosport.release_dependency_graph.metadata.distribution", side_effect=wrong):
        with pytest.raises(ValueError, match="transitive version"):
            capture_build_host_dependency_graph(SOURCE)


def _release(tmp_path, dependency_graph):
    exe = tmp_path / "Autosport.exe"
    start = tmp_path / "WINDOWS_START_HERE.txt"
    example = tmp_path / "example"
    example.mkdir()
    (example / "market.jsonl").write_text("{}\n", encoding="utf-8")
    exe.write_bytes(b"synthetic-windows-executable")
    start.write_text("start\n", encoding="utf-8")
    base = {
        "status": "PASS", "real_money_execution": False,
        "human_tested": False, "nvda_verified": False,
    }
    evidence_paths = []
    for kind in ("diagnostic", "accessibility", "keyboard", "restart"):
        row = dict(base)
        if kind == "restart":
            row.update({
                "session_restart_status": "PASS",
                "transaction_recovery_status": "PASS",
                "recovery_disposition": "aborted_uncommitted",
                "process_kill_relaunch_status": "PASS",
                "process_kill_stage_pid": 101,
                "process_kill_return_code": -15,
                "process_recovery_pid": 202,
                "process_recovery_run_id": "build-host-dependency-graph",
                "process_recovery_disposition": "committed",
                "process_recovery_registry_status": "completed",
                "process_recovery_manifest_phase": "completed",
                "process_recovery_base_paper_book_sha256": "1" * 64,
                "process_recovery_new_paper_book_sha256": "2" * 64,
                "process_recovery_base_decision_ledger_sha256": "3" * 64,
                "process_recovery_new_decision_ledger_sha256": "4" * 64,
            })
        path = tmp_path / (kind + ".json")
        path.write_text(json.dumps(row), encoding="utf-8")
        evidence_paths.append(path)
    archive, _ = build_windows_package(
        exe, start, example, *evidence_paths, tmp_path / "release.zip", SHA,
        source_manifest=SOURCE, dependency_metadata=_direct(),
        dependency_graph=dependency_graph,
    )
    return archive


def test_graph_is_in_actual_release_zip_and_canonical_verifier_requires_it(tmp_path):
    archive = _release(tmp_path, _captured())
    verified = verify_windows_package(
        archive, expected_source_sha=SHA,
        require_source_manifest=True,
        require_dependency_metadata=True,
        require_dependency_graph=True,
    )
    assert verified["status"] == "PASS"
    import zipfile
    with zipfile.ZipFile(archive) as zip_file:
        member = zip_file.read("Autosport-V1/DEPENDENCY_GRAPH_METADATA.json")
    assert json.loads(member)["evidence_class"] == "BUILD_HOST_REQUIREMENT_GRAPH_ONLY"


def test_requiring_missing_graph_fails_even_when_old_direct_metadata_passes(tmp_path):
    archive = _release(tmp_path, None)
    assert verify_windows_package(
        archive, expected_source_sha=SHA,
        require_source_manifest=True, require_dependency_metadata=True,
    )["status"] == "PASS"
    with pytest.raises(ValueError, match="missing build-host dependency graph"):
        verify_windows_package(
            archive, expected_source_sha=SHA, require_dependency_graph=True,
        )


def test_invalid_graph_is_rejected_before_any_archive_is_written(tmp_path):
    invalid = _captured()
    invalid["evidence_class"] = "BUNDLED_AND_LICENSE_APPROVED"
    with pytest.raises(ValueError, match="evidence class"):
        _release(tmp_path, invalid)
    assert not (tmp_path / "release.zip").exists()
