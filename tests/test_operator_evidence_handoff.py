from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import autosport.operator_evidence_handoff as handoff


_SOURCE_SHA = "1" * 40
_PACKAGE_SHA = "2" * 64
_EXE_SHA = "3" * 64
_MANIFEST_SHA = "4" * 64


def test_workspace_manifest_delegates_to_canonical_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object]] = []

    def fake_verify(manifest: object, workspace: object) -> dict[str, object]:
        calls.append((manifest, workspace))
        return {
            "manifest_sha256": _MANIFEST_SHA,
            "file_count": 5,
            "run_summary_count": 1,
            "fixed_evidence_set_complete": True,
            "real_money_execution": False,
        }

    monkeypatch.setattr(handoff, "_verify_evidence_manifest", fake_verify)

    result = handoff.verify_workspace_manifest(Path("manifest.json"), Path("workspace"))

    assert calls == [(Path("manifest.json"), Path("workspace"))]
    assert result.status == "PASS"
    assert result.manifest_sha256 == _MANIFEST_SHA
    assert result.file_count == 5
    assert result.run_summary_count == 1
    assert result.fixed_evidence_set_complete is True
    assert result.real_money_execution is False
    assert result.human_tested is False
    assert result.nvda_verified is False
    assert result.whole_product_complete is False


def test_workspace_direct_construction_and_replace_cannot_mint_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged = handoff.WorkspaceEvidenceHandoff(
        _status="PASS",
        _manifest_sha256=_MANIFEST_SHA,
        _file_count=5,
        _run_summary_count=1,
        _fixed_evidence_set_complete=True,
    )
    with pytest.raises(
        handoff.OperatorEvidenceHandoffAuthorityError,
        match="not issued",
    ):
        _ = forged.status

    monkeypatch.setattr(
        handoff,
        "_verify_evidence_manifest",
        lambda *_args, **_kwargs: {
            "manifest_sha256": _MANIFEST_SHA,
            "file_count": 5,
            "run_summary_count": 1,
            "fixed_evidence_set_complete": True,
            "real_money_execution": False,
        },
    )
    issued = handoff.verify_workspace_manifest("manifest.json", "workspace")
    issued.assert_product_issued()
    assert issued.status == "PASS"

    cloned = replace(issued)
    with pytest.raises(
        handoff.OperatorEvidenceHandoffAuthorityError,
        match="not issued",
    ):
        _ = cloned.status

    replaced = replace(issued, _manifest_sha256="5" * 64)
    with pytest.raises(
        handoff.OperatorEvidenceHandoffAuthorityError,
        match="not issued",
    ):
        _ = replaced.manifest_sha256


def test_workspace_manifest_propagates_fail_closed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ValueError("manifest mismatch")

    monkeypatch.setattr(handoff, "_verify_evidence_manifest", fail)

    with pytest.raises(ValueError, match="manifest mismatch"):
        handoff.verify_workspace_manifest("manifest.json", "workspace")


def test_workspace_manifest_refuses_truth_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handoff,
        "_verify_evidence_manifest",
        lambda *_args, **_kwargs: {
            "manifest_sha256": _MANIFEST_SHA,
            "file_count": 4,
            "run_summary_count": 0,
            "fixed_evidence_set_complete": True,
            "real_money_execution": True,
        },
    )

    with pytest.raises(ValueError, match="real_money_execution"):
        handoff.verify_workspace_manifest("manifest.json", "workspace")


def test_nvda_template_delegates_and_preserves_machine_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, str, str]] = []
    template = {
        "candidate": {"source_sha": _SOURCE_SHA, "package_sha256": _PACKAGE_SHA},
        "checks": [],
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "v1_ready": False,
    }

    def fake_template(
        release_zip: object,
        *,
        expected_source_sha: str,
        expected_package_sha256: str,
    ) -> dict[str, object]:
        calls.append((release_zip, expected_source_sha, expected_package_sha256))
        return template

    monkeypatch.setattr(handoff, "_create_nvda_template", fake_template)

    result = handoff.create_nvda_handoff_template(
        Path("release.zip"),
        expected_source_sha=_SOURCE_SHA,
        expected_package_sha256=_PACKAGE_SHA,
    )

    assert result is template
    assert calls == [(Path("release.zip"), _SOURCE_SHA, _PACKAGE_SHA)]


def test_nvda_template_refuses_truth_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        handoff,
        "_create_nvda_template",
        lambda *_args, **_kwargs: {
            "real_money_execution": False,
            "human_tested": True,
            "nvda_verified": False,
            "v1_ready": False,
        },
    )

    with pytest.raises(ValueError, match="human_tested"):
        handoff.create_nvda_handoff_template(
            "release.zip",
            expected_source_sha=_SOURCE_SHA,
            expected_package_sha256=_PACKAGE_SHA,
        )


def _valid_nvda_report() -> dict[str, object]:
    return {
        "status": "FAIL",
        "package_sha256": _PACKAGE_SHA,
        "source_sha": _SOURCE_SHA,
        "autosport_exe_sha256": _EXE_SHA,
        "failed_checks": ["primary_tab_flow"],
        "requires_owner_release_decision": True,
        "machine_verified_physical_execution": False,
        "real_money_execution": False,
        "human_tested": False,
        "nvda_verified": False,
        "v1_ready": False,
    }


