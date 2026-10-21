from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from weakref import WeakKeyDictionary

from .evidence_export import verify_evidence_manifest as _verify_evidence_manifest
from .nvda_acceptance import create_template as _create_nvda_template
from .nvda_acceptance import validate_evidence as _validate_nvda_evidence


class OperatorEvidenceHandoffAuthorityError(ValueError):
    """Raised when a handoff result was not issued by the canonical adapter."""


@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True, eq=False)
class WorkspaceEvidenceHandoff:
    """Product-issued projection of canonical workspace-evidence verification.

    Public evidence properties fail closed unless this exact object was minted by
    verify_workspace_manifest(). Direct construction and dataclasses.replace() do
    not create canonical verification evidence.
    """

    _status: Literal["PASS"]
    _manifest_sha256: str
    _file_count: int
    _run_summary_count: int
    _fixed_evidence_set_complete: bool

    def assert_product_issued(self) -> None:
        _assert_workspace_handoff_product_issued(self)

    @property
    def status(self) -> Literal["PASS"]:
        _assert_workspace_handoff_product_issued(self)
        return self._status

    @property
    def manifest_sha256(self) -> str:
        _assert_workspace_handoff_product_issued(self)
        return self._manifest_sha256

    @property
    def file_count(self) -> int:
        _assert_workspace_handoff_product_issued(self)
        return self._file_count

    @property
    def run_summary_count(self) -> int:
        _assert_workspace_handoff_product_issued(self)
        return self._run_summary_count

    @property
    def fixed_evidence_set_complete(self) -> bool:
        _assert_workspace_handoff_product_issued(self)
        return self._fixed_evidence_set_complete

    @property
    def real_money_execution(self) -> Literal[False]:
        _assert_workspace_handoff_product_issued(self)
        return False

    @property
    def human_tested(self) -> Literal[False]:
        _assert_workspace_handoff_product_issued(self)
        return False

    @property
    def nvda_verified(self) -> Literal[False]:
        _assert_workspace_handoff_product_issued(self)
        return False

    @property
    def whole_product_complete(self) -> Literal[False]:
        _assert_workspace_handoff_product_issued(self)
        return False


@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True, eq=False)
class NvdaEvidenceHandoff:
    """Product-issued machine validation of human-supplied physical NVDA evidence.

    PASS means that the canonical validator accepted the supplied record for the
    exact release candidate. It never promotes human/NVDA/release truth.
    """

    _status: Literal["PASS", "FAIL"]
    _package_sha256: str
    _source_sha: str
    _autosport_exe_sha256: str
    _failed_checks: tuple[str, ...]

    def assert_product_issued(self) -> None:
        _assert_nvda_handoff_product_issued(self)

    @property
    def status(self) -> Literal["PASS", "FAIL"]:
        _assert_nvda_handoff_product_issued(self)
        return self._status

    @property
    def package_sha256(self) -> str:
        _assert_nvda_handoff_product_issued(self)
        return self._package_sha256

    @property
    def source_sha(self) -> str:
        _assert_nvda_handoff_product_issued(self)
        return self._source_sha

    @property
    def autosport_exe_sha256(self) -> str:
        _assert_nvda_handoff_product_issued(self)
        return self._autosport_exe_sha256

    @property
    def failed_checks(self) -> tuple[str, ...]:
        _assert_nvda_handoff_product_issued(self)
        return self._failed_checks

    @property
    def requires_owner_release_decision(self) -> Literal[True]:
        _assert_nvda_handoff_product_issued(self)
        return True

    @property
    def real_money_execution(self) -> Literal[False]:
        _assert_nvda_handoff_product_issued(self)
        return False

    @property
    def human_tested(self) -> Literal[False]:
        _assert_nvda_handoff_product_issued(self)
        return False

    @property
    def nvda_verified(self) -> Literal[False]:
        _assert_nvda_handoff_product_issued(self)
        return False

    @property
    def whole_product_complete(self) -> Literal[False]:
        _assert_nvda_handoff_product_issued(self)
        return False


def _workspace_handoff_state(
    value: WorkspaceEvidenceHandoff,
) -> tuple[object, ...]:
    return (
        value._status,
        value._manifest_sha256,
        value._file_count,
        value._run_summary_count,
        value._fixed_evidence_set_complete,
    )


def _nvda_handoff_state(value: NvdaEvidenceHandoff) -> tuple[object, ...]:
    return (
        value._status,
        value._package_sha256,
        value._source_sha,
        value._autosport_exe_sha256,
        value._failed_checks,
    )


_ISSUED_WORKSPACE_HANDOFFS: WeakKeyDictionary[
    WorkspaceEvidenceHandoff, tuple[object, ...]
] = WeakKeyDictionary()
_ISSUED_NVDA_HANDOFFS: WeakKeyDictionary[
    NvdaEvidenceHandoff, tuple[object, ...]
] = WeakKeyDictionary()


def _assert_workspace_handoff_product_issued(value: object) -> None:
    if type(value) is not WorkspaceEvidenceHandoff:
        raise OperatorEvidenceHandoffAuthorityError(
            "workspace evidence handoff was not issued by the canonical verifier adapter"
        )
    expected = _ISSUED_WORKSPACE_HANDOFFS.get(value)
    if expected is None or expected != _workspace_handoff_state(value):
        raise OperatorEvidenceHandoffAuthorityError(
            "workspace evidence handoff was not issued by the canonical verifier adapter"
        )


def _assert_nvda_handoff_product_issued(value: object) -> None:
    if type(value) is not NvdaEvidenceHandoff:
        raise OperatorEvidenceHandoffAuthorityError(
            "NVDA evidence handoff was not issued by the canonical verifier adapter"
        )
    expected = _ISSUED_NVDA_HANDOFFS.get(value)
    if expected is None or expected != _nvda_handoff_state(value):
        raise OperatorEvidenceHandoffAuthorityError(
            "NVDA evidence handoff was not issued by the canonical verifier adapter"
        )


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
    result = WorkspaceEvidenceHandoff(
        _status="PASS",
        _manifest_sha256=report["manifest_sha256"],
        _file_count=report["file_count"],
        _run_summary_count=report["run_summary_count"],
        _fixed_evidence_set_complete=report["fixed_evidence_set_complete"],
    )
    _ISSUED_WORKSPACE_HANDOFFS[result] = _workspace_handoff_state(result)
    return result


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
    if (status == "PASS") != (len(failed_checks) == 0):
        raise ValueError(
            "canonical NVDA evidence result has contradictory status/failed_checks"
        )
    result = NvdaEvidenceHandoff(
        _status=status,
        _package_sha256=report["package_sha256"],
        _source_sha=report["source_sha"],
        _autosport_exe_sha256=report["autosport_exe_sha256"],
        _failed_checks=tuple(failed_checks),
    )
    _ISSUED_NVDA_HANDOFFS[result] = _nvda_handoff_state(result)
    return result
