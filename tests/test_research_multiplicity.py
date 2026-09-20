from __future__ import annotations

import json
from decimal import Decimal, localcontext

import pytest

from autosport.research_multiplicity import (
    ExperimentFamilyMember,
    ExperimentFamilyPlan,
    MetricDirection,
    MultiplicityControlKind,
    SequentialDecision,
    SequentialLookEvidence,
    SequentialMultiplicityEvidenceStore,
)
from autosport.scientific_registry import ResearchOutcome


def _sha(char: str) -> str:
    return char * 64


def _member(label: str, *, hypothesis: str = "a", variant: str = "b") -> ExperimentFamilyMember:
    return ExperimentFamilyMember(
        hypothesis_id=f"hyp-{hypothesis}",
        hypothesis_sha256=_sha(hypothesis),
        semantic_variant_sha256=_sha(variant),
        candidate_label=label,
    )


def _plan(
    *,
    members: tuple[ExperimentFamilyMember, ...] | None = None,
    spends: tuple[Decimal, ...] = (Decimal("0.005"), Decimal("0.005")),
) -> ExperimentFamilyPlan:
    return ExperimentFamilyPlan(
        family_id="family-2026-09",
        research_protocol_id="protocol-1",
        protocol_sha256=_sha("c"),
        research_question_id="question-1",
        primary_metric="net_utility",
        direction=MetricDirection.HIGHER_IS_BETTER,
        control_kind=MultiplicityControlKind.FWER,
        method_id=ExperimentFamilyPlan.IMPLEMENTED_METHOD,
        stopping_rule="exactly two predeclared batch looks; stop on rejection or terminal harm",
        familywise_alpha=Decimal("0.05"),
        look_alpha_spend=spends,
        members=members or (
            _member("candidate A", hypothesis="a", variant="b"),
            _member("candidate B", hypothesis="d", variant="e"),
        ),
        frozen_at="2026-09-20T03:00:00Z",
    )


def _look(
    plan: ExperimentFamilyPlan,
    member: ExperimentFamilyMember,
    *,
    index: int,
    p: str,
    classification: ResearchOutcome = ResearchOutcome.NULL,
    experiment_id: str | None = None,
    bundle_char: str = "f",
    bundle_id: str | None = None,
    label: str | None = None,
    observed_at: str | None = None,
) -> SequentialLookEvidence:
    return SequentialLookEvidence(
        family_plan_sha256=plan.plan_sha256,
        member_authority_id=member.member_authority_id,
        hypothesis_id=member.hypothesis_id,
        experiment_id=experiment_id or f"exp-{member.member_authority_id[:8]}-{index}",
        evaluation_bundle_id=bundle_id or f"bundle-{bundle_char}-{index}",
        evaluation_bundle_sha256=_sha(bundle_char),
        look_index=index,
        observed_p_value=Decimal(p),
        classification=classification,
        observed_at=observed_at or f"2026-09-20T03:0{index}:00Z",
        candidate_label=label or member.candidate_label,
    )


def test_repeated_peeking_cannot_create_unplanned_looks(tmp_path) -> None:
    plan = _plan()
    member = plan.members[0]
    store = SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "multiplicity.json", plan
    )

    first = store.append(_look(plan, member, index=1, p="0.5", bundle_char="1"))
    assert first.decision is SequentialDecision.CONTINUE
    assert first.alpha_boundary == Decimal("0.005")

    with pytest.raises(ValueError, match="next predeclared"):
        store.append(_look(plan, member, index=1, p="0.001", bundle_char="2"))
    with pytest.raises(ValueError, match="next predeclared"):
        store.append(_look(plan, member, index=3, p="0.001", bundle_char="3"))

    second = store.append(_look(plan, member, index=2, p="0.5", bundle_char="4"))
    assert second.decision is SequentialDecision.RETAIN_NULL
    with pytest.raises(ValueError, match="terminal"):
        store.append(_look(plan, member, index=3, p="0.0001", bundle_char="5"))


