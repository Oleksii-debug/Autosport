"""Plan 6 §5Б: the official release must inventory BOTH sealed PE CArchives.

This remains a bounded CArchive census, not a transitive SBOM or license clearance.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from autosport.data_tool_package import (
    bind_portable_data_tool, verify_portable_data_tool,
)
from autosport.release_package import build_windows_package
from autosport.release_sbom import capture_embedded_executable_inventory


SOURCE = "a" * 40


def _base(tmp_path: Path) -> tuple[Path, Path]:
    exe = tmp_path / "Autosport.exe"
    data_exe = tmp_path / "Autosport-Data.exe"
    guide = tmp_path / "WINDOWS_START_HERE.txt"
    example = tmp_path / "demo"
    example.mkdir()
    (example / "market.jsonl").write_text("{}\n", encoding="utf-8")
    exe.write_bytes(b"mock main PE bytes")
    data_exe.write_bytes(b"mock independent data PE bytes")
    guide.write_text("Operator guide\n", encoding="utf-8")
    evidence = {"status": "PASS", "real_money_execution": False,
                "human_tested": False, "nvda_verified": False}
    paths = []
    for name in ("diagnostic", "accessibility", "keyboard", "recovery"):
        path = tmp_path / (name + ".json")
        payload = dict(evidence)
        if name == "recovery":
            payload.update({
                "session_restart_status": "PASS",
                "transaction_recovery_status": "PASS",
                "recovery_disposition": "aborted_uncommitted",
                "process_kill_relaunch_status": "PASS",
                "process_kill_stage_pid": 101, "process_kill_return_code": -15,
                "process_recovery_pid": 202,
                "process_recovery_run_id": "both-exe-inventory-fixture",
                "process_recovery_disposition": "committed",
                "process_recovery_registry_status": "completed",
                "process_recovery_manifest_phase": "completed",
                "process_recovery_base_paper_book_sha256": "1" * 64,
                "process_recovery_new_paper_book_sha256": "2" * 64,
                "process_recovery_base_decision_ledger_sha256": "3" * 64,
                "process_recovery_new_decision_ledger_sha256": "4" * 64,
            })
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths.append(path)
    package = tmp_path / "release.zip"
    build_windows_package(
        exe, guide, example, *paths, package, SOURCE,
    )
    return package, data_exe


def _census(data_exe: Path) -> dict:
    return capture_embedded_executable_inventory(
        data_exe, SOURCE,
        reader_factory=lambda _: SimpleNamespace(
            toc={"PYZ.pyz": (0, 30, 60, True, "z"),
                 "python312.dll": (30, 40, 70, True, "b")},
        ),
    )


def test_second_executable_census_is_bound_to_final_package(tmp_path: Path) -> None:
    package, data_exe = _base(tmp_path)
    receipt = _census(data_exe)
    result = bind_portable_data_tool(
        package, data_exe, data_executable_inventory=receipt,
    )
    report = verify_portable_data_tool(
        package, require_data_executable_inventory=True,
    )
    assert report["status"] == "PASS"
    assert result["package_sha256"] == report["package_sha256"]
    assert report["autosport_data_exe_sha256"] == receipt["executable_sha256"]
    with zipfile.ZipFile(package) as archive:
        actual = json.loads(archive.read("Autosport-V1/DATA_EXECUTABLE_INVENTORY.json"))
        assert actual == receipt
        assert hashlib.sha256(archive.read("Autosport-V1/Autosport-Data.exe")).hexdigest() == receipt["executable_sha256"]


def test_legacy_fixture_mode_is_not_mistaken_for_official_release(tmp_path: Path) -> None:
    package, data_exe = _base(tmp_path)
    bind_portable_data_tool(package, data_exe)
    assert verify_portable_data_tool(package)["status"] == "PASS"
    with pytest.raises(ValueError, match="missing Data executable inventory"):
        verify_portable_data_tool(
            package, require_data_executable_inventory=True,
        )


@pytest.mark.parametrize("change", [
    lambda receipt: receipt.update(source_sha="b" * 40),
    lambda receipt: receipt.update(executable_sha256="0" * 64),
    lambda receipt: receipt.update(evidence_class="COMPLETE_SBOM"),
    lambda receipt: receipt["components"][0].update(name="../../secret"),
])
def test_rejects_forged_second_binary_census_before_zip_mutation(
    tmp_path: Path, change,
) -> None:
    package, data_exe = _base(tmp_path)
    inventory = deepcopy(_census(data_exe))
    change(inventory)
    before = package.read_bytes()
    with pytest.raises(ValueError):
        bind_portable_data_tool(
            package, data_exe, data_executable_inventory=inventory,
        )
    assert package.read_bytes() == before


def test_official_builder_requires_independent_data_executable_census() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts/package_windows.py").read_text(
        encoding="utf-8",
    )
    assert "data_embedded_inventory = capture_embedded_executable_inventory(" in source
    assert "trusted_data_exe, args.source_sha" in source
    assert "data_executable_inventory=data_embedded_inventory" in source
    assert "require_data_executable_inventory=True" in source
