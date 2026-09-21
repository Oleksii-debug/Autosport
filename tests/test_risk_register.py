import json

import pytest

from autosport.risk_register import (
    RiskEvidence,
    RiskKind,
    RiskRecord,
    RiskRegisterSnapshot,
    RiskSeverity,
    RiskStatus,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def evidence(*, digest: str = DIGEST_A) -> RiskEvidence:
    return RiskEvidence("paper.execution", "receipt-17", digest)


def record(
    risk_id: str = "risk-1",
    *,
    severity: RiskSeverity = RiskSeverity.HIGH,
    status: RiskStatus = RiskStatus.OPEN,
    evidence_items: tuple[RiskEvidence, ...] = (),
    first: str = "2026-09-21T08:00:00Z",
    updated: str = "2026-09-21T08:10:00Z",
) -> RiskRecord:
    return RiskRecord(
        risk_id=risk_id,
        kind=RiskKind.INCIDENT,
        severity=severity,
        status=status,
        title="Provider feed gap",
        summary="Feed stopped producing lawful observations.",
        owner="operator",
        first_observed_at=first,
        updated_at=updated,
        operator_action="Keep execution disabled and inspect provider health.",
        mitigation="Restart isolated collector." if status in {RiskStatus.MITIGATING, RiskStatus.RESOLVED} else "",
        evidence=evidence_items,
    )


def test_snapshot_is_deterministically_ordered_by_unresolved_severity_and_id() -> None:
    low = record("risk-z", severity=RiskSeverity.LOW)
    critical = record("risk-b", severity=RiskSeverity.CRITICAL)
    critical_a = record("risk-a", severity=RiskSeverity.CRITICAL)
    resolved = record(
        "risk-0",
        severity=RiskSeverity.CRITICAL,
        status=RiskStatus.RESOLVED,
        evidence_items=(evidence(),),
    )
    snapshot = RiskRegisterSnapshot.build(
        "2026-09-21T09:00:00Z", [resolved, low, critical, critical_a]
    )
    assert tuple(item.risk_id for item in snapshot.records) == (
        "risk-a", "risk-b", "risk-z", "risk-0"
    )
    assert snapshot.unresolved_count == 3
    assert snapshot.critical_unresolved_count == 2


def test_json_and_digest_are_stable_for_equivalent_input_order() -> None:
    first = RiskRegisterSnapshot.build(
        "2026-09-21T09:00:00Z", [record("risk-b"), record("risk-a")]
    )
    second = RiskRegisterSnapshot.build(
        "2026-09-21T09:00:00Z", [record("risk-a"), record("risk-b")]
    )
    assert first.to_json() == second.to_json()
    assert first.sha256 == second.sha256
    assert json.loads(first.to_json())["truth_boundary"] == "operator_diagnostics_only"


def test_operator_projection_is_explicit_and_screen_reader_linear() -> None:
    snapshot = RiskRegisterSnapshot.build("2026-09-21T09:00:00Z", [record()])
    assert snapshot.operator_rows() == (
        "risk=risk-1 | kind=incident | severity=high | status=open | owner=operator | "
        "action=Keep execution disabled and inspect provider health. | evidence=0 | "
        "title=Provider feed gap",
    )


def test_resolved_risk_requires_positive_evidence() -> None:
    with pytest.raises(ValueError, match="resolved risk requires evidence"):
        record(status=RiskStatus.RESOLVED)


def test_resolved_risk_with_evidence_is_allowed_without_operator_action() -> None:
    item = RiskRecord(
        risk_id="model-1",
        kind=RiskKind.MODEL_RISK,
        severity=RiskSeverity.MEDIUM,
        status=RiskStatus.RESOLVED,
        title="Calibration regression",
        summary="Frozen challenger failed calibration gate.",
        owner="science",
        first_observed_at="2026-09-21T07:00:00Z",
        updated_at="2026-09-21T08:00:00Z",
        operator_action="",
        mitigation="Challenger retired; champion retained.",
        evidence=(evidence(),),
    )
    row = RiskRegisterSnapshot.build("2026-09-21T09:00:00Z", [item]).operator_rows()[0]
    assert "action=none" in row
    assert "evidence=1" in row


def test_future_update_is_rejected() -> None:
    item = record(updated="2026-09-21T09:00:01Z")
    with pytest.raises(ValueError, match="future"):
        RiskRegisterSnapshot.build("2026-09-21T09:00:00Z", [item])


def test_update_before_first_observation_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not precede"):
        record(first="2026-09-21T08:11:00Z", updated="2026-09-21T08:10:00Z")


def test_duplicate_risk_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate risk_id"):
        RiskRegisterSnapshot.build("2026-09-21T09:00:00Z", [record(), record()])


def test_conflicting_evidence_binding_across_records_is_rejected() -> None:
    left = record("risk-left", evidence_items=(evidence(digest=DIGEST_A),))
    right = record("risk-right", evidence_items=(evidence(digest=DIGEST_B),))
    with pytest.raises(ValueError, match="different digests"):
        RiskRegisterSnapshot.build("2026-09-21T09:00:00Z", [left, right])


def test_duplicate_evidence_identity_inside_record_is_rejected_even_same_digest() -> None:
    item = evidence()
    with pytest.raises(ValueError, match="duplicate evidence identities"):
        record(evidence_items=(item, item))


@pytest.mark.parametrize("bad_digest", ["A" * 64, "a" * 63, "g" * 64, " a" * 32])
def test_evidence_digest_is_exact_lowercase_sha256(bad_digest: str) -> None:
    with pytest.raises(ValueError, match="64 lowercase"):
        evidence(digest=bad_digest)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-21T08:00:00+00:00",
        "2026-09-21T08:00Z",
        "2026-09-21T10:00:00+02:00",
        " 2026-09-21T08:00:00Z",
    ],
)
def test_timestamp_requires_canonical_utc_seconds(timestamp: str) -> None:
    with pytest.raises(ValueError):
        record(first=timestamp)


