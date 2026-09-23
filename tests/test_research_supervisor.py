import json
from datetime import datetime, timedelta, timezone

import pytest

from autosport.research_supervisor import (
    ConflictingResearchTriggerError,
    ResearchPhase,
    ResearchSupervisor,
    ResearchSupervisorError,
    ResearchSupervisorLimitReached,
    ResearchTrigger,
    StaleResearchCheckpointError,
    SupervisorStatus,
)
from autosport.scientific_registry import (
    DatasetSnapshot,
    EvaluationBundleRef,
    ExperimentRecord,
    FeatureSet,
    Hypothesis,
    ModelVersion,
    Postmortem,
    PromotionAction,
    PromotionDecision,
    ResearchOutcome,
    ResearchProtocol,
    ResearchQuestion,
    ScientificRegistry,
    StrategyVersion,
)
from autosport.strategy_experiment import ScientificProtocolBinding


SOURCE_SHA = "1" * 64


def _workspace(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    registry.append(
        ResearchQuestion(
            question_id="question-1",
            statement="Does the challenger improve the frozen primary metric?",
            source_sha256=SOURCE_SHA,
            created_at="2026-09-17T12:00:00Z",
        )
    )
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json",
        registry,
    )
    return registry, supervisor


def _payload_sha(record) -> str:
    import hashlib

    canonical = json.dumps(
        record.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _protocol(
    question: ResearchQuestion,
    hypothesis: Hypothesis,
    *,
    protocol_id: str,
    question_sha256: str | None = None,
    hypothesis_sha256: str | None = None,
) -> ResearchProtocol:
    binding = ScientificProtocolBinding(
        research_protocol_id=protocol_id,
        research_question_id=question.question_id,
        research_question_sha256=question_sha256 or _payload_sha(question),
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=hypothesis_sha256 or _payload_sha(hypothesis),
        inclusion_criteria="predeclared cases",
        exclusion_criteria="invalid provenance",
        lawful_source_requirements="retained lawful evidence",
        causal_cutoff="2026-09-17T12:09:00Z",
        evaluation_design="frozen walk-forward",
        feature_set_version="v1",
        uncertainty_method="bootstrap interval",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split",),
        random_seed_policy="fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule="positive effect and guardrails",
        expected_artifacts=("evaluation bundle",),
        code_config_sha256=SOURCE_SHA,
        frozen_at_utc="2026-09-17T12:09:00Z",
    )
    return ResearchProtocol(
        binding,
        SOURCE_SHA,
        SOURCE_SHA,
        SOURCE_SHA,
        "2026-09-17T12:09:00Z",
    )


def _trigger(**overrides):
    values = {
        "trigger_id": "trigger-1",
        "question_id": "question-1",
        "requested_at": "2026-09-17T12:01:00Z",
        "budget_units": 32,
        "deadline_at": "2026-09-18T12:00:00Z",
    }
    values.update(overrides)
    return ResearchTrigger(**values)


def test_duplicate_timezone_equivalent_trigger_collapses_to_one_run(tmp_path):
    _, supervisor = _workspace(tmp_path)
    first = supervisor.accept_trigger(_trigger())
    second = supervisor.accept_trigger(
        _trigger(requested_at="2026-09-17T14:01:00+02:00")
    )

    assert second == first
    assert len(supervisor.list_runs()) == 1
    assert first.created_at == "2026-09-17T12:01:00Z"


def test_conflicting_trigger_identity_fails_closed(tmp_path):
    _, supervisor = _workspace(tmp_path)
    supervisor.accept_trigger(_trigger())

    with pytest.raises(ConflictingResearchTriggerError):
        supervisor.accept_trigger(_trigger(budget_units=31))


def test_restart_preserves_checkpoint_and_rejects_stale_phase(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    started = supervisor.accept_trigger(_trigger())
    advanced = supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:02:00Z",
    )
    assert advanced.phase is ResearchPhase.HYPOTHESIS
    assert advanced.checkpoint_index == 1

    reopened = ResearchSupervisor(supervisor.path, registry)
    assert reopened.status(started.run_id) == advanced
    with pytest.raises(StaleResearchCheckpointError):
        reopened.advance(
            started.run_id,
            expected_phase=ResearchPhase.QUESTION,
            at="2026-09-17T12:03:00Z",
        )


def test_scientific_binding_must_exist_and_be_causally_available(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    registry.append(
        Hypothesis(
            hypothesis_id="hypothesis-1",
            research_question_id="question-1",
            statement="Candidate is better.",
            falsifiable_prediction="Primary metric improves.",
            failure_criteria="Primary metric does not improve.",
            primary_metric="score",
            protective_metrics=("drawdown",),
            created_at="2026-09-17T12:10:00Z",
        )
    )
    started = supervisor.accept_trigger(_trigger())

    with pytest.raises(ResearchSupervisorError, match="future Hypothesis"):
        supervisor.advance(
            started.run_id,
            expected_phase=ResearchPhase.QUESTION,
            at="2026-09-17T12:05:00Z",
            bindings=(("hypothesis_id", "hypothesis-1"),),
        )

    advanced = supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:10:00Z",
        bindings=(("hypothesis_id", "hypothesis-1"),),
    )
    assert advanced.bindings == (("hypothesis_id", "hypothesis-1"),)


def test_unknown_binding_is_not_accepted_as_provenance(tmp_path):
    _, supervisor = _workspace(tmp_path)
    started = supervisor.accept_trigger(_trigger())

    with pytest.raises(ResearchSupervisorError, match="unsupported supervisor binding"):
        supervisor.advance(
            started.run_id,
            expected_phase=ResearchPhase.QUESTION,
            at="2026-09-17T12:02:00Z",
            bindings=(("made_up_authority", "anything"),),
        )


def test_pause_resume_and_stop_are_durable_and_fail_closed(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    started = supervisor.accept_trigger(_trigger())
    paused = supervisor.pause(started.run_id, at="2026-09-17T12:02:00Z")
    assert paused.status is SupervisorStatus.PAUSED

    with pytest.raises(ResearchSupervisorError, match="not active"):
        supervisor.advance(
            started.run_id,
            expected_phase=ResearchPhase.QUESTION,
            at="2026-09-17T12:03:00Z",
        )

    resumed = supervisor.resume(started.run_id, at="2026-09-17T12:04:00Z")
    assert resumed.status is SupervisorStatus.ACTIVE
    stopped = supervisor.stop(
        started.run_id,
        at="2026-09-17T12:05:00Z",
        reason="operator STOP",
    )
    assert stopped.status is SupervisorStatus.STOPPED
    assert stopped.stop_reason == "operator STOP"

    reopened = ResearchSupervisor(supervisor.path, registry)
    assert reopened.status(started.run_id) == stopped
    with pytest.raises(ResearchSupervisorError):
        reopened.resume(started.run_id, at="2026-09-17T12:06:00Z")


def test_budget_exhaustion_stops_without_advancing_phase(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    started = supervisor.accept_trigger(_trigger(budget_units=1))
    supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:02:00Z",
    )

    with pytest.raises(ResearchSupervisorLimitReached, match="budget exhausted"):
        supervisor.advance(
            started.run_id,
            expected_phase=ResearchPhase.HYPOTHESIS,
            at="2026-09-17T12:03:00Z",
        )

    reopened = ResearchSupervisor(supervisor.path, registry)
    state = reopened.status(started.run_id)
    assert state.phase is ResearchPhase.HYPOTHESIS
    assert state.status is SupervisorStatus.STOPPED
    assert state.stop_reason == "BUDGET_EXHAUSTED"
    assert state.consumed_budget_units == 1


def test_deadline_expiry_is_persisted_as_terminal_stop(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    started = supervisor.accept_trigger(
        _trigger(deadline_at="2026-09-17T12:02:00Z")
    )

    with pytest.raises(ResearchSupervisorLimitReached, match="deadline expired"):
        supervisor.advance(
            started.run_id,
            expected_phase=ResearchPhase.QUESTION,
            at="2026-09-17T12:03:00Z",
        )

    state = ResearchSupervisor(supervisor.path, registry).status(started.run_id)
    assert state.phase is ResearchPhase.QUESTION
    assert state.status is SupervisorStatus.STOPPED
    assert state.stop_reason == "DEADLINE_EXPIRED"


def test_full_frozen_lifecycle_reaches_complete_without_phase_skips(tmp_path):
    _, supervisor = _workspace(tmp_path)
    state = supervisor.accept_trigger(_trigger())
    now = datetime(2026, 9, 17, 12, 2, tzinfo=timezone.utc)

    expected = ResearchPhase.QUESTION
    while expected is not ResearchPhase.COMPLETE:
        state = supervisor.advance(
            state.run_id,
            expected_phase=expected,
            at=now.isoformat(),
        )
        expected = state.phase
        now += timedelta(minutes=1)

    assert state.phase is ResearchPhase.COMPLETE
    assert state.status is SupervisorStatus.COMPLETED
    assert state.checkpoint_index == len(ResearchPhase) - 1


def test_state_digest_detects_out_of_band_mutation(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    supervisor.accept_trigger(_trigger())
    payload = json.loads(supervisor.path.read_text(encoding="utf-8"))
    payload["runs"][0]["question_id"] = "tampered-question"
    supervisor.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="state digest mismatch"):
        ResearchSupervisor(supervisor.path, registry)


def test_trigger_cannot_reference_future_question(tmp_path):
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
    registry.append(
        ResearchQuestion(
            question_id="future-question",
            statement="Future question",
            source_sha256=SOURCE_SHA,
            created_at="2026-09-17T13:00:00Z",
        )
    )
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json",
        registry,
    )
    with pytest.raises(ResearchSupervisorError, match="from the future"):
        supervisor.accept_trigger(
            ResearchTrigger(
                trigger_id="future-trigger",
                question_id="future-question",
                requested_at="2026-09-17T12:00:00Z",
                budget_units=5,
            )
        )


def test_hypothesis_binding_must_belong_to_run_question(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    registry.append(
        ResearchQuestion(
            question_id="question-2",
            statement="Independent second question",
            source_sha256=SOURCE_SHA,
            created_at="2026-09-17T12:04:00Z",
        )
    )
    registry.append(
        Hypothesis(
            hypothesis_id="hypothesis-2",
            research_question_id="question-2",
            statement="Second-lineage hypothesis.",
            falsifiable_prediction="Metric improves.",
            failure_criteria="Metric does not improve.",
            primary_metric="score",
            protective_metrics=(),
            created_at="2026-09-17T12:05:00Z",
        )
    )
    started = supervisor.accept_trigger(_trigger())

    with pytest.raises(
        ResearchSupervisorError,
        match="hypothesis research question does not match supervisor run",
    ):
        supervisor.advance(
            started.run_id,
            expected_phase=ResearchPhase.QUESTION,
            at="2026-09-17T12:06:00Z",
            bindings=(("hypothesis_id", "hypothesis-2"),),
        )


def test_protocol_binding_requires_explicit_supervisor_hypothesis(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    question = ResearchQuestion(
        question_id="question-1",
        statement="Does the challenger improve the frozen primary metric?",
        source_sha256=SOURCE_SHA,
        created_at="2026-09-17T12:00:00Z",
    )
    hypothesis = Hypothesis(
        hypothesis_id="hypothesis-1",
        research_question_id="question-1",
        statement="Candidate is better.",
        falsifiable_prediction="Primary metric improves.",
        failure_criteria="Primary metric does not improve.",
        primary_metric="score",
        protective_metrics=("drawdown",),
        created_at="2026-09-17T12:05:00Z",
    )
    registry.append(hypothesis)
    registry.append(_protocol(question, hypothesis, protocol_id="protocol-1"))

    started = supervisor.accept_trigger(_trigger())
    state = supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:06:00Z",
        bindings=(),
    )

    with pytest.raises(
        ResearchSupervisorError,
        match="research protocol requires explicit supervisor hypothesis binding",
    ):
        supervisor.advance(
            state.run_id,
            expected_phase=ResearchPhase.HYPOTHESIS,
            at="2026-09-17T12:10:00Z",
            bindings=(("research_protocol_id", "protocol-1"),),
        )


def test_restart_rejects_protocol_without_explicit_supervisor_hypothesis(tmp_path):
    import hashlib

    registry, supervisor = _workspace(tmp_path)
    question = ResearchQuestion(
        question_id="question-1",
        statement="Does the challenger improve the frozen primary metric?",
        source_sha256=SOURCE_SHA,
        created_at="2026-09-17T12:00:00Z",
    )
    hypothesis = Hypothesis(
        hypothesis_id="hypothesis-1",
        research_question_id="question-1",
        statement="Candidate is better.",
        falsifiable_prediction="Primary metric improves.",
        failure_criteria="Primary metric does not improve.",
        primary_metric="score",
        protective_metrics=("drawdown",),
        created_at="2026-09-17T12:05:00Z",
    )
    registry.append(hypothesis)
    registry.append(_protocol(question, hypothesis, protocol_id="protocol-1"))

    started = supervisor.accept_trigger(_trigger())
    state = supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:06:00Z",
        bindings=(("hypothesis_id", "hypothesis-1"),),
    )
    supervisor.advance(
        state.run_id,
        expected_phase=ResearchPhase.HYPOTHESIS,
        at="2026-09-17T12:10:00Z",
        bindings=(("research_protocol_id", "protocol-1"),),
    )

    payload = json.loads(supervisor.path.read_text(encoding="utf-8"))
    run = payload["runs"][0]
    del run["bindings"]["hypothesis_id"]

    def digest(value):
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    run["run_sha256"] = digest(
        {key: value for key, value in run.items() if key != "run_sha256"}
    )
    payload["state_sha256"] = digest(
        {key: value for key, value in payload.items() if key != "state_sha256"}
    )
    supervisor.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ResearchSupervisorError,
        match="research protocol requires explicit supervisor hypothesis binding",
    ):
        ResearchSupervisor(supervisor.path, registry)


def test_protocol_binding_must_match_run_question_hypothesis_and_payload_digests(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    question_1 = ResearchQuestion(
        question_id="question-1",
        statement="Does the challenger improve the frozen primary metric?",
        source_sha256=SOURCE_SHA,
        created_at="2026-09-17T12:00:00Z",
    )
    hypothesis_1 = Hypothesis(
        hypothesis_id="hypothesis-1",
        research_question_id="question-1",
        statement="Candidate is better.",
        falsifiable_prediction="Primary metric improves.",
        failure_criteria="Primary metric does not improve.",
        primary_metric="score",
        protective_metrics=("drawdown",),
        created_at="2026-09-17T12:05:00Z",
    )
    question_2 = ResearchQuestion(
        question_id="question-2",
        statement="Independent second question",
        source_sha256=SOURCE_SHA,
        created_at="2026-09-17T12:00:00Z",
    )
    hypothesis_2 = Hypothesis(
        hypothesis_id="hypothesis-2",
        research_question_id="question-2",
        statement="Independent second hypothesis.",
        falsifiable_prediction="Second metric improves.",
        failure_criteria="Second metric does not improve.",
        primary_metric="other-score",
        protective_metrics=(),
        created_at="2026-09-17T12:05:00Z",
    )
    for record in (hypothesis_1, question_2, hypothesis_2):
        registry.append(record)
    registry.append(_protocol(question_2, hypothesis_2, protocol_id="protocol-2"))

    started = supervisor.accept_trigger(_trigger())
    state = supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:06:00Z",
        bindings=(("hypothesis_id", "hypothesis-1"),),
    )

    with pytest.raises(
        ResearchSupervisorError,
        match="research protocol question does not match supervisor run",
    ):
        supervisor.advance(
            state.run_id,
            expected_phase=ResearchPhase.HYPOTHESIS,
            at="2026-09-17T12:10:00Z",
            bindings=(("research_protocol_id", "protocol-2"),),
        )

    registry.append(
        _protocol(
            question_1,
            hypothesis_1,
            protocol_id="protocol-bad-question-digest",
            question_sha256="f" * 64,
        )
    )
    with pytest.raises(
        ResearchSupervisorError,
        match="research protocol question digest does not match canonical question",
    ):
        supervisor.advance(
            state.run_id,
            expected_phase=ResearchPhase.HYPOTHESIS,
            at="2026-09-17T12:10:00Z",
            bindings=(("research_protocol_id", "protocol-bad-question-digest"),),
        )

    registry.append(
        _protocol(
            question_1,
            hypothesis_1,
            protocol_id="protocol-bad-hypothesis-digest",
            hypothesis_sha256="e" * 64,
        )
    )
    with pytest.raises(
        ResearchSupervisorError,
        match="research protocol hypothesis digest does not match canonical hypothesis",
    ):
        supervisor.advance(
            state.run_id,
            expected_phase=ResearchPhase.HYPOTHESIS,
            at="2026-09-17T12:10:00Z",
            bindings=(("research_protocol_id", "protocol-bad-hypothesis-digest"),),
        )

    registry.append(_protocol(question_1, hypothesis_1, protocol_id="protocol-1"))
    valid = supervisor.advance(
        state.run_id,
        expected_phase=ResearchPhase.HYPOTHESIS,
        at="2026-09-17T12:10:00Z",
        bindings=(("research_protocol_id", "protocol-1"),),
    )
    assert ("research_protocol_id", "protocol-1") in valid.bindings


def test_restart_rejects_self_consistent_cross_linked_scientific_binding(tmp_path):
    import hashlib

    registry, supervisor = _workspace(tmp_path)
    registry.append(
        Hypothesis(
            hypothesis_id="hypothesis-1",
            research_question_id="question-1",
            statement="Candidate is better.",
            falsifiable_prediction="Primary metric improves.",
            failure_criteria="Primary metric does not improve.",
            primary_metric="score",
            protective_metrics=(),
            created_at="2026-09-17T12:05:00Z",
        )
    )
    registry.append(
        ResearchQuestion(
            question_id="question-2",
            statement="Independent second question",
            source_sha256=SOURCE_SHA,
            created_at="2026-09-17T12:04:00Z",
        )
    )
    registry.append(
        Hypothesis(
            hypothesis_id="hypothesis-2",
            research_question_id="question-2",
            statement="Second-lineage hypothesis.",
            falsifiable_prediction="Other metric improves.",
            failure_criteria="Other metric does not improve.",
            primary_metric="other-score",
            protective_metrics=(),
            created_at="2026-09-17T12:05:00Z",
        )
    )
    started = supervisor.accept_trigger(_trigger())
    supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:06:00Z",
        bindings=(("hypothesis_id", "hypothesis-1"),),
    )

    payload = json.loads(supervisor.path.read_text(encoding="utf-8"))
    run = payload["runs"][0]
    run["bindings"]["hypothesis_id"] = "hypothesis-2"

    def digest(value):
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    run["run_sha256"] = digest(
        {key: value for key, value in run.items() if key != "run_sha256"}
    )
    payload["state_sha256"] = digest(
        {key: value for key, value in payload.items() if key != "state_sha256"}
    )
    supervisor.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ResearchSupervisorError,
        match="hypothesis research question does not match supervisor run",
    ):
        ResearchSupervisor(supervisor.path, registry)


