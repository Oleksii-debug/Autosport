"""Direct build-host metadata is evidence only, never a full SBOM assertion."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from autosport.release_sbom import (
    capture_direct_installed_dependencies, validate_direct_dependency_metadata,
)

SHA = "a" * 40
SOURCE = {
    "source_sha": SHA,
    "declared_runtime_dependencies": ["APScheduler==3.11.3", "tk-uia==0.8.0"],
    "declared_build_dependencies": ["pyinstaller==6.22.3"],
}


def _fake_distribution(name: str):
    versions = {"apscheduler": "3.11.3", "tk-uia": "0.8.0", "pyinstaller": "6.22.3"}
    labels = {"apscheduler": "APScheduler", "tk-uia": "tk_uia", "pyinstaller": "pyinstaller"}
    return SimpleNamespace(
        version=versions[name],
        metadata={"Name": labels[name], "License-Expression": "MIT" if name == "tk-uia" else None},
    )


def test_build_host_direct_metadata_is_deterministic_and_honest():
    with patch("autosport.release_sbom.metadata.distribution", side_effect=_fake_distribution):
        one = capture_direct_installed_dependencies(SOURCE)
        two = capture_direct_installed_dependencies(SOURCE)
    assert one == two
    assert one["source_sha"] == SHA
    assert one["evidence_class"] == "BUILD_HOST_DIRECT_DEPENDENCIES_ONLY"
    assert [entry["name"] for entry in one["distributions"]] == [
        "apscheduler", "pyinstaller", "tk-uia"
    ]
    assert one["distributions"][-1]["license_evidence"] == "DISTRIBUTION_METADATA_UNVERIFIED"
    assert one["distributions"][0]["license_evidence"] == "NOT_PROVIDED"


@pytest.mark.parametrize("edit", [
    lambda x: x.update(schema_version=True),
    lambda x: x.update(evidence_class="COMPLETE_SBOM_VERIFIED"),
    lambda x: x["distributions"].pop(),
    lambda x: x["distributions"][0].update(version="999"),
    lambda x: x["distributions"][0].update(name="APScheduler"),
    lambda x: x["distributions"][0].update(license_evidence="LICENSE_CLEARED"),
    lambda x: x["distributions"][0].update(license_expression="MIT"),
    lambda x: x["distributions"].reverse(),
])
def test_rejects_forged_or_incomplete_metadata(edit):
    with patch("autosport.release_sbom.metadata.distribution", side_effect=_fake_distribution):
        original = capture_direct_installed_dependencies(SOURCE)
    tampered = deepcopy(original)
    edit(tampered)
    with pytest.raises(ValueError):
        validate_direct_dependency_metadata(tampered, SOURCE)


def test_rejects_missing_or_wrong_distribution_before_release():
    with patch(
        "autosport.release_sbom.metadata.distribution",
        side_effect=__import__("importlib").metadata.PackageNotFoundError,
    ):
        with pytest.raises(ValueError, match="not installed"):
            capture_direct_installed_dependencies(SOURCE)
    wrong = lambda name: SimpleNamespace(
        version="999", metadata={"Name": name, "License-Expression": "MIT"}
    )
    with patch("autosport.release_sbom.metadata.distribution", side_effect=wrong):
        with pytest.raises(ValueError, match="differs"):
            capture_direct_installed_dependencies(SOURCE)


def test_rejects_duplicate_names_and_conflicting_pins():
    bad = deepcopy(SOURCE)
    bad["declared_runtime_dependencies"].append("tk_uia==0.8.0")
    with pytest.raises(ValueError, match="duplicate"):
        capture_direct_installed_dependencies(bad)
    bad = deepcopy(SOURCE)
    bad["declared_build_dependencies"].append("TK_UIA==9.0.0")
    with pytest.raises(ValueError, match="conflicting"):
        capture_direct_installed_dependencies(bad)


def _packaging_fixture(root, source_manifest, dependency_metadata, *, embedded_inventory=None):
    import json
    from autosport.release_package import build_windows_package
    exe = root / "Autosport.exe"
    guide = root / "WINDOWS_START_HERE.txt"
    sample = root / "sample"
    sample.mkdir()
    (sample / "market.jsonl").write_text("{}\\n", encoding="utf-8")
    exe.write_bytes(b"fake-windows-executable")
    guide.write_text("start\\n", encoding="utf-8")
    base = {"status": "PASS", "real_money_execution": False, "human_tested": False, "nvda_verified": False}
    paths = []
    for label in ("diagnostic", "accessibility", "keyboard", "restart"):
        record = dict(base)
        if label == "restart":
            record.update({
                "session_restart_status": "PASS", "transaction_recovery_status": "PASS",
                "recovery_disposition": "aborted_uncommitted",
                "process_kill_relaunch_status": "PASS", "process_kill_stage_pid": 101,
                "process_kill_return_code": -15, "process_recovery_pid": 202,
                "process_recovery_run_id": "source-test",
                "process_recovery_disposition": "committed",
                "process_recovery_registry_status": "completed",
                "process_recovery_manifest_phase": "completed",
                "process_recovery_base_paper_book_sha256": "1" * 64,
                "process_recovery_new_paper_book_sha256": "2" * 64,
                "process_recovery_base_decision_ledger_sha256": "3" * 64,
                "process_recovery_new_decision_ledger_sha256": "4" * 64,
            })
        path = root / (label + ".json")
        path.write_text(json.dumps(record), encoding="utf-8")
        paths.append(path)
    package, _ = build_windows_package(
        exe, guide, sample, *paths, root / "release.zip", SHA,
        source_manifest=source_manifest, dependency_metadata=dependency_metadata,
        embedded_executable_inventory=embedded_inventory,
    )
    return package


def test_exact_source_bound_metadata_round_trip_and_required_gate(tmp_path):
    from autosport.release_package import (
        _SOURCE_MANIFEST_INPUTS, verify_windows_package,
    )
    manifest = {
        **SOURCE,
        "schema_version": 1,
        "evidence_class": "SOURCE_DECLARATIONS_ONLY",
        "source_files_sha256": {path: "0" * 64 for path in _SOURCE_MANIFEST_INPUTS},
    }
    with patch("autosport.release_sbom.metadata.distribution", side_effect=_fake_distribution):
        evidence = capture_direct_installed_dependencies(manifest)
    package = _packaging_fixture(tmp_path, manifest, evidence)
    passed = verify_windows_package(
        package, expected_source_sha=SHA,
        require_source_manifest=True, require_dependency_metadata=True,
    )
    assert passed["status"] == "PASS"
    assert passed["source_sha"] == SHA


def test_release_requires_real_build_host_metadata_in_cli_mode(tmp_path):
    from autosport.release_package import (
        _SOURCE_MANIFEST_INPUTS, verify_windows_package,
    )
    manifest = {
        **SOURCE, "schema_version": 1, "evidence_class": "SOURCE_DECLARATIONS_ONLY",
        "source_files_sha256": {path: "0" * 64 for path in _SOURCE_MANIFEST_INPUTS},
    }
    package = _packaging_fixture(tmp_path, manifest, None)
    with pytest.raises(ValueError, match="missing direct dependency metadata"):
        verify_windows_package(
            package, expected_source_sha=SHA,
            require_source_manifest=True, require_dependency_metadata=True,
        )


def test_binary_bound_inventory_roundtrip_and_missing_required_gate(tmp_path):
    """The additional manifest is bound to the exact packaged EXE, not host wheel names."""
    from autosport.release_package import _SOURCE_MANIFEST_INPUTS, verify_windows_package
    from autosport.release_sbom import capture_embedded_executable_inventory
    from types import SimpleNamespace

    manifest = {
        **SOURCE, "schema_version": 1,
        "evidence_class": "SOURCE_DECLARATIONS_ONLY",
        "source_files_sha256": {path: "0" * 64 for path in _SOURCE_MANIFEST_INPUTS},
    }
    exe = tmp_path / "Autosport.exe"
    exe.write_bytes(b"fake-windows-executable")
    census = capture_embedded_executable_inventory(
        exe, SHA,
        reader_factory=lambda _: SimpleNamespace(
            toc={"PYZ.pyz": (0, 10, 20, 0, "z")}
        ),
    )
    package = _packaging_fixture(
        tmp_path, manifest, None, embedded_inventory=census,
    )
    assert verify_windows_package(
        package, expected_source_sha=SHA, require_source_manifest=True,
        require_embedded_executable_inventory=True,
    )["status"] == "PASS"

    without_dir = tmp_path / "without-inventory"
    without_dir.mkdir()
    package_without_inventory = _packaging_fixture(without_dir, manifest, None)
    with pytest.raises(ValueError, match="missing embedded executable inventory"):
        verify_windows_package(
            package_without_inventory, expected_source_sha=SHA,
            require_embedded_executable_inventory=True,
        )

    forged = deepcopy(census)
    forged["executable_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="inventory identity/schema"):
        _packaging_fixture(tmp_path, manifest, None, embedded_inventory=forged)
