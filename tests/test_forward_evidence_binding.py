from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.decision_ledger import DecisionRecord, JsonlDecisionLedger
from autosport.forward_evidence_binding import (
    CAPTURE_PLAN_ACTION,
    bind_forward_evidence,
    register_forward_capture_plan,
)
from autosport.pre_evaluation_binding import (
    PreEvaluationDenominatorContext,
    ProviderMemberIdentity,
    bind_pre_evaluation_session,
)
from autosport.pre_evaluation_evidence import (
    CanonicalCandidateFacts,
    PreEvaluationEvidenceAuthority,
    PreEvaluationPolicy,
)
from autosport.scientific_registry import (
    EvaluationBundleRef,
    ForwardCapturePlan,
    ForwardCaptureSlot,
    ResearchProtocol,
    ScientificRegistry,
)
from autosport.strategy_experiment import ScientificProtocolBinding


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
T_PROTOCOL = "2026-09-20T00:00:00+00:00"
T_PLAN = "2026-09-20T00:30:00+00:00"
T_SLOT = "2026-09-20T01:00:00+00:00"
T_CLOSE = "2026-09-20T01:10:00+00:00"
T_EVAL = "2026-09-20T01:20:00+00:00"


def _ns(value: str) -> int:
    dt = datetime.fromisoformat(value).astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = dt - epoch
    return (
        delta.days * 86_400 * 1_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _binding(protocol_id: str = "protocol-1") -> ScientificProtocolBinding:
    return ScientificProtocolBinding(
        research_protocol_id=protocol_id,
        research_question_id="question-1",
        research_question_sha256=SHA_A,
        hypothesis_id="hypothesis-1",
        hypothesis_sha256=SHA_B,
        inclusion_criteria="predeclared forward slots",
        exclusion_criteria="invalid provenance",
        lawful_source_requirements="lawful provider evidence retained",
        causal_cutoff=T_CLOSE,
        evaluation_design="precommitted forward observation",
        feature_set_version="features-v1",
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split", "source split"),
        random_seed_policy="seed fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule="promote only if primary improves and guardrails pass",
        expected_artifacts=("forward capture plan", "evaluation bundle"),
        code_config_sha256=SHA_C,
        frozen_at_utc=T_PROTOCOL,
    )


def _protocol() -> ResearchProtocol:
    return ResearchProtocol(
        binding=_binding(),
        source_sha256=SHA_C,
        environment_sha256=SHA_D,
        dataset_manifest_sha256=SHA_A,
        available_at_utc=T_PROTOCOL,
    )


def _plan(protocol: ResearchProtocol, **changes: object) -> ForwardCapturePlan:
    values: dict[str, object] = {
        "capture_plan_id": "capture-plan-1",
        "run_id": "run-1",
        "session_id": "session-1",
        "campaign_id": "campaign-1",
        "research_protocol_id": protocol.record_id,
        "evaluation_bundle_id": "evaluation-1",
        "protocol_sha256": protocol.protocol_sha256,
        "window_open_utc": T_SLOT,
        "window_close_utc": T_CLOSE,
        "slots": (
            ForwardCaptureSlot("row-a", T_SLOT, 60),
            ForwardCaptureSlot("row-b", T_SLOT, 60),
        ),
        "created_at": T_PLAN,
    }
    values.update(changes)
    return ForwardCapturePlan(**values)


def _evaluation(
    protocol: ResearchProtocol,
    *,
    evaluation_bundle_id: str = "evaluation-1",
    protocol_sha256: str | None = None,
    created_at: str = T_EVAL,
) -> EvaluationBundleRef:
    return EvaluationBundleRef(
        evaluation_bundle_id=evaluation_bundle_id,
        bundle_sha256=SHA_A,
        evaluator_source_sha256=SHA_B,
        dataset_snapshot_id="dataset-1",
        protocol_sha256=protocol_sha256 or protocol.protocol_sha256,
        artifact_hashes=(SHA_C,),
        created_at=created_at,
    )


def _facts(row_key: str, *, observed_ns: int | None = None) -> CanonicalCandidateFacts:
    return CanonicalCandidateFacts(
        candidate_id=row_key,
        observed_at_ns=_ns("2026-09-20T01:00:10+00:00")
        if observed_ns is None
        else observed_ns,
        config_enabled=True,
        risk_required_micros=10,
        risk_available_micros=10,
        cost_estimate_micros=4,
        cost_limit_micros=4,
        source_authority_id=f"canonical:{row_key}",
        source_revision="provider-rev-1",
    )


def _bound(
    ledger: JsonlDecisionLedger,
    protocol: ResearchProtocol,
    *,
    campaign_id: str = "campaign-1",
    session_id: str = "session-1",
    row_keys: tuple[str, ...] = ("row-a", "row-b"),
    evaluated_at_ns: int | None = None,
    missing: frozenset[str] = frozenset(),
    observed_at_ns: int | None = None,
):
    evaluated = (
        _ns("2026-09-20T01:00:30+00:00")
        if evaluated_at_ns is None
        else evaluated_at_ns
    )
    by_key = {
        row_key: None if row_key in missing else _facts(row_key, observed_ns=observed_at_ns)
        for row_key in row_keys
    }
    evidence = PreEvaluationEvidenceAuthority(
        PreEvaluationPolicy(max_age_ns=120 * 1_000_000_000)
    ).evaluate_session(
        session_id=session_id,
        candidate_ids=row_keys,
        resolver=by_key.get,
        evaluated_at_ns=evaluated,
    )
    members = tuple(
        ProviderMemberIdentity(
            row_key=row_key,
            member_sha256={
                "row-a": SHA_C,
                "row-b": SHA_D,
                "row-c": SHA_E,
            }[row_key],
        )
        for row_key in row_keys
    )
    context = PreEvaluationDenominatorContext(
        session_id=session_id,
        campaign_id=campaign_id,
        research_protocol_id=protocol.record_id,
        protocol_sha256=protocol.protocol_sha256,
        provider_evidence_sha256=SHA_B,
    )
    return bind_pre_evaluation_session(
        evidence,
        context=context,
        provider_members=members,
        ledger=ledger,
    )


def _ready(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    ledger.path.touch()
    protocol = _protocol()
    registry.append(protocol)
    plan = _plan(protocol)
    plan_record_sha = register_forward_capture_plan(
        registry=registry,
        ledger=ledger,
        plan=plan,
    )
    bound = _bound(ledger, protocol)
    registry.append(_evaluation(protocol))
    return registry, ledger, protocol, plan, plan_record_sha, bound


def test_happy_path_binds_exact_precommitted_lineage_and_is_deterministic(tmp_path):
    registry, ledger, protocol, plan, record_sha, bound = _ready(tmp_path)

    first = bind_forward_evidence(
        registry=registry,
        ledger=ledger,
        capture_plan_id=plan.capture_plan_id,
        bound_session=bound,
        evaluation_bundle_id=plan.evaluation_bundle_id,
    )
    second = bind_forward_evidence(
        registry=ScientificRegistry(registry.path),
        ledger=JsonlDecisionLedger(ledger.path),
        capture_plan_id=plan.capture_plan_id,
        bound_session=bound,
        evaluation_bundle_id=plan.evaluation_bundle_id,
    )

    assert first == second
    assert first.capture_plan_record_sha256 == record_sha
    assert first.session_id == plan.session_id
    assert first.campaign_id == plan.campaign_id
    assert first.protocol_sha256 == protocol.protocol_sha256
    assert first.authority_sha256 == second.authority_sha256


def test_register_capture_plan_is_idempotent_without_minting_second_witness(tmp_path):
    registry, ledger, _, plan, record_sha, _ = _ready(tmp_path)

    assert register_forward_capture_plan(
        registry=registry, ledger=ledger, plan=plan
    ) == record_sha
    matches = [
        record
        for record in ledger.verified_records()
        if record.action == CAPTURE_PLAN_ACTION
    ]
    assert len(matches) == 1


def test_registry_rejects_capture_plan_without_earlier_protocol(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    protocol = _protocol()

    with pytest.raises(ValueError, match="earlier ResearchProtocol"):
        registry.append(_plan(protocol))


def test_capture_plan_rejects_post_outcome_fields_and_late_creation():
    protocol = _protocol()
    payload = _plan(protocol).to_payload()
    payload["status"] = "won"
    with pytest.raises(ValueError, match="fields mismatch"):
        ForwardCapturePlan.from_payload(payload)

    with pytest.raises(ValueError, match="created before"):
        _plan(protocol, created_at=T_SLOT)


def test_late_plan_witness_outside_bound_prefix_is_rejected(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    ledger.path.touch()
    protocol = _protocol()
    registry.append(protocol)
    plan = _plan(protocol)
    registry.append(plan)

    # Bind the session before the plan obtains a Decision Ledger witness.
    bound = _bound(ledger, protocol)
    register_forward_capture_plan(registry=registry, ledger=ledger, plan=plan)
    registry.append(_evaluation(protocol))

    with pytest.raises(ValueError, match="exactly one forward capture plan witness"):
        bind_forward_evidence(
            registry=registry,
            ledger=ledger,
            capture_plan_id=plan.capture_plan_id,
            bound_session=bound,
            evaluation_bundle_id=plan.evaluation_bundle_id,
        )


def test_evaluation_published_before_capture_plan_is_rejected(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    ledger.path.touch()
    protocol = _protocol()
    registry.append(protocol)
    registry.append(_evaluation(protocol))
    plan = _plan(protocol)
    register_forward_capture_plan(registry=registry, ledger=ledger, plan=plan)
    bound = _bound(ledger, protocol)

    with pytest.raises(ValueError, match="must durably precede EvaluationBundle"):
        bind_forward_evidence(
            registry=registry,
            ledger=ledger,
            capture_plan_id=plan.capture_plan_id,
            bound_session=bound,
            evaluation_bundle_id=plan.evaluation_bundle_id,
        )


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("campaign", "campaign_id mismatch"),
        ("session", "session_id mismatch"),
        ("evaluation", "evaluation_bundle_id mismatch"),
    ),
)
def test_valid_but_foreign_identity_cannot_be_composed(tmp_path, kind, expected):
    registry, ledger, protocol, plan, _, bound = _ready(tmp_path)

    if kind == "campaign":
        bound = _bound(ledger, protocol, campaign_id="campaign-foreign")
        evaluation_id = plan.evaluation_bundle_id
    elif kind == "session":
        bound = _bound(ledger, protocol, session_id="session-foreign")
        evaluation_id = plan.evaluation_bundle_id
    else:
        registry.append(
            _evaluation(protocol, evaluation_bundle_id="evaluation-foreign")
        )
        evaluation_id = "evaluation-foreign"

    with pytest.raises(ValueError, match=expected):
        bind_forward_evidence(
            registry=registry,
            ledger=ledger,
            capture_plan_id=plan.capture_plan_id,
            bound_session=bound,
            evaluation_bundle_id=evaluation_id,
        )


def test_valid_foreign_protocol_evaluation_cannot_be_composed(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    ledger.path.touch()
    protocol = _protocol()
    registry.append(protocol)
    plan = _plan(protocol)
    register_forward_capture_plan(registry=registry, ledger=ledger, plan=plan)
    bound = _bound(ledger, protocol)
    registry.append(_evaluation(protocol, protocol_sha256=SHA_E))

    with pytest.raises(ValueError, match="protocol_sha256 mismatch"):
        bind_forward_evidence(
            registry=registry,
            ledger=ledger,
            capture_plan_id=plan.capture_plan_id,
            bound_session=bound,
            evaluation_bundle_id=plan.evaluation_bundle_id,
        )


def test_foreign_slot_membership_cannot_be_composed(tmp_path):
    registry, ledger, protocol, plan, _, _ = _ready(tmp_path)
    foreign = _bound(ledger, protocol, row_keys=("row-a", "row-c"))

    with pytest.raises(ValueError, match="slot membership must exactly equal"):
        bind_forward_evidence(
            registry=registry,
            ledger=ledger,
            capture_plan_id=plan.capture_plan_id,
            bound_session=foreign,
            evaluation_bundle_id=plan.evaluation_bundle_id,
        )


def test_late_evaluation_and_pre_schedule_observation_fail_closed(tmp_path):
    registry, ledger, protocol, plan, _, _ = _ready(tmp_path)

    late = _bound(
        ledger,
        protocol,
        evaluated_at_ns=_ns("2026-09-20T01:02:00+00:00"),
        observed_at_ns=_ns("2026-09-20T01:01:50+00:00"),
    )
    with pytest.raises(ValueError, match="pre-evaluation time is outside"):
        bind_forward_evidence(
            registry=registry,
            ledger=ledger,
            capture_plan_id=plan.capture_plan_id,
            bound_session=late,
            evaluation_bundle_id=plan.evaluation_bundle_id,
        )

    early_observation = _bound(
        ledger,
        protocol,
        observed_at_ns=_ns("2026-09-20T00:59:59+00:00"),
    )
    with pytest.raises(ValueError, match="observation time is outside"):
        bind_forward_evidence(
            registry=registry,
            ledger=ledger,
            capture_plan_id=plan.capture_plan_id,
            bound_session=early_observation,
            evaluation_bundle_id=plan.evaluation_bundle_id,
        )


def test_missing_planned_observation_is_not_forward_evidence(tmp_path):
    registry, ledger, protocol, plan, _, _ = _ready(tmp_path)
    incomplete = _bound(ledger, protocol, missing=frozenset({"row-b"}))

    with pytest.raises(ValueError, match="incomplete for planned slot row-b"):
        bind_forward_evidence(
            registry=registry,
            ledger=ledger,
            capture_plan_id=plan.capture_plan_id,
            bound_session=incomplete,
            evaluation_bundle_id=plan.evaluation_bundle_id,
        )


def test_duplicate_plan_witness_inside_bound_prefix_is_rejected(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific.json")
    ledger = JsonlDecisionLedger(tmp_path / "decisions.jsonl")
    ledger.path.touch()
    protocol = _protocol()
    registry.append(protocol)
    plan = _plan(protocol)
    register_forward_capture_plan(registry=registry, ledger=ledger, plan=plan)
    original = ledger.verified_records()[0]
    ledger.append(
        DecisionRecord(
            replay_run_id=original.replay_run_id,
            agent=original.agent,
            observed_ts=original.observed_ts,
            action=original.action,
            payload=dict(original.payload),
            context_hash=original.context_hash,
            decision_id="duplicate-forward-plan-witness",
            recorded_at=original.recorded_at,
        )
    )
    bound = _bound(ledger, protocol)
    registry.append(_evaluation(protocol))

    with pytest.raises(ValueError, match="exactly one forward capture plan witness"):
        bind_forward_evidence(
            registry=registry,
            ledger=ledger,
            capture_plan_id=plan.capture_plan_id,
            bound_session=bound,
            evaluation_bundle_id=plan.evaluation_bundle_id,
        )


def test_ledger_suffix_after_bound_prefix_does_not_change_provenance(tmp_path):
    registry, ledger, _, plan, _, bound = _ready(tmp_path)
    expected = bind_forward_evidence(
        registry=registry,
        ledger=ledger,
        capture_plan_id=plan.capture_plan_id,
        bound_session=bound,
        evaluation_bundle_id=plan.evaluation_bundle_id,
    )
    ledger.append(
        DecisionRecord(
            replay_run_id="later-run",
            agent="later-agent",
            observed_ts=T_EVAL,
            action="UNRELATED",
            payload={"note": "later durable suffix"},
            context_hash=SHA_A,
        )
    )

    replay = bind_forward_evidence(
        registry=registry,
        ledger=ledger,
        capture_plan_id=plan.capture_plan_id,
        bound_session=bound,
        evaluation_bundle_id=plan.evaluation_bundle_id,
    )
    assert replay.authority_sha256 == expected.authority_sha256


def test_restart_revalidates_forward_plan_semantics(tmp_path):
    registry, _, _, plan, _, _ = _ready(tmp_path)
    reopened = ScientificRegistry(registry.path)
    entry = reopened.get("ForwardCapturePlan", plan.capture_plan_id)
    assert entry is not None
    assert ForwardCapturePlan.from_payload(entry.payload) == plan