def _append_downstream_chain(registry, protocol, *, suffix: str):
    dataset = DatasetSnapshot(
        dataset_snapshot_id=f"dataset-{suffix}",
        manifest_sha256=SOURCE_SHA,
        source_identity=f"source-{suffix}",
        license_identity="licensed",
        causal_cutoff="2026-09-17T12:09:00Z",
        available_at_utc="2026-09-17T12:10:00Z",
    )
    feature = FeatureSet(
        feature_set_id=f"feature-{suffix}",
        version="v1",
        definition_sha256="2" * 64,
        source_sha256=SOURCE_SHA,
        available_at_utc="2026-09-17T12:10:00Z",
    )
    model = ModelVersion(
        model_version_id=f"model-{suffix}",
        model_family="baseline",
        artifact_sha256="3" * 64,
        source_sha256=SOURCE_SHA,
        environment_sha256=SOURCE_SHA,
        dataset_snapshot_id=dataset.record_id,
        feature_set_id=feature.record_id,
        research_protocol_id=protocol.record_id,
        seed=7,
        config_sha256=SOURCE_SHA,
        created_at="2026-09-17T12:11:00Z",
    )
    strategy = StrategyVersion(
        strategy_version_id=f"strategy-{suffix}",
        canonical_strategy_id="research-supervisor-test",
        source_sha256=SOURCE_SHA,
        environment_sha256=SOURCE_SHA,
        config_sha256=SOURCE_SHA,
        created_at="2026-09-17T12:12:00Z",
        model_version_id=model.record_id,
    )
    bundle = EvaluationBundleRef(
        evaluation_bundle_id=f"evaluation-{suffix}",
        bundle_sha256="4" * 64,
        evaluator_source_sha256=SOURCE_SHA,
        dataset_snapshot_id=dataset.record_id,
        protocol_sha256=protocol.protocol_sha256,
        artifact_hashes=("5" * 64,),
        created_at="2026-09-17T12:13:00Z",
        evaluated_strategy_version_id=strategy.record_id,
        evaluated_model_version_id=model.record_id,
    )
    experiment = ExperimentRecord(
        experiment_id=f"experiment-{suffix}",
        research_protocol_id=protocol.record_id,
        dataset_snapshot_id=dataset.record_id,
        feature_set_id=feature.record_id,
        model_version_id=model.record_id,
        strategy_version_id=strategy.record_id,
        evaluation_bundle_id=bundle.record_id,
        seed=7,
        config_sha256=SOURCE_SHA,
        outcome=ResearchOutcome.NULL,
        created_at="2026-09-17T12:12:30Z",
        completed_at="2026-09-17T12:14:00Z",
    )
    for record in (dataset, feature, model, strategy, bundle, experiment):
        registry.append(record)
    decision = PromotionDecision(
        promotion_decision_id=f"decision-{suffix}",
        action=PromotionAction.RETAIN,
        candidate_strategy_version_id=strategy.record_id,
        candidate_model_version_id=model.record_id,
        research_protocol_id=protocol.record_id,
        protocol_sha256=protocol.protocol_sha256,
        evaluation_bundle_id=bundle.record_id,
        evaluation_bundle_sha256=bundle.bundle_sha256,
        decided_at="2026-09-17T12:15:00Z",
        reason="retain after null result",
    )
    registry.record_promotion(decision)
    postmortem = Postmortem(
        postmortem_id=f"postmortem-{suffix}",
        experiment_id=experiment.record_id,
        classification=ResearchOutcome.NULL,
        finding="No material improvement.",
        retest_conditions=("new independent data",),
        created_at="2026-09-17T12:16:00Z",
    )
    registry.append(postmortem)
    return {
        "dataset": dataset.record_id,
        "feature": feature.record_id,
        "model": model.record_id,
        "strategy": strategy.record_id,
        "evaluation": bundle.record_id,
        "experiment": experiment.record_id,
        "decision": decision.record_id,
        "postmortem": postmortem.record_id,
    }


