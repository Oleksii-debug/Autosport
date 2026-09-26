from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

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
from autosport.research_multiplicity_family_close import (
    MultiplicityFamilyCloseError,
    MultiplicityFamilyCloseEvidence,
    derive_multiplicity_family_close,
    require_current_multiplicity_family_close,
)
from autosport.scientific_registry import ResearchOutcome


def _sha(char: str) -> str:
    return char * 64


def _member(
    label: str,
    *,
    hypothesis: str,
    variant: str,
) -> ExperimentFamilyMember:
    return ExperimentFamilyMember(
        hypothesis_id=f"hyp-{hypothesis}",
        hypothesis_sha256=_sha(hypothesis),
        semantic_variant_sha256=_sha(variant),
        candidate_label=label,
    )


def _plan() -> ExperimentFamilyPlan:
    return ExperimentFamilyPlan(
        family_id="family-close-1",
        research_protocol_id="protocol-close-1",
        protocol_sha256=_sha("c"),
        research_question_id="question-close-1",
        primary_metric="net_utility",
        direction=MetricDirection.HIGHER_IS_BETTER,
        control_kind=MultiplicityControlKind.FWER,
        method_id=ExperimentFamilyPlan.IMPLEMENTED_METHOD,
        stopping_rule="two predeclared looks",
        familywise_alpha=Decimal("0.05"),
        look_alpha_spend=(Decimal("0.005"), Decimal("0.005")),
        members=(
            _member("candidate A", hypothesis="a", variant="b"),
            _member("candidate B", hypothesis="d", variant="e"),
        ),
        frozen_at="2026-09-24T01:00:00Z",
    )


def _look(
    plan: ExperimentFamilyPlan,
    member: ExperimentFamilyMember,
    *,
    index: int,
    p: str,
    classification: ResearchOutcome = ResearchOutcome.NULL,
    suffix: int,
) -> SequentialLookEvidence:
    return SequentialLookEvidence(
        family_plan_sha256=plan.plan_sha256,
        member_authority_id=member.member_authority_id,
        hypothesis_id=member.hypothesis_id,
        experiment_id=f"experiment-{suffix}",
        evaluation_bundle_id=f"bundle-{suffix}",
        evaluation_bundle_sha256=f"{suffix:064x}",
        look_index=index,
        observed_p_value=Decimal(p),
        classification=classification,
        observed_at=f"2026-09-24T01:{suffix:02d}:00Z",
        candidate_label=member.candidate_label,
    )


@pytest.fixture
def family_store(tmp_path):
    SequentialMultiplicityEvidenceStore.initialize_workspace(tmp_path)
    plan = _plan()
    store = SequentialMultiplicityEvidenceStore.initialize_pristine(
        tmp_path / "multiplicity.json",
        plan,
    )
    return plan, store


def test_family_close_rejects_missing_predeclared_member(family_store):
    plan, store = family_store
    store.append(
        _look(
            plan,
            plan.members[0],
            index=1,
            p="0.8",
            classification=ResearchOutcome.NEGATIVE,
            suffix=1,
        )
    )

    with pytest.raises(MultiplicityFamilyCloseError, match="no durable evidence"):
        derive_multiplicity_family_close(store)


def test_family_close_rejects_nonterminal_member(family_store):
    plan, store = family_store
    store.append(_look(plan, plan.members[0], index=1, p="0.5", suffix=1))
    store.append(
        _look(
            plan,
            plan.members[1],
            index=1,
            p="0.9",
            classification=ResearchOutcome.NEGATIVE,
            suffix=2,
        )
    )

    with pytest.raises(MultiplicityFamilyCloseError, match="not terminal"):
        derive_multiplicity_family_close(store)


def test_complete_family_close_is_deterministic_and_non_authorizing(family_store):
    plan, store = family_store
    store.append(
        _look(
            plan,
            plan.members[0],
            index=1,
            p="0.9",
            classification=ResearchOutcome.NEGATIVE,
            suffix=1,
        )
    )
    store.append(_look(plan, plan.members[1], index=1, p="0.001", suffix=2))

    first = derive_multiplicity_family_close(store)
    second = derive_multiplicity_family_close(store)

    assert first == second
    assert first.evidence_sha256 == second.evidence_sha256
    assert first.family_plan_sha256 == plan.plan_sha256
    assert first.record_count == 2
    assert len(first.terminal_members) == len(plan.members)
    assert all(
        member.decision is not SequentialDecision.CONTINUE
        for member in first.terminal_members
    )
    assert first.promotion_authorized is False
    assert require_current_multiplicity_family_close(store, first) == first


