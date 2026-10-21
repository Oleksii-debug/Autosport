from __future__ import annotations

import pytest

import autosport.operator_evidence_handoff as handoff


class _ForgedWorkspaceHandoff(handoff.WorkspaceEvidenceHandoff):
    def assert_product_issued(self) -> None:
        return None


class _ForgedNvdaHandoff(handoff.NvdaEvidenceHandoff):
    def assert_product_issued(self) -> None:
        return None


def test_workspace_handoff_subclass_cannot_bypass_product_issuance():
    forged = _ForgedWorkspaceHandoff(
        _status="PASS",
        _manifest_sha256="a" * 64,
        _file_count=1,
        _run_summary_count=1,
        _fixed_evidence_set_complete=True,
    )

    with pytest.raises(
        handoff.OperatorEvidenceHandoffAuthorityError,
        match="not issued",
    ):
        _ = forged.status


def test_nvda_handoff_subclass_cannot_bypass_product_issuance():
    forged = _ForgedNvdaHandoff(
        _status="PASS",
        _package_sha256="b" * 64,
        _source_sha="c" * 40,
        _autosport_exe_sha256="d" * 64,
        _failed_checks=(),
    )

    with pytest.raises(
        handoff.OperatorEvidenceHandoffAuthorityError,
        match="not issued",
    ):
        _ = forged.status