def test_candidate_rename_cannot_reset_family_or_member_identity(tmp_path) -> None:
    original = _member("candidate old", hypothesis="a", variant="b")
    renamed = _member("candidate renamed", hypothesis="a", variant="b")
    assert original.member_authority_id == renamed.member_authority_id

    original_plan = _plan(members=(original,))
    renamed_plan = _plan(members=(renamed,))
    assert original_plan.plan_sha256 == renamed_plan.plan_sha256

    store = SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "multiplicity.json", original_plan
    )
    store.append(
        _look(
            original_plan,
            original,
            index=1,
            p="0.5",
            bundle_char="6",
            label="candidate old",
        )
    )
    with pytest.raises(ValueError, match="next predeclared"):
        store.append(
            _look(
                renamed_plan,
                renamed,
                index=1,
                p="0.001",
                bundle_char="7",
                label="candidate renamed",
            )
        )


def test_ten_thousand_siblings_share_one_exact_fwer_budget() -> None:
    members = tuple(
        ExperimentFamilyMember(
            hypothesis_id=f"hyp-{index}",
            hypothesis_sha256=f"{index:064x}",
            semantic_variant_sha256=f"{index + 10001:064x}",
            candidate_label=f"candidate-{index}",
        )
        for index in range(1, 10001)
    )
    plan = _plan(members=members, spends=(Decimal("0.000005"),))
    assert len(plan.members) == 10000
    assert plan.control_kind is MultiplicityControlKind.FWER

    with pytest.raises(ValueError, match="exceeds familywise_alpha"):
        _plan(members=members, spends=(Decimal("0.0000051"),))


def test_fdr_is_explicit_but_not_mislabeled_as_implemented_fwer_method() -> None:
    with pytest.raises(ValueError, match="provides FWER control only"):
        ExperimentFamilyPlan(
            family_id="family-fdr",
            research_protocol_id="protocol-1",
            protocol_sha256=_sha("c"),
            research_question_id="question-1",
            primary_metric="net_utility",
            direction=MetricDirection.HIGHER_IS_BETTER,
            control_kind=MultiplicityControlKind.FDR,
            method_id=ExperimentFamilyPlan.IMPLEMENTED_METHOD,
            stopping_rule="predeclared",
            familywise_alpha=Decimal("0.05"),
            look_alpha_spend=(Decimal("0.005"),),
            members=(_member("a"),),
            frozen_at="2026-09-20T03:00:00Z",
        )


def test_family_split_or_merge_cannot_reuse_existing_store(tmp_path) -> None:
    path = tmp_path / "multiplicity.json"
    plan = _plan()
    SequentialMultiplicityEvidenceStore.initialize_pristine(path, plan)

    split_plan = _plan(members=(plan.members[0],))
    with pytest.raises(ValueError, match="another family plan"):
        SequentialMultiplicityEvidenceStore.initialize_pristine(path, split_plan)

    extra = _member("candidate C", hypothesis="8", variant="9")
    merged_plan = _plan(members=(*plan.members, extra))
    with pytest.raises(ValueError, match="another family plan"):
        SequentialMultiplicityEvidenceStore.initialize_pristine(path, merged_plan)


def test_negative_result_is_terminal_and_survives_restart(tmp_path) -> None:
    path = tmp_path / "multiplicity.json"
    plan = _plan()
    member = plan.members[0]
    store = SequentialMultiplicityEvidenceStore.initialize_pristine(path, plan)
    result = store.append(
        _look(
            plan,
            member,
            index=1,
            p="0.8",
            classification=ResearchOutcome.NEGATIVE,
            bundle_char="8",
        )
    )
    assert result.decision is SequentialDecision.TERMINAL_NEGATIVE

    reopened = SequentialMultiplicityEvidenceStore(path)
    persisted = reopened.assessments(member.member_authority_id)
    assert len(persisted) == 1
    assert persisted[0].evidence.classification is ResearchOutcome.NEGATIVE
    assert persisted[0].decision is SequentialDecision.TERMINAL_NEGATIVE

    with pytest.raises(ValueError, match="terminal"):
        reopened.append(_look(plan, member, index=2, p="0.001", bundle_char="9"))


