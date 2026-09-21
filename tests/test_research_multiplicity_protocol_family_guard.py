from __future__ import annotations

from dataclasses import replace
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


def _member(
    *,
    label: str,
    variant: str,
    hypothesis_id: str = "hypothesis-1",
    hypothesis_sha: str = "a",
) -> ExperimentFamilyMember:
    return ExperimentFamilyMember(
        hypothesis_id=hypothesis_id,
        hypothesis_sha256=_sha(hypothesis_sha),
        semantic_variant_sha256=_sha(variant),
        candidate_label=label,
    )


def _plan(member: ExperimentFamilyMember) -> ExperimentFamilyPlan:
    return ExperimentFamilyPlan(
        family_id="family-display-1",
        research_protocol_id="protocol-1",
        protocol_sha256=_sha("c"),
        research_question_id="question-1",
        primary_metric="net_utility",
        direction=MetricDirection.HIGHER_IS_BETTER,
        control_kind=MultiplicityControlKind.FWER,
        method_id=ExperimentFamilyPlan.IMPLEMENTED_METHOD,
        stopping_rule="one frozen predeclared look",
        familywise_alpha=Decimal("0.05"),
        look_alpha_spend=(Decimal("0.01"),),
        members=(member,),
        frozen_at="2026-09-21T00:00:00Z",
    )


def test_fresh_semantic_variant_cannot_mint_second_family_for_frozen_protocol(
    tmp_path,
) -> None:
    SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)
    first = _plan(_member(label="candidate one", variant="b"))
    SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "family-one.json", first
    )

    second = replace(
        first,
        family_id="family-display-2",
        members=(_member(label="candidate two", variant="d"),),
    )

    with pytest.raises(ValueError, match="ResearchProtocol is already bound"):
        SequentialMultiplicityEvidenceStore.initialize_pristine(
            tmp_path / "family-two.json", second
        )


def test_split_member_cannot_mint_second_family_for_same_protocol(tmp_path) -> None:
    SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)
    first = replace(
        _plan(_member(label="candidate one", variant="b")),
        members=(
            _member(label="candidate one", variant="b"),
            _member(label="candidate two", variant="d"),
        ),
    )
    SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "family-one.json", first
    )

    split = replace(
        first,
        family_id="family-split",
        members=(_member(label="candidate three", variant="e"),),
    )

    with pytest.raises(ValueError, match="ResearchProtocol is already bound"):
        SequentialMultiplicityEvidenceStore.initialize_pristine(
            tmp_path / "family-split.json", split
        )


def test_protocol_revision_cannot_premint_capacity_for_same_hypothesis(tmp_path) -> None:
    SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)
    first = _plan(_member(label="candidate one", variant="b"))
    SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "family-one.json", first
    )

    renamed = replace(
        first,
        family_id="family-display-2",
        research_protocol_id="protocol-2",
        protocol_sha256=_sha("d"),
        research_question_id="question-renamed",
        members=(_member(label="candidate renamed", variant="e"),),
        frozen_at="2026-09-21T00:01:00Z",
    )

    with pytest.raises(ValueError, match="hypothesis authority is already bound"):
        SequentialMultiplicityEvidenceStore.initialize_pristine(
            tmp_path / "family-two.json", renamed
        )

    assert not (tmp_path / "family-two.json").exists()


def test_protocol_revision_cannot_reset_capacity_after_negative_result(tmp_path) -> None:
    SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)
    first = _plan(_member(label="candidate one", variant="b"))
    first_store = SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "family-one.json", first
    )
    first_member = first.members[0]
    first_store.append(
        SequentialLookEvidence(
            family_plan_sha256=first.plan_sha256,
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

    adaptive_reset = replace(
        first,
        family_id="family-display-2",
        research_protocol_id="protocol-2",
        protocol_sha256=_sha("d"),
        members=(_member(label="candidate renamed after negative", variant="e"),),
        frozen_at="2026-09-21T00:02:00Z",
    )

    with pytest.raises(ValueError, match="hypothesis authority is already bound"):
        SequentialMultiplicityEvidenceStore.initialize_pristine(
            tmp_path / "family-two.json", adaptive_reset
        )

    assert not (tmp_path / "family-two.json").exists()


def test_new_protocol_with_distinct_hypothesis_authority_remains_eligible(
    tmp_path,
) -> None:
    SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)
    first = _plan(_member(label="candidate one", variant="b"))
    SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "family-one.json", first
    )

    second = replace(
        first,
        family_id="family-display-2",
        research_protocol_id="protocol-2",
        protocol_sha256=_sha("d"),
        research_question_id="question-2",
        members=(
            _member(
                label="candidate two",
                variant="e",
                hypothesis_id="hypothesis-2",
                hypothesis_sha="f",
            ),
        ),
        frozen_at="2026-09-21T00:02:00Z",
    )
    second_store = SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "family-two.json", second
    )

    assert second_store.plan.plan_sha256 == second.plan_sha256
    assert second_store.plan.research_protocol_id == "protocol-2"


def test_exact_existing_family_reopen_remains_idempotent(tmp_path) -> None:
    SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)
    plan = _plan(_member(label="candidate one", variant="b"))
    path = tmp_path / "family-one.json"
    first = SequentialMultiplicityEvidenceStore.initialize_pristine(path, plan)
    reopened = SequentialMultiplicityEvidenceStore.initialize_pristine(path, plan)

    assert reopened.plan.plan_sha256 == first.plan.plan_sha256
