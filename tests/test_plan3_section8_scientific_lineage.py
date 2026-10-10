"""Plan-3 Section-8 research-lineage regression: reused from PR #1277.

Offline fixtures only; no model, money, provider or owner authority.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