def test_schema_types_fail_closed_instead_of_coercing_strings_or_enums() -> None:
    with pytest.raises(ValueError, match="kind must be RiskKind"):
        RiskRecord(
            risk_id="risk-1",
            kind="incident",  # type: ignore[arg-type]
            severity=RiskSeverity.HIGH,
            status=RiskStatus.OPEN,
            title="x",
            summary="x",
            owner="x",
            first_observed_at="2026-09-21T08:00:00Z",
            updated_at="2026-09-21T08:00:00Z",
            operator_action="x",
            mitigation="",
        )


def test_snapshot_does_not_mutate_input_sequence() -> None:
    items = [record("risk-b"), record("risk-a")]
    original_ids = [item.risk_id for item in items]
    RiskRegisterSnapshot.build("2026-09-21T09:00:00Z", items)
    assert [item.risk_id for item in items] == original_ids


def test_evidence_order_is_canonical_for_digest_stability() -> None:
    a = RiskEvidence("z.authority", "e-2", "c" * 64)
    b = RiskEvidence("a.authority", "e-1", "d" * 64)
    left = record("risk-order", evidence_items=(a, b))
    right = record("risk-order", evidence_items=(b, a))
    first = RiskRegisterSnapshot.build("2026-09-21T09:00:00Z", [left])
    second = RiskRegisterSnapshot.build("2026-09-21T09:00:00Z", [right])
    assert first.sha256 == second.sha256
    assert tuple(item.authority_family for item in left.evidence) == (
        "a.authority", "z.authority"
    )


def test_build_rejects_non_record_before_sorting() -> None:
    with pytest.raises(ValueError, match="exact RiskRecord"):
        RiskRegisterSnapshot.build(
            "2026-09-21T09:00:00Z", [object()]  # type: ignore[list-item]
        )


def test_mitigating_risk_requires_mitigation_text() -> None:
    with pytest.raises(ValueError, match="mitigation must not be empty"):
        RiskRecord(
            risk_id="risk-mitigating",
            kind=RiskKind.INCIDENT,
            severity=RiskSeverity.HIGH,
            status=RiskStatus.MITIGATING,
            title="x",
            summary="x",
            owner="x",
            first_observed_at="2026-09-21T08:00:00Z",
            updated_at="2026-09-21T08:00:00Z",
            operator_action="x",
            mitigation="",
        )


def test_direct_snapshot_constructor_cannot_bypass_canonical_order() -> None:
    direct = RiskRegisterSnapshot(
        generated_at="2026-09-21T09:00:00Z",
        records=(
            record("risk-z", severity=RiskSeverity.LOW),
            record("risk-a", severity=RiskSeverity.CRITICAL),
        ),
    )
    built = RiskRegisterSnapshot.build(
        "2026-09-21T09:00:00Z",
        [
            record("risk-a", severity=RiskSeverity.CRITICAL),
            record("risk-z", severity=RiskSeverity.LOW),
        ],
    )
    assert direct.to_json() == built.to_json()
    assert direct.sha256 == built.sha256
    assert tuple(item.risk_id for item in direct.records) == ("risk-a", "risk-z")