def test_nvda_verification_delegates_and_projects_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object, str, str]] = []

    def fake_validate(
        release_zip: object,
        evidence_path: object,
        *,
        expected_source_sha: str,
        expected_package_sha256: str,
    ) -> dict[str, object]:
        calls.append(
            (release_zip, evidence_path, expected_source_sha, expected_package_sha256)
        )
        return _valid_nvda_report()

    monkeypatch.setattr(handoff, "_validate_nvda_evidence", fake_validate)

    result = handoff.verify_nvda_handoff(
        Path("release.zip"),
        Path("nvda.json"),
        expected_source_sha=_SOURCE_SHA,
        expected_package_sha256=_PACKAGE_SHA,
    )

    assert calls == [
        (Path("release.zip"), Path("nvda.json"), _SOURCE_SHA, _PACKAGE_SHA)
    ]
    assert result.status == "FAIL"
    assert result.package_sha256 == _PACKAGE_SHA
    assert result.source_sha == _SOURCE_SHA
    assert result.autosport_exe_sha256 == _EXE_SHA
    assert result.failed_checks == ("primary_tab_flow",)
    assert result.requires_owner_release_decision is True
    assert result.real_money_execution is False
    assert result.human_tested is False
    assert result.nvda_verified is False
    assert result.whole_product_complete is False


def test_nvda_direct_construction_and_replace_cannot_mint_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged = handoff.NvdaEvidenceHandoff(
        _status="PASS",
        _package_sha256=_PACKAGE_SHA,
        _source_sha=_SOURCE_SHA,
        _autosport_exe_sha256=_EXE_SHA,
        _failed_checks=(),
    )
    with pytest.raises(
        handoff.OperatorEvidenceHandoffAuthorityError,
        match="not issued",
    ):
        _ = forged.status
    with pytest.raises(
        handoff.OperatorEvidenceHandoffAuthorityError,
        match="not issued",
    ):
        _ = forged.real_money_execution

    report = _valid_nvda_report()
    report["status"] = "PASS"
    report["failed_checks"] = []
    monkeypatch.setattr(
        handoff,
        "_validate_nvda_evidence",
        lambda *_args, **_kwargs: report,
    )
    issued = handoff.verify_nvda_handoff(
        "release.zip",
        "nvda.json",
        expected_source_sha=_SOURCE_SHA,
        expected_package_sha256=_PACKAGE_SHA,
    )
    issued.assert_product_issued()
    assert issued.status == "PASS"
    assert issued.failed_checks == ()

    cloned = replace(issued)
    with pytest.raises(
        handoff.OperatorEvidenceHandoffAuthorityError,
        match="not issued",
    ):
        _ = cloned.status

    replaced = replace(issued, _package_sha256="6" * 64)
    with pytest.raises(
        handoff.OperatorEvidenceHandoffAuthorityError,
        match="not issued",
    ):
        _ = replaced.status


@pytest.mark.parametrize(
    ("status", "failed_checks"),
    [
        ("PASS", ["primary_tab_flow"]),
        ("FAIL", []),
    ],
)
def test_nvda_verification_rejects_contradictory_status_and_failed_checks(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    failed_checks: list[str],
) -> None:
    report = _valid_nvda_report()
    report["status"] = status
    report["failed_checks"] = failed_checks
    monkeypatch.setattr(
        handoff,
        "_validate_nvda_evidence",
        lambda *_args, **_kwargs: report,
    )

    with pytest.raises(ValueError, match="status/failed_checks"):
        handoff.verify_nvda_handoff(
            "release.zip",
            "nvda.json",
            expected_source_sha=_SOURCE_SHA,
            expected_package_sha256=_PACKAGE_SHA,
        )


def test_nvda_verification_refuses_machine_physical_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _valid_nvda_report()
    report["machine_verified_physical_execution"] = True
    monkeypatch.setattr(handoff, "_validate_nvda_evidence", lambda *_args, **_kwargs: report)

    with pytest.raises(ValueError, match="machine_verified_physical_execution"):
        handoff.verify_nvda_handoff(
            "release.zip",
            "nvda.json",
            expected_source_sha=_SOURCE_SHA,
            expected_package_sha256=_PACKAGE_SHA,
        )


def test_nvda_verification_requires_owner_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _valid_nvda_report()
    report["requires_owner_release_decision"] = False
    monkeypatch.setattr(handoff, "_validate_nvda_evidence", lambda *_args, **_kwargs: report)

    with pytest.raises(ValueError, match="owner release decision"):
        handoff.verify_nvda_handoff(
            "release.zip",
            "nvda.json",
            expected_source_sha=_SOURCE_SHA,
            expected_package_sha256=_PACKAGE_SHA,
        )


def test_nvda_verification_propagates_canonical_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ValueError("candidate identity mismatch")

    monkeypatch.setattr(handoff, "_validate_nvda_evidence", fail)

    with pytest.raises(ValueError, match="candidate identity mismatch"):
        handoff.verify_nvda_handoff(
            "release.zip",
            "nvda.json",
            expected_source_sha=_SOURCE_SHA,
            expected_package_sha256=_PACKAGE_SHA,
        )