def test_family_close_survives_restart_with_same_exact_state(family_store):
    plan, store = family_store
    for suffix, member in enumerate(plan.members, start=1):
        store.append(
            _look(
                plan,
                member,
                index=1,
                p="0.9",
                classification=ResearchOutcome.INCONCLUSIVE,
                suffix=suffix,
            )
        )
    evidence = derive_multiplicity_family_close(store)

    reopened = SequentialMultiplicityEvidenceStore(store.path)
    assert derive_multiplicity_family_close(reopened) == evidence
    assert require_current_multiplicity_family_close(reopened, evidence) == evidence


def test_payload_roundtrip_is_strict_and_digest_bound(family_store):
    plan, store = family_store
    for suffix, member in enumerate(plan.members, start=1):
        store.append(
            _look(
                plan,
                member,
                index=1,
                p="0.9",
                classification=ResearchOutcome.HARMFUL,
                suffix=suffix,
            )
        )
    evidence = derive_multiplicity_family_close(store)
    payload = evidence.to_payload()
    assert MultiplicityFamilyCloseEvidence.from_payload(payload) == evidence

    tampered = json.loads(json.dumps(payload))
    tampered["terminal_members"][0]["decision"] = SequentialDecision.REJECT_NULL.value
    with pytest.raises(ValueError, match="identity mismatch"):
        MultiplicityFamilyCloseEvidence.from_payload(tampered)


def test_caller_recomputed_forgery_is_rejected_by_store_reresolution(family_store):
    plan, store = family_store
    for suffix, member in enumerate(plan.members, start=1):
        store.append(
            _look(
                plan,
                member,
                index=1,
                p="0.9",
                classification=ResearchOutcome.NEGATIVE,
                suffix=suffix,
            )
        )
    evidence = derive_multiplicity_family_close(store)
    forged_terminal = replace(
        evidence.terminal_members[0],
        decision=SequentialDecision.REJECT_NULL,
    )
    forged = replace(
        evidence,
        terminal_members=(forged_terminal, *evidence.terminal_members[1:]),
    )

    assert forged.evidence_sha256 != evidence.evidence_sha256
    with pytest.raises(MultiplicityFamilyCloseError, match="does not match"):
        require_current_multiplicity_family_close(store, forged)


def test_cosmetic_label_and_json_rewrite_preserve_family_close_identity(family_store):
    plan, store = family_store
    for suffix, member in enumerate(plan.members, start=1):
        store.append(
            _look(
                plan,
                member,
                index=1,
                p="0.9",
                classification=ResearchOutcome.NEGATIVE,
                suffix=suffix,
            )
        )
    evidence = derive_multiplicity_family_close(store)

    state = json.loads(store.path.read_text(encoding="utf-8"))
    renamed_member = state["plan"]["members"][0]
    renamed_member["candidate_label"] = "cosmetic candidate alias"
    renamed_authority = renamed_member["member_authority_id"]
    for record in state["records"]:
        if record["member_authority_id"] == renamed_authority:
            record["candidate_label"] = "cosmetic candidate alias"
    store.path.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    reopened = SequentialMultiplicityEvidenceStore(store.path)
    assert reopened.plan.plan_sha256 == plan.plan_sha256
    assert derive_multiplicity_family_close(reopened) == evidence
    assert require_current_multiplicity_family_close(reopened, evidence) == evidence


def test_authoritative_look_change_invalidates_prior_family_close(family_store):
    plan, store = family_store
    for suffix, member in enumerate(plan.members, start=1):
        store.append(
            _look(
                plan,
                member,
                index=1,
                p="0.9",
                classification=ResearchOutcome.NEGATIVE,
                suffix=suffix,
            )
        )
    evidence = derive_multiplicity_family_close(store)

    state = json.loads(store.path.read_text(encoding="utf-8"))
    raw = state["records"][0]
    changed = SequentialLookEvidence(
        family_plan_sha256=raw["family_plan_sha256"],
        member_authority_id=raw["member_authority_id"],
        hypothesis_id=raw["hypothesis_id"],
        experiment_id=raw["experiment_id"],
        evaluation_bundle_id=raw["evaluation_bundle_id"],
        evaluation_bundle_sha256=raw["evaluation_bundle_sha256"],
        look_index=raw["look_index"],
        observed_p_value=Decimal(raw["observed_p_value"]),
        classification=ResearchOutcome(raw["classification"]),
        observed_at="2026-09-24T01:59:00Z",
        candidate_label=raw["candidate_label"],
    )
    state["records"][0] = changed.to_payload()
    store.path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    reopened = SequentialMultiplicityEvidenceStore(store.path)
    with pytest.raises(MultiplicityFamilyCloseError, match="does not match"):
        require_current_multiplicity_family_close(reopened, evidence)


