from __future__ import annotations

import pytest

from autosport.model_risk_register import (
    ModelRiskRegisterError,
    OperatorPriority,
    OperatorRiskRegisterView,
    OperatorRiskRow,
    RegisterEntryType,
    RiskRegisterEntry,
    RiskSeverity,
    RiskStatus,
    build_operator_risk_register_view,
)


def _critical_execution_blocker() -> RiskRegisterEntry:
    return RiskRegisterEntry(
        entry_id="risk-critical-origin",
        entry_type=RegisterEntryType.INCIDENT,
        severity=RiskSeverity.CRITICAL,
        status=RiskStatus.OPEN,
        title="Canonical critical incident",
        summary="Canonical evidence says execution must remain blocked.",
        detected_at="2026-09-21T17:00:00Z",
        updated_at="2026-09-21T17:01:00Z",
        evidence_refs=("evidence:canonical",),
        affected_authorities=("execution.supervised",),
        blocks_product_readiness=True,
        blocks_execution=True,
    )


def test_direct_operator_projection_cannot_hide_canonical_critical_blocker() -> None:
    canonical = _critical_execution_blocker()
    canonical_view = build_operator_risk_register_view(
        (canonical,),
        as_of="2026-09-21T17:02:00Z",
    )
    assert canonical_view.readiness_blocking_ids == (canonical.entry_id,)
    assert canonical_view.execution_blocking_ids == (canonical.entry_id,)
    assert canonical_view.critical_unresolved_count == 1

    forged_row = OperatorRiskRow(
        entry_id=canonical.entry_id,
        entry_sha256="a" * 64,
        entry_type=RegisterEntryType.INCIDENT,
        severity=RiskSeverity.LOW,
        status=RiskStatus.RESOLVED,
        priority=OperatorPriority.CLOSED,
        title="Fabricated resolved projection",
        summary="Caller claims the critical incident is resolved.",
        updated_at="2026-09-21T17:01:00Z",
        blocks_product_readiness=False,
        blocks_execution=False,
        evidence_count=0,
    )

    with pytest.raises(ModelRiskRegisterError):
        OperatorRiskRegisterView(
            as_of="2026-09-21T17:02:00Z",
            rows=(forged_row,),
            readiness_blocking_ids=(),
            execution_blocking_ids=(),
            unresolved_count=0,
            critical_unresolved_count=0,
        )