def test_downstream_scientific_bindings_must_stay_on_one_durable_lineage(tmp_path):
    registry, supervisor = _workspace(tmp_path)
    question = ResearchQuestion(
        question_id="question-1",
        statement="Does the challenger improve the frozen primary metric?",
        source_sha256=SOURCE_SHA,
        created_at="2026-09-17T12:00:00Z",
    )
    hypothesis = Hypothesis(
        hypothesis_id="hypothesis-1",
        research_question_id="question-1",
        statement="Candidate is better.",
        falsifiable_prediction="Primary metric improves.",
        failure_criteria="Primary metric does not improve.",
        primary_metric="score",
        protective_metrics=("drawdown",),
        created_at="2026-09-17T12:05:00Z",
    )
    registry.append(hypothesis)
    protocol = _protocol(question, hypothesis, protocol_id="protocol-1")
    registry.append(protocol)
    first = _append_downstream_chain(registry, protocol, suffix="one")
    second = _append_downstream_chain(registry, protocol, suffix="two")

    state = supervisor.accept_trigger(_trigger())
    state = supervisor.advance(
        state.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:06:00Z",
        bindings=(("hypothesis_id", hypothesis.record_id),),
    )
    state = supervisor.advance(
        state.run_id,
        expected_phase=ResearchPhase.HYPOTHESIS,
        at="2026-09-17T12:10:00Z",
        bindings=(("research_protocol_id", protocol.record_id),),
    )
    state = supervisor.advance(
        state.run_id,
        expected_phase=ResearchPhase.SOURCE_SEARCH,
        at="2026-09-17T12:14:00Z",
        bindings=(
            ("dataset_snapshot_id", first["dataset"]),
            ("evaluation_bundle_id", first["evaluation"]),
            ("feature_set_id", first["feature"]),
            ("model_version_id", first["model"]),
            ("strategy_version_id", first["strategy"]),
        ),
    )

    with pytest.raises(
        ResearchSupervisorError,
        match="experiment dataset_snapshot_id does not match supervisor binding",
    ):
        supervisor.advance(
            state.run_id,
            expected_phase=ResearchPhase.PROTOCOL_FREEZE,
            at="2026-09-17T12:14:30Z",
            bindings=(("experiment_id", second["experiment"]),),
        )

    state = supervisor.advance(
        state.run_id,
        expected_phase=ResearchPhase.PROTOCOL_FREEZE,
        at="2026-09-17T12:14:30Z",
        bindings=(("experiment_id", first["experiment"]),),
    )

    with pytest.raises(
        ResearchSupervisorError,
        match=(
            "promotion decision candidate_strategy_version_id "
            "does not match supervisor binding"
        ),
    ):
        supervisor.advance(
            state.run_id,
            expected_phase=ResearchPhase.DATASET_SNAPSHOT,
            at="2026-09-17T12:15:00Z",
            bindings=(("promotion_decision_id", second["decision"]),),
        )

    with pytest.raises(
        ResearchSupervisorError,
        match="postmortem experiment does not match supervisor binding",
    ):
        supervisor.advance(
            state.run_id,
            expected_phase=ResearchPhase.DATASET_SNAPSHOT,
            at="2026-09-17T12:16:00Z",
            bindings=(("postmortem_id", second["postmortem"]),),
        )


