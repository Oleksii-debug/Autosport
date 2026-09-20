from __future__ import annotations

import json
from decimal import Decimal

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