def test_class_read_state_rebind_cannot_mint_family_close(
    family_store,
    monkeypatch,
):
    plan, store = family_store
    for suffix, member in enumerate(plan.members, start=1):
        store.append(
            _look(
                plan,
                member,
                index=1,
                p="0.9",
                classification=ResearchOutcome.NEGATIVE,
                suffix=suffix,
            )
        )

    complete_evidence = derive_multiplicity_family_close(store)
    canonical_read = vars(SequentialMultiplicityEvidenceStore)["_read_state"]
    forged_loaded = canonical_read(store)

    durable_state = json.loads(store.path.read_text(encoding="utf-8"))
    durable_state["records"] = []
    store.path.write_text(
        json.dumps(
            durable_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    with pytest.raises(MultiplicityFamilyCloseError, match="no durable evidence"):
        derive_multiplicity_family_close(store)

    forged_calls: list[str] = []

    def forged_read(_store):
        forged_calls.append("forged _read_state")
        return forged_loaded

    monkeypatch.setattr(
        SequentialMultiplicityEvidenceStore,
        "_read_state",
        forged_read,
    )
    with pytest.raises(
        MultiplicityFamilyCloseError,
        match="store read dispatch changed",
    ):
        derive_multiplicity_family_close(store)
    with pytest.raises(
        MultiplicityFamilyCloseError,
        match="store read dispatch changed",
    ):
        require_current_multiplicity_family_close(store, complete_evidence)

    assert forged_calls == []


def test_class_enrollment_validator_rebind_cannot_mint_close_from_unenrolled_copy(
    family_store,
    monkeypatch,
):
    plan, store = family_store
    for suffix, member in enumerate(plan.members, start=1):
        store.append(
            _look(
                plan,
                member,
                index=1,
                p="0.9",
                classification=ResearchOutcome.NEGATIVE,
                suffix=suffix,
            )
        )

    complete_evidence = derive_multiplicity_family_close(store)
    copied_path = store.path.with_name("copied-multiplicity.json")
    copied_path.write_bytes(store.path.read_bytes())

    with pytest.raises(ValueError, match="enrollment"):
        SequentialMultiplicityEvidenceStore(
            copied_path,
            workspace_root=store.workspace_root,
        )

    forged_calls: list[str] = []

    def forged_validate(_store, _plan):
        forged_calls.append("forged enrollment validation")

    monkeypatch.setattr(
        SequentialMultiplicityEvidenceStore,
        "_validate_workspace_enrollment",
        forged_validate,
    )
    forged_store = SequentialMultiplicityEvidenceStore(
        copied_path,
        workspace_root=store.workspace_root,
    )
    assert forged_calls == ["forged enrollment validation"]
    forged_calls.clear()

    with pytest.raises(
        MultiplicityFamilyCloseError,
        match="store enrollment dispatch changed",
    ):
        derive_multiplicity_family_close(forged_store)
    with pytest.raises(
        MultiplicityFamilyCloseError,
        match="store enrollment dispatch changed",
    ):
        require_current_multiplicity_family_close(
            forged_store,
            complete_evidence,
        )

    assert forged_calls == []


def test_store_subclass_cannot_override_positive_derivation(family_store):
    plan, store = family_store
    for suffix, member in enumerate(plan.members, start=1):
        store.append(
            _look(
                plan,
                member,
                index=1,
                p="0.9",
                classification=ResearchOutcome.NEGATIVE,
                suffix=suffix,
            )
        )

    class ForgedStore(SequentialMultiplicityEvidenceStore):
        pass

    forged = ForgedStore(store.path)
    with pytest.raises(TypeError, match="canonical"):
        derive_multiplicity_family_close(forged)


def test_exact_instance_method_mutation_is_ignored_by_fresh_reresolution(family_store):
    plan, store = family_store
    for suffix, member in enumerate(plan.members, start=1):
        store.append(
            _look(
                plan,
                member,
                index=1,
                p="0.9",
                classification=ResearchOutcome.NEGATIVE,
                suffix=suffix,
            )
        )
    expected = derive_multiplicity_family_close(store)

    store._read_state = lambda: (_ for _ in ()).throw(
        RuntimeError("forged instance dispatch")
    )
    assert derive_multiplicity_family_close(store) == expected
    assert require_current_multiplicity_family_close(store, expected) == expected
