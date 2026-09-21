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


def _member(*, variant: str, label: str) -> ExperimentFamilyMember:
    return ExperimentFamilyMember(
        hypothesis_id="hypothesis-1",
        hypothesis_sha256=_sha("a"),
        semantic_variant_sha256=_sha(variant),
        candidate_label=label,
    )


def _plan(
    *,
    family_id: str,
    protocol_id: str,
    protocol_sha: str,
    variant: str,
    label: str,
    frozen_at: str,
) -> ExperimentFamilyPlan:
    return ExperimentFamilyPlan(
        family_id=family_id,
        research_protocol_id=protocol_id,
        protocol_sha256=_sha(protocol_sha),
        research_question_id="question-1",
        primary_metric="net_utility",
        direction=MetricDirection.HIGHER_IS_BETTER,
        control_kind=MultiplicityControlKind.FWER,
        method_id=ExperimentFamilyPlan.IMPLEMENTED_METHOD,
        stopping_rule="one frozen predeclared look",
        familywise_alpha=Decimal("0.05"),
        look_alpha_spend=(Decimal("0.01"),),
        members=(_member(variant=variant, label=label),),
        frozen_at=frozen_at,
    )


def test_protocol_revision_cannot_reset_consumed_multiplicity_without_new_authority(
    tmp_path,
) -> None:
    """Protocol/version hashes alone must not prove a new statistical opportunity.

    The first family consumes a real negative sequential look.  After observing that
    result, the caller changes the protocol/family IDs and the semantic-variant digest
    while keeping the same research question, hypothesis, metric, statistical method,
    stopping rule, and alpha contract.

    A fresh family must fail closed unless an independent canonical confirmation/trial
    authority proves this is genuinely a new pre-registered comparison opportunity.
    Caller-controlled identity re-hashing is not such proof.
    """

    SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)

    first_plan = _plan(
        family_id="family-1",
        protocol_id="protocol-1",
        protocol_sha="c",
        variant="b",
        label="candidate one",
        frozen_at="2026-09-21T00:00:00Z",
    )
    first_store = SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "family-one.json",
        first_plan,
    )
    first_member = first_plan.members[0]
    first_store.append(
        SequentialLookEvidence(
            family_plan_sha256=first_plan.plan_sha256,
            member_authority_id=first_member.member_authority_id,
            hypothesis_id=first_member.hypothesis_id,
            experiment_id="experiment-1",
            evaluation_bundle_id="bundle-1",
            evaluation_bundle_sha256=_sha("f"),
            look_index=1,
            observed_p_value=Decimal("0.50"),
            classification=ResearchOutcome.NEGATIVE,
            observed_at="2026-09-21T00:01:00Z",
            candidate_label=first_member.candidate_label,
        )
    )

    adaptive_reset = _plan(
        family_id="family-2",
        protocol_id="protocol-2",
        protocol_sha="d",
        variant="e",
        label="candidate renamed after negative result",
        frozen_at="2026-09-21T00:02:00Z",
    )

    with pytest.raises(ValueError):
        SequentialMultiplicityEvidenceStore.initialize_pristine(
            tmp_path / "family-two.json",
            adaptive_reset,
        )

    assert not (tmp_path / "family-two.json").exists()