def test_restart_rejects_rehashed_cross_linked_downstream_experiment(tmp_path):
    import hashlib

    registry, supervisor = _workspace(tmp_path)
    question = ResearchQuestion(
        question_id="question-1",
        statement="Does the challenger improve the frozen primary metric?",
        source_sha256=SOURCE_SHA,
        created_at="2026-09-17T12:00:00Z",
    )
    hypothesis = Hypothesis(
        hypothesis_id="hypothesis-1",
        research_question_id="question-1",
        statement="Candidate is better.",
        falsifiable_prediction="Primary metric improves.",
        failure_criteria="Primary metric does not improve.",
        primary_metric="score",
        protective_metrics=("drawdown",),
        created_at="2026-09-17T12:05:00Z",
    )
    registry.append(hypothesis)
    protocol = _protocol(question, hypothesis, protocol_id="protocol-1")
    registry.append(protocol)
    first = _append_downstream_chain(registry, protocol, suffix="one")
    second = _append_downstream_chain(registry, protocol, suffix="two")

    state = supervisor.accept_trigger(_trigger())
    state = supervisor.advance(
        state.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-09-17T12:06:00Z",
        bindings=(("hypothesis_id", hypothesis.record_id),),
    )
    state = supervisor.advance(
        state.run_id,
        expected_phase=ResearchPhase.HYPOTHESIS,
        at="2026-09-17T12:10:00Z",
        bindings=(("research_protocol_id", protocol.record_id),),
    )
    supervisor.advance(
        state.run_id,
        expected_phase=ResearchPhase.SOURCE_SEARCH,
        at="2026-09-17T12:14:00Z",
        bindings=(
            ("dataset_snapshot_id", first["dataset"]),
            ("evaluation_bundle_id", first["evaluation"]),
            ("experiment_id", first["experiment"]),
            ("feature_set_id", first["feature"]),
            ("model_version_id", first["model"]),
            ("strategy_version_id", first["strategy"]),
        ),
    )

    payload = json.loads(supervisor.path.read_text(encoding="utf-8"))
    run = payload["runs"][0]
    run["bindings"]["experiment_id"] = second["experiment"]

    def digest(value):
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    run["run_sha256"] = digest(
        {key: value for key, value in run.items() if key != "run_sha256"}
    )
    payload["state_sha256"] = digest(
        {key: value for key, value in payload.items() if key != "state_sha256"}
    )
    supervisor.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ResearchSupervisorError,
        match="experiment dataset_snapshot_id does not match supervisor binding",
    ):
        ResearchSupervisor(supervisor.path, registry)
