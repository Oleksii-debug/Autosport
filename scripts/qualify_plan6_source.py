"""Run real Plan-6 local source tests on exact clean Git HEAD.

This source-only receipt is not evidence of a Windows installer, NVDA or full SBOM.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

GROUPS = (
    ("uk_localization", (
        "tests/test_localization_catalog.py",
        "tests/test_gui_localization_contract.py",
    )),
    ("machine_accessibility", (
        "tests/test_as02_webview_hidden_implicit_names.py",
        "tests/test_external_uia_semantic_readiness_contract.py",
    )),
    ("release_integrity", (
        "tests/test_plan6_5b_binary_module_attribution_release.py",
        "tests/test_plan6_5b_carchive_snapshot_consistency.py",
        "tests/test_plan6_5b_carchive_span_nonoverlap.py",
        "tests/test_plan6_5b_config_migration_source_binding.py",
        "tests/test_plan6_5b_dependency_evidence_coherence.py",
        "tests/test_plan6_5b_dependency_license_evidence.py",
        "tests/test_plan6_5b_embedded_aggregate_budget.py",
        "tests/test_plan6_5b_embedded_executable_inventory.py",
        "tests/test_plan6_5b_embedded_pyz_test_module_rejection.py",
        "tests/test_plan6_5b_embedded_secret_payload_rejection.py",
        "tests/test_plan6_5b_embedded_windows_names.py",
        "tests/test_plan6_5b_nested_pyz_inventory.py",
        "tests/test_plan6_5b_portable_data_exe_resource_bounds.py",
        "tests/test_plan6_5b_pyz_nonoverlap.py",
        "tests/test_plan6_5b_second_executable_inventory.py",
        "tests/test_plan6_5b_source_sha_evidence_boundary.py",
        "tests/test_plan6_release_windows_handle_path_fingerprint.py",
        "tests/test_plan6_7a_windows_update_stat_views.py",
        "tests/test_plan6_7a_installed_trust_stat_views.py",
        "tests/test_plan6_source_qualifier.py",
        "tests/test_release_binary_attribution.py",
        "tests/test_release_build_dependency_metadata.py",
        "tests/test_release_package_source_identity.py",
    )),
    ("paper_install_update_recovery", (
        "tests/test_offline_release_install.py",
        "tests/test_offline_release_lifecycle.py",
        "tests/test_offline_release_recovery.py",
        "tests/test_offline_release_upgrade.py",
        "tests/test_data_tools_installed_release_entry.py",
        "tests/test_offline_release_deflate_errors.py",
        "tests/test_offline_release_install_windows_namespace.py",
        "tests/test_plan6_6a_data_tool_zip_preflight.py",
        "tests/test_plan6_6a_uninstall_atomic_quarantine.py",
        "tests/test_plan6_nested_release_reparse_boundary.py",
        "tests/test_plan6_offline_install_reparse_boundary.py",
    )),
)
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PASSED = re.compile(r"(?<!\d)(\d+) passed(?:,|\s|$)")

def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        timeout=20, check=False,
    )
    if result.returncode:
        raise ValueError("source Git identity unavailable")
    return result.stdout.strip()

def _identity(root: Path) -> str:
    sha = _git(root, "rev-parse", "--verify", "HEAD")
    if not _SHA.fullmatch(sha):
        raise ValueError("not a canonical full Git commit")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("source tree changed or contains untracked files")
    return sha

def _test_group(root: Path, name: str, paths: tuple[str, ...], *, timeout: int) -> dict:
    if not paths or not all((root / path).is_file() for path in paths):
        return {"group": name, "status": "FAIL_MISSING_TEST_SOURCE",
                "passed": 0, "exit_code": None}
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *paths],
            cwd=root, capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"group": name, "status": "FAIL_UNEXECUTED",
                "passed": 0, "exit_code": None}
    summary = (result.stdout + "\n" + result.stderr)[-12000:]
    match = _PASSED.search(summary)
    passed = int(match.group(1)) if match else 0
    status = "PASS" if result.returncode == 0 and passed > 0 else "FAIL"
    return {"group": name, "status": status, "passed": passed,
            "exit_code": result.returncode}

def run(root: Path, *, timeout: int = 1200) -> dict:
    if type(timeout) is not int or not 1 <= timeout <= 7200:
        raise ValueError("invalid group timeout")
    before = _identity(root)
    results = [_test_group(root, name, paths, timeout=timeout)
               for name, paths in GROUPS]
    try:
        after = _identity(root)
        stable = before == after
    except ValueError:
        stable = False
    qualified = stable and len(results) == len(GROUPS) and all(
        item["status"] == "PASS" for item in results
    )
    return {
        "schema_version": 1,
        "evidence_class": "EXACT_HEAD_LOCAL_SOURCE_TESTS_ONLY",
        "source_sha": before,
        "status": "PASS_SOURCE_ONLY" if qualified else "FAIL_OR_INCOMPLETE",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "platform": sys.platform,
        "tracked_tree_stable": stable,
        "groups": results,
        "executed_pass_count": sum(item["passed"] for item in results),
        "windows_packaged_candidate_verified": False,
        "complete_executable_sbom_verified": False,
        "license_clearance_verified": False,
        "schema_migration_qualified": False,
        "human_tested": False,
        "nvda_verified": False,
        "real_money_execution": False,
        "whole_product_complete": False,
    }

def _write_report(destination: Path, report: dict) -> None:
    if not destination.is_absolute() or not destination.parent.is_dir():
        raise ValueError("report destination needs an absolute existing parent")
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode("utf-8")
    fd, staging = tempfile.mkstemp(prefix=".plan6-qual-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging, destination)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Qualify existing Plan 6 tests on exact clean source HEAD"
    )
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run(Path(__file__).resolve().parents[1], timeout=args.timeout)
        if args.output:
            _write_report(args.output, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] == "PASS_SOURCE_ONLY" else 3
    except (OSError, ValueError, subprocess.SubprocessError):
        print("plan6_qualification=FAIL_CLOSED")
        return 3

if __name__ == "__main__":
    raise SystemExit(main())
