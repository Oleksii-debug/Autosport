"""Canonical Windows ZIP integration for review-only sealed binary module attribution."""
from __future__ import annotations

import copy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from autosport.release_binary_attribution import capture_binary_module_attribution
from autosport.data_tool_package import (
    bind_portable_data_tool, verify_portable_data_tool,
)
from autosport.release_package import (
    _SOURCE_MANIFEST_INPUTS, build_windows_package, verify_windows_package,
)


SHA = "a" * 40
SOURCE = {
    "schema_version": 1,
    "source_sha": SHA,
    "evidence_class": "SOURCE_DECLARATIONS_ONLY",
    "source_files_sha256": {p: "0" * 64 for p in _SOURCE_MANIFEST_INPUTS},
    "declared_runtime_dependencies": ["tk-uia==0.8.0"],
    "declared_build_dependencies": ["pyinstaller==6.22.3"],
}
GRAPH = {
    "schema_version": 1,
    "source_sha": SHA,
    "evidence_class": "BUILD_HOST_REQUIREMENT_GRAPH_ONLY",
    "components": [
        {
            "name": name, "version": version, "direct_roles": roles,
            "requires": [], "license_expression": None,
            "license_evidence": "NOT_PROVIDED",
        }
        for name, version, roles in (
            ("pyinstaller", "6.22.3", ["build"]),
            ("tk-uia", "0.8.0", ["runtime"]),
        )
    ],
}


def _input_files(root: Path):
    exe = root / "Autosport.exe"
    exe.write_bytes(b"synthetic sealed main executable")
    guide = root / "WINDOWS_START_HERE.txt"
    guide.write_text("PAPER simulation only\n", encoding="utf-8")
    sample = root / "example"
    sample.mkdir()
    (sample / "market.jsonl").write_text("{}\n", encoding="utf-8")
    paths = []
    for kind in ("diagnostic", "accessibility", "keyboard", "restart"):
        record = {
            "status": "PASS", "real_money_execution": False,
            "human_tested": False, "nvda_verified": False,
        }
        if kind == "restart":
            record.update({
                "session_restart_status": "PASS",
                "transaction_recovery_status": "PASS",
                "recovery_disposition": "aborted_uncommitted",
                "process_kill_relaunch_status": "PASS",
                "process_kill_stage_pid": 101,
                "process_kill_return_code": -15,
                "process_recovery_pid": 202,
                "process_recovery_run_id": "binary-attribution",
                "process_recovery_disposition": "committed",
                "process_recovery_registry_status": "completed",
                "process_recovery_manifest_phase": "completed",
                "process_recovery_base_paper_book_sha256": "1" * 64,
                "process_recovery_new_paper_book_sha256": "2" * 64,
                "process_recovery_base_decision_ledger_sha256": "3" * 64,
                "process_recovery_new_decision_ledger_sha256": "4" * 64,
            })
        path = root / (kind + ".json")
        path.write_text(json.dumps(record), encoding="utf-8")
        paths.append(path)
    inventory = {
        "schema_version": 2, "source_sha": SHA,
        "executable_sha256": sha256(exe.read_bytes()).hexdigest(),
        "evidence_class": "EMBEDDED_PYINSTALLER_CARCHIVE_AND_PYZ_TOC_ONLY",
        "components": [{
            "name": "PYZ-00.pyz", "typecode": "z",
            "packed_bytes": 100, "unpacked_bytes": 500,
        }],
        "pyz_modules": [{
            "name": "example.module", "typecode": 0,
            "position": 1, "packed_bytes": 25,
        }],
    }
    attribution = capture_binary_module_attribution(
        GRAPH, inventory, packages_distributions={"example": ["tk-uia"]},
    )
    return (exe, guide, sample, *paths), inventory, attribution


def test_canonical_package_seals_attribution_and_verifies_it(tmp_path: Path):
    files, inventory, attribution = _input_files(tmp_path)
    output, _ = build_windows_package(
        *files, tmp_path / "release.zip", SHA,
        source_manifest=SOURCE, dependency_graph=GRAPH,
        embedded_executable_inventory=inventory,
        binary_module_attribution=attribution,
    )
    result = verify_windows_package(
        output, expected_source_sha=SHA, require_source_manifest=True,
        require_dependency_graph=True, require_embedded_executable_inventory=True,
        require_binary_module_attribution=True,
    )
    assert result["status"] == "PASS"
    assert attribution["modules"][0]["review"] == "HOST_MATCH_REVIEW_REQUIRED"


