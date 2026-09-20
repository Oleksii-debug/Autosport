from __future__ import annotations

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


def _sha(char: str) -> str:
    return char * 64


def _consumed_store(tmp_path):
    member = ExperimentFamilyMember(
        hypothesis_id="hyp-a",
        hypothesis_sha256=_sha("a"),
        semantic_variant_sha256=_sha("b"),
        candidate_label="candidate A",
    )
    plan = ExperimentFamilyPlan(
        family_id="family-valid-utf8-reset",
        research_protocol_id="protocol-1",
        protocol_sha256=_sha("c"),
        research_question_id="question-1",
        primary_metric="net_utility",
        direction=MetricDirection.HIGHER_IS_BETTER,
        control_kind=MultiplicityControlKind.FWER,
        method_id=ExperimentFamilyPlan.IMPLEMENTED_METHOD,
        stopping_rule="two predeclared batch looks",
        familywise_alpha=Decimal("0.05"),
        look_alpha_spend=(Decimal("0.005"), Decimal("0.005")),
        members=(member,),
        frozen_at="2026-09-20T03:00:00Z",
    )

    SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)
    store_path = tmp_path / "multiplicity.evidence"
    store = SequentialMultiplicityEvidenceStore.initialize_pristine(store_path, plan)
    store.append(
        SequentialLookEvidence(
            family_plan_sha256=plan.plan_sha256,
            member_authority_id=member.member_authority_id,
            hypothesis_id=member.hypothesis_id,
            experiment_id="exp-1",
            evaluation_bundle_id="bundle-1",
            evaluation_bundle_sha256=_sha("d"),
            look_index=1,
            observed_p_value=Decimal("0.5"),
            classification=ResearchOutcome.NULL,
            observed_at="2026-09-20T03:01:00Z",
            candidate_label=member.candidate_label,
        )
    )
    return store_path


@pytest.mark.parametrize("replacement", ["{}", "valid utf8 replacement"])
def test_valid_utf8_replacement_cannot_hide_consumed_store_from_rebootstrap(
    tmp_path, replacement: str
) -> None:
    store_path = _consumed_store(tmp_path)
    store_path.write_text(replacement, encoding="utf-8")
    (tmp_path / SequentialMultiplicityEvidenceStore.ENROLLMENT_FILE).unlink()
    (tmp_path / SequentialMultiplicityEvidenceStore.WORKSPACE_AUTHORITY_FILE).unlink()

    with pytest.raises(ValueError, match="rebootstrap"):
        SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)