def test_evaluation_evidence_cannot_be_reused_under_sibling_hypothesis(tmp_path) -> None:
    plan = _plan()
    first, second = plan.members
    store = SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "multiplicity.json", plan
    )
    store.append(_look(plan, first, index=1, p="0.5", bundle_char="a"))
    with pytest.raises(ValueError, match="already been consumed"):
        store.append(_look(plan, second, index=1, p="0.5", bundle_char="a"))


def test_family_identity_is_decimal_context_invariant() -> None:
    with localcontext() as ctx:
        ctx.prec = 6
        low_precision = _plan().plan_sha256
    with localcontext() as ctx:
        ctx.prec = 50
        high_precision = _plan().plan_sha256
    assert low_precision == high_precision


def test_significant_predeclared_look_is_terminal_rejection(tmp_path) -> None:
    plan = _plan()
    member = plan.members[0]
    store = SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "multiplicity.json", plan
    )
    result = store.append(_look(plan, member, index=1, p="0.005", bundle_char="b"))
    assert result.decision is SequentialDecision.REJECT_NULL
    assert result.terminal is True


def test_persisted_family_tamper_is_detected_on_restart(tmp_path) -> None:
    path = tmp_path / "multiplicity.json"
    plan = _plan()
    SequentialMultiplicityEvidenceStore.initialize_pristine(path, plan)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["plan"]["primary_metric"] = "cherry_picked_metric"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="plan identity mismatch"):
        SequentialMultiplicityEvidenceStore(path)


def test_significant_look_before_family_freeze_is_rejected(tmp_path) -> None:
    plan = _plan()
    member = plan.members[0]
    store = SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "multiplicity.json", plan
    )
    with pytest.raises(ValueError, match="predates the frozen"):
        store.append(
            _look(
                plan,
                member,
                index=1,
                p="0.001",
                bundle_char="c",
                observed_at="2026-09-20T02:59:59Z",
            )
        )


def test_same_evaluation_bundle_id_cannot_change_digest_after_restart(tmp_path) -> None:
    path = tmp_path / "multiplicity.json"
    plan = _plan()
    member = plan.members[0]
    store = SequentialMultiplicityEvidenceStore.initialize_pristine(path, plan)
    store.append(
        _look(
            plan,
            member,
            index=1,
            p="0.5",
            bundle_char="c",
            bundle_id="canonical-evaluation-1",
        )
    )

    reopened = SequentialMultiplicityEvidenceStore(path)
    with pytest.raises(ValueError, match="different immutable digest"):
        reopened.append(
            _look(
                plan,
                member,
                index=2,
                p="0.5",
                bundle_char="d",
                bundle_id="canonical-evaluation-1",
            )
        )


def test_hypothesis_id_tamper_changes_frozen_member_authority(tmp_path) -> None:
    path = tmp_path / "multiplicity.json"
    plan = _plan()
    SequentialMultiplicityEvidenceStore.initialize_pristine(path, plan)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["plan"]["members"][0]["hypothesis_id"] = "hyp-alias"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="family member authority identity mismatch"):
        SequentialMultiplicityEvidenceStore(path)


def test_sequential_look_timestamps_cannot_move_backwards(tmp_path) -> None:
    plan = _plan()
    member = plan.members[0]
    store = SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "multiplicity.json", plan
    )
    store.append(
        _look(
            plan,
            member,
            index=1,
            p="0.5",
            bundle_char="c",
            observed_at="2026-09-20T03:05:00Z",
        )
    )
    with pytest.raises(ValueError, match="timestamps must be monotonic"):
        store.append(
            _look(
                plan,
                member,
                index=2,
                p="0.5",
                bundle_char="d",
                observed_at="2026-09-20T03:04:00Z",
            )
        )