def test_missing_binary_report_fails_official_required_verifier(tmp_path: Path):
    files, inventory, _attribution = _input_files(tmp_path)
    output, _ = build_windows_package(
        *files, tmp_path / "release.zip", SHA,
        source_manifest=SOURCE, dependency_graph=GRAPH,
        embedded_executable_inventory=inventory,
    )
    with pytest.raises(ValueError, match="lacks sealed binary module attribution"):
        verify_windows_package(
            output, expected_source_sha=SHA,
            require_binary_module_attribution=True,
        )


def test_forged_clearance_rejected_before_package_publication(tmp_path: Path):
    files, inventory, attribution = _input_files(tmp_path)
    forged = copy.deepcopy(attribution)
    forged["modules"][0]["review"] = "LICENSE_APPROVED"
    output = tmp_path / "must-not-exist.zip"
    with pytest.raises(ValueError, match="license clearance"):
        build_windows_package(
            *files, output, SHA,
            source_manifest=SOURCE, dependency_graph=GRAPH,
            embedded_executable_inventory=inventory,
            binary_module_attribution=forged,
        )
    assert not output.exists()


def _data_candidate(root: Path):
    data_exe = root / "Autosport-Data.exe"
    data_exe.write_bytes(b"synthetic independent Data executable")
    census = {
        "schema_version": 2, "source_sha": SHA,
        "executable_sha256": sha256(data_exe.read_bytes()).hexdigest(),
        "evidence_class": "EMBEDDED_PYINSTALLER_CARCHIVE_AND_PYZ_TOC_ONLY",
        "components": [{
            "name": "PYZ-00.pyz", "typecode": "z",
            "packed_bytes": 100, "unpacked_bytes": 500,
        }],
        "pyz_modules": [{
            "name": "example.module", "typecode": 0,
            "position": 1, "packed_bytes": 25,
        }],
    }
    report = capture_binary_module_attribution(
        GRAPH, census, packages_distributions={"example": ["tk-uia"]},
    )
    return data_exe, census, report


def test_independent_data_binary_hints_sealed_and_verified(tmp_path: Path):
    files, _main_census, _main_report = _input_files(tmp_path)
    package, _ = build_windows_package(
        *files, tmp_path / "release.zip", SHA,
        source_manifest=SOURCE, dependency_graph=GRAPH,
    )
    data_exe, census, attribution = _data_candidate(tmp_path)
    bind_portable_data_tool(
        package, data_exe,
        data_executable_inventory=census,
        data_binary_module_attribution=attribution,
    )
    verified = verify_portable_data_tool(
        package, require_data_executable_inventory=True,
        require_data_binary_module_attribution=True,
    )
    assert verified["status"] == "PASS"
    assert verified["autosport_data_exe_sha256"] == census["executable_sha256"]


def test_data_binary_report_required_only_at_official_gate(tmp_path: Path):
    files, _main_census, _main_report = _input_files(tmp_path)
    package, _ = build_windows_package(
        *files, tmp_path / "release.zip", SHA,
        source_manifest=SOURCE, dependency_graph=GRAPH,
    )
    data_exe, census, _attribution = _data_candidate(tmp_path)
    bind_portable_data_tool(package, data_exe, data_executable_inventory=census)
    assert verify_portable_data_tool(package)["status"] == "PASS"
    with pytest.raises(ValueError, match="missing Data binary module attribution"):
        verify_portable_data_tool(
            package, require_data_binary_module_attribution=True,
        )


def test_data_binary_fake_license_approval_rejected_before_zip_write(tmp_path: Path):
    files, _main_census, _main_report = _input_files(tmp_path)
    package, _ = build_windows_package(
        *files, tmp_path / "release.zip", SHA,
        source_manifest=SOURCE, dependency_graph=GRAPH,
    )
    data_exe, census, attribution = _data_candidate(tmp_path)
    before = package.read_bytes()
    forged = copy.deepcopy(attribution)
    forged["modules"][0]["review"] = "CLEARANCE_APPROVED"
    with pytest.raises(ValueError, match="license clearance"):
        bind_portable_data_tool(
            package, data_exe,
            data_executable_inventory=census,
            data_binary_module_attribution=forged,
        )
    assert package.read_bytes() == before
