"""Plan 6 §5Б: direct and transitive build-host disclosures must agree.

Both snapshots are deliberately LIMITED build-host evidence; passing these
checks is never an executable SBOM, license approval, or physical NVDA claim.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import zipfile

import pytest

from autosport.release_dependency_graph import validate_build_host_dependency_graph
from autosport.release_package import (
    _SOURCE_MANIFEST_INPUTS,
    _canonical_json_bytes,
    _require_dependency_evidence_coherence,
    _write_canonical_zip,
    build_windows_package,
    verify_windows_package,
)
from autosport.release_sbom import validate_direct_dependency_metadata


SHA = "a" * 40
SOURCE = {
    "schema_version": 1,
    "source_sha": SHA,
    "evidence_class": "SOURCE_DECLARATIONS_ONLY",
    "source_files_sha256": {p: "0" * 64 for p in _SOURCE_MANIFEST_INPUTS},
    "declared_runtime_dependencies": ["tk-uia==0.8.0"],
    "declared_build_dependencies": ["pyinstaller==6.22.3"],
}


def _snapshots():
    entries = [
        {"name": "pyinstaller", "version": "6.22.3", "roles": ["build"],
         "license_expression": None, "license_evidence": "NOT_PROVIDED"},
        {"name": "tk-uia", "version": "0.8.0", "roles": ["runtime"],
         "license_expression": "MIT",
         "license_evidence": "DISTRIBUTION_METADATA_UNVERIFIED"},
    ]
    direct = {
        "schema_version": 1, "source_sha": SHA,
        "evidence_class": "BUILD_HOST_DIRECT_DEPENDENCIES_ONLY",
        "distributions": entries,
    }
    graph = {
        "schema_version": 1, "source_sha": SHA,
        "evidence_class": "BUILD_HOST_REQUIREMENT_GRAPH_ONLY",
        "components": [
            {"name": entry["name"], "version": entry["version"],
             "direct_roles": entry["roles"], "requires": [],
             "license_expression": entry["license_expression"],
             "license_evidence": entry["license_evidence"]}
            for entry in entries
        ],
    }
    return direct, graph


def _legitimate_but_conflicting(direct, graph):
    # Both independently valid metadata schemas and source/version pins remain
    # intact; only the unverified claimed license on the same root disagrees.
    graph["components"][1]["license_expression"] = "BSD-2-Clause"


def test_coherent_build_host_evidence_is_accepted():
    direct, graph = _snapshots()
    validate_direct_dependency_metadata(direct, SOURCE)
    validate_build_host_dependency_graph(graph, SOURCE)
    _require_dependency_evidence_coherence(direct, graph)


def test_two_independently_valid_snapshots_cannot_disagree():
    direct, graph = _snapshots()
    _legitimate_but_conflicting(direct, graph)
    validate_direct_dependency_metadata(direct, SOURCE)
    validate_build_host_dependency_graph(graph, SOURCE)
    with pytest.raises(ValueError, match="evidence disagree"):
        _require_dependency_evidence_coherence(direct, graph)


def test_builder_rejects_disagreement_before_reading_or_writing_any_release(tmp_path):
    direct, graph = _snapshots()
    _legitimate_but_conflicting(direct, graph)
    path = tmp_path / "must-not-exist.zip"
    with pytest.raises(ValueError, match="evidence disagree"):
        build_windows_package(
            "unavailable.exe", "unavailable-guide.txt", "unavailable-sample",
            "no-diagnostic.json", "no-accessibility.json", "no-keyboard.json",
            "no-restart.json", path, SHA,
            source_manifest=SOURCE, dependency_metadata=direct,
            dependency_graph=graph,
        )
    assert not path.exists()


def _release_fixture(root: Path) -> Path:
    exe = root / "Autosport.exe"
    guide = root / "WINDOWS_START_HERE.txt"
    example = root / "example"
    example.mkdir()
    (example / "market.jsonl").write_text("{}\n", encoding="utf-8")
    exe.write_bytes(b"synthetic-executable")
    guide.write_text("paper-only release\n", encoding="utf-8")
    rows = []
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
                "process_recovery_run_id": "cross-consistency-test",
                "process_recovery_disposition": "committed",
                "process_recovery_registry_status": "completed",
                "process_recovery_manifest_phase": "completed",
                "process_recovery_base_paper_book_sha256": "1" * 64,
                "process_recovery_new_paper_book_sha256": "2" * 64,
                "process_recovery_base_decision_ledger_sha256": "3" * 64,
                "process_recovery_new_decision_ledger_sha256": "4" * 64,
            })
        output = root / (kind + ".json")
        output.write_text(json.dumps(record), encoding="utf-8")
        rows.append(output)
    direct, graph = _snapshots()
    archive, _ = build_windows_package(
        exe, guide, example, *rows, root / "valid.zip", SHA,
        source_manifest=SOURCE, dependency_metadata=direct,
        dependency_graph=graph,
    )
    return archive


def test_verifier_rejects_consistent_hashes_but_inconsistent_evidence(tmp_path):
    valid = _release_fixture(tmp_path)
    assert verify_windows_package(
        valid, expected_source_sha=SHA,
        require_source_manifest=True,
        require_dependency_metadata=True,
        require_dependency_graph=True,
    )["status"] == "PASS"
    with zipfile.ZipFile(valid) as archive:
        members = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    prefix = "Autosport-V1/"
    graph_name = prefix + "DEPENDENCY_GRAPH_METADATA.json"
    graph = json.loads(members[graph_name])
    graph["components"][1]["license_expression"] = "BSD-2-Clause"
    members[graph_name] = _canonical_json_bytes(graph)

    manifest_name = prefix + "PACKAGE_MANIFEST.json"
    manifest = json.loads(members[manifest_name])
    manifest["files"]["DEPENDENCY_GRAPH_METADATA.json"] = sha256(members[graph_name]).hexdigest()
    members[manifest_name] = _canonical_json_bytes(manifest)
    checksum_name = prefix + "SHA256SUMS.txt"
    members[checksum_name] = "".join(
        f"{sha256(members[name]).hexdigest()}  {name[len(prefix):]}\n"
        for name in sorted(members) if name != checksum_name
    ).encode("utf-8")
    tampered = tmp_path / "conflicting-but-rehashed.zip"
    _write_canonical_zip(tampered, members)

    with pytest.raises(ValueError, match="evidence disagree"):
        verify_windows_package(
            tampered, expected_source_sha=SHA,
            require_source_manifest=True,
            require_dependency_metadata=True,
            require_dependency_graph=True,
        )
