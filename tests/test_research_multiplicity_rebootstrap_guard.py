from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.research_multiplicity import (
    ExperimentFamilyMember,
    ExperimentFamilyPlan,
    MetricDirection,
    MultiplicityControlKind,
    SequentialLookEvidence,
    SequentialMultiplicityEvidenceStore,
)
from autosport.scientific_registry import ResearchOutcome
from autosport.workspace_lock import WorkspaceEconomicLock


def test_parseable_plan_shape_corruption_cannot_reset_consumed_workspace(tmp_path) -> None:
    member = ExperimentFamilyMember(
        hypothesis_id="hyp-a",
        hypothesis_sha256="a" * 64,
        semantic_variant_sha256="b" * 64,
        candidate_label="candidate-a",
    )
    plan = ExperimentFamilyPlan(
        family_id="family-a",
        research_protocol_id="protocol-a",
        protocol_sha256="c" * 64,
        research_question_id="question-a",
        primary_metric="net_utility",
        direction=MetricDirection.HIGHER_IS_BETTER,
        control_kind=MultiplicityControlKind.FWER,
        method_id=ExperimentFamilyPlan.IMPLEMENTED_METHOD,
        stopping_rule="two frozen looks",
        familywise_alpha=Decimal("0.05"),
        look_alpha_spend=(Decimal("0.005"), Decimal("0.005")),
        members=(member,),
        frozen_at="2026-09-20T03:00:00Z",
    )

    SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)
    store_path = tmp_path / "consumed.evidence"
    store = SequentialMultiplicityEvidenceStore.initialize_pristine(store_path, plan)
    store.append(
        SequentialLookEvidence(
            family_plan_sha256=plan.plan_sha256,
            member_authority_id=member.member_authority_id,
            hypothesis_id=member.hypothesis_id,
            experiment_id="experiment-a-1",
            evaluation_bundle_id="bundle-a-1",
            evaluation_bundle_sha256="d" * 64,
            look_index=1,
            observed_p_value=Decimal("0.5"),
            classification=ResearchOutcome.NULL,
            observed_at="2026-09-20T03:01:00Z",
            candidate_label=member.candidate_label,
        )
    )

    raw = json.loads(store_path.read_text(encoding="utf-8"))
    raw["plan"] = None
    store_path.write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / SequentialMultiplicityEvidenceStore.ENROLLMENT_FILE).unlink()
    (tmp_path / SequentialMultiplicityEvidenceStore.WORKSPACE_AUTHORITY_FILE).unlink()

    with pytest.raises(ValueError, match="recognizable multiplicity evidence is invalid"):
        SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)


def test_bootstrap_discovery_does_not_read_workspace_economic_lock(
    tmp_path, monkeypatch
) -> None:
    lock_path = tmp_path / WorkspaceEconomicLock.FILE_NAME
    lock_path.write_bytes(b"\0")
    original_read_text = Path.read_text

    def _windows_locked_read(path: Path, *args, **kwargs):
        if path.name == WorkspaceEconomicLock.FILE_NAME:
            raise PermissionError("simulated Windows lock sharing denial")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _windows_locked_read)

    assert (
        SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)
        == tmp_path.resolve(strict=False)
    )

