from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.research_multiplicity import (
    ExperimentFamilyMember,
    ExperimentFamilyPlan,
    MetricDirection,
    MultiplicityControlKind,
    SequentialMultiplicityEvidenceStore,
)


def _sha(char: str) -> str:
    return char * 64


def _member(*, label: str, variant: str) -> ExperimentFamilyMember:
    return ExperimentFamilyMember(
        hypothesis_id="hypothesis-1",
        hypothesis_sha256=_sha("a"),
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


def test_new_protocol_version_can_own_a_distinct_family(tmp_path) -> None:
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
        members=(_member(label="candidate two", variant="e"),),
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
