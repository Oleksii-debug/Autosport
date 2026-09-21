from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .evidence_export import verify_evidence_manifest as _verify_evidence_manifest
from .nvda_acceptance import create_template as _create_nvda_template
from .nvda_acceptance import validate_evidence as _validate_nvda_evidence


@dataclass(frozen=True, slots=True)
class WorkspaceEvidenceHandoff:
    """Operator-facing projection of one canonical workspace-evidence verification.

    This is deliberately not a second verifier. The canonical manifest verifier owns
    workspace identity and integrity semantics; this projection only gives packaged
    product surfaces a small API that cannot accidentally promote release truth.
    """

    status: Literal["PASS"]
    manifest_sha256: str
    file_count: int
    run_summary_count: int
    fixed_evidence_set_complete: bool
    real_money_execution: Literal[False] = False
    human_tested: Literal[False] = False
    nvda_verified: Literal[False] = False
    whole_product_complete: Literal[False] = False


@dataclass(frozen=True, slots=True)
class NvdaEvidenceHandoff:
    """Machine validation result for human-supplied physical NVDA evidence.

    PASS means that the supplied record satisfies the canonical validator for the
    exact release candidate. It never means that this adapter itself performed a
    physical Windows/NVDA test and therefore cannot promote human/NVDA truth.
    """

    status: Literal["PASS", "FAIL"]
    package_sha256: str
    source_sha: str
    autosport_exe_sha256: str
    failed_checks: tuple[str, ...]
    requires_owner_release_decision: Literal[True]
    real_money_execution: Literal[False] = False
    human_tested: Literal[False] = False
    nvda_verified: Literal[False] = False
    whole_product_complete: Literal[False] = False


def _require_machine_false(payload: dict[str, Any], *fields: str) -> None:
    for field in fields:
        if payload.get(field) is not False:
            raise ValueError(
                f"canonical evidence result attempted to promote machine-forbidden truth: {field}"
            )


def verify_workspace_manifest(
    manifest: str | Path,
    workspace: str | Path,
) -> WorkspaceEvidenceHandoff:
    """Verify workspace evidence through the single canonical verifier.

    Canonical verifier failures are intentionally allowed to propagate. Operator
    surfaces must treat those errors as fail-closed rather than converting them into
    a successful handoff.
    """

    report = _verify_evidence_manifest(manifest, workspace)
    _require_machine_false(report, "real_money_execution")
    return WorkspaceEvidenceHandoff(
        status="PASS",
        manifest_sha256=report["manifest_sha256"],
        file_count=report["file_count"],
        run_summary_count=report["run_summary_count"],
        fixed_evidence_set_complete=report["fixed_evidence_set_complete"],
    )


def create_nvda_handoff_template(
    release_zip: str | Path,
    *,
    expected_source_sha: str,
    expected_package_sha256: str,
) -> dict[str, Any]:
    """Create the canonical physical-NVDA evidence template without shelling out."""

    payload = _create_nvda_template(
        release_zip,
        expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    _require_machine_false(
        payload,
        "real_money_execution",
        "human_tested",
        "nvda_verified",
        "v1_ready",
    )
    return payload


def verify_nvda_handoff(
    release_zip: str | Path,
    evidence_path: str | Path,
    *,
    expected_source_sha: str,
    expected_package_sha256: str,
) -> NvdaEvidenceHandoff:
    """Validate human-supplied NVDA evidence through the canonical validator."""

    report = _validate_nvda_evidence(
        release_zip,
        evidence_path,
        expected_source_sha=expected_source_sha,
        expected_package_sha256=expected_package_sha256,
    )
    _require_machine_false(
        report,
        "real_money_execution",
        "human_tested",
        "nvda_verified",
        "v1_ready",
        "machine_verified_physical_execution",
    )
    if report.get("requires_owner_release_decision") is not True:
        raise ValueError(
            "canonical NVDA evidence result must require an owner release decision"
        )
    status = report["status"]
    if status not in {"PASS", "FAIL"}:
        raise ValueError("canonical NVDA evidence result has an invalid status")
    failed_checks = report["failed_checks"]
    if not isinstance(failed_checks, list) or not all(
        isinstance(item, str) for item in failed_checks
    ):
        raise ValueError("canonical NVDA evidence result has invalid failed_checks")
    return NvdaEvidenceHandoff(
        status=status,
        package_sha256=report["package_sha256"],
        source_sha=report["source_sha"],
        autosport_exe_sha256=report["autosport_exe_sha256"],
        failed_checks=tuple(failed_checks),
        requires_owner_release_decision=True,
    )
