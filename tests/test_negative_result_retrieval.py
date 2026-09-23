from __future__ import annotations

import pytest

from test_scientific_registry_index import (
    SHA_A,
    SHA_B,
    SHA_C,
    SHA_D,
    T0,
    T1,
    T2,
    T3,
    _frozen_promotion_rule_text,
    _payload_sha,
    _seed_registry,
)

from autosport.monotonic_workspace_authority import MonotonicAuthorityRollbackError
from autosport.negative_result_retrieval import search_negative_results
from autosport.scientific_registry import (
    EvaluationBundleRef,
    ExperimentRecord,
    Hypothesis,
    Postmortem,
    ResearchOutcome,
    ResearchProtocol,
    ResearchQuestion,
    ScientificRegistry,
)
from autosport.strategy_experiment import ScientificProtocolBinding


def _with_postmortem(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = _seed_registry(path)
    registry.append(
        Postmortem(
            postmortem_id="postmortem-2",
            experiment_id="experiment-2",
            classification=ResearchOutcome.NULL,
            finding="Калібрування слабке на зимовому сегменті.",
            retest_conditions=("NEW_EVALUATION_BUNDLE",),
            created_at=T3,
        )
    )
    return path, registry


def _append_second_non_positive(registry):
    protocol = registry.get("ResearchProtocol", "protocol-1")
    assert protocol is not None
    bundle = EvaluationBundleRef(
        "eval-3",
        SHA_C,
        SHA_A,
        "dataset-1",
        protocol.payload["protocol_sha256"],
        (SHA_B,),
        T3,
        evaluated_strategy_version_id="strategy-2",
        evaluated_model_version_id="model-1",
    )
    experiment = ExperimentRecord(
        "experiment-3",
        "protocol-1",
        "dataset-1",
        "features-1",
        "strategy-2",
        "eval-3",
        8,
        SHA_B,
        ResearchOutcome.HARMFUL,
        T3,
        model_version_id="model-1",
        completed_at=T3,
        notes="Candidate primary metric degraded materially.",
    )
    registry.append(bundle)
    registry.append(experiment)


def test_semantic_retrieval_returns_only_non_positive_canonical_history(tmp_path):
    _path, registry = _with_postmortem(tmp_path)

    hits = search_negative_results(
        registry,
        "candidate primary metric",
        as_of=T3,
    )

    assert [hit.experiment_id for hit in hits] == ["experiment-2"]
    hit = hits[0]
    assert hit.outcome is ResearchOutcome.NULL
    assert hit.research_protocol_id == "protocol-1"
    assert hit.research_question_id == "question-1"
    assert hit.hypothesis_id == "hypothesis-1"
    assert hit.postmortem_ids == ("postmortem-2",)
    assert hit.matched_terms == ("candidate", "primary", "metric")
    assert hit.score == 3
    assert len(hit.experiment_record_sha256) == 64
    assert len(hit.protocol_record_sha256) == 64
    assert len(hit.question_record_sha256) == 64
    assert len(hit.hypothesis_record_sha256) == 64
    assert all(len(value) == 64 for value in hit.postmortem_record_sha256s)


def test_unicode_casefold_postmortem_search_is_causal_and_restart_deterministic(tmp_path):
    path, registry = _with_postmortem(tmp_path)

    before_postmortem = search_negative_results(
        registry,
        "КАЛІБРУВАННЯ зимовому",
        as_of=T2,
    )
    assert before_postmortem == ()

    expected = search_negative_results(
        registry,
        "КАЛІБРУВАННЯ зимовому",
        as_of=T3,
    )
    reopened = search_negative_results(
        ScientificRegistry(path),
        "калібрування ЗИМОВОМУ",
        as_of=T3,
    )

    assert reopened == expected
    assert len(expected) == 1
    assert expected[0].matched_terms == ("калібрування", "зимовому")
    assert expected[0].postmortem_ids == ("postmortem-2",)
    assert expected[0].available_at == T3


def test_hit_availability_compares_timezone_offsets_by_actual_instant(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = _seed_registry(path)
    postmortem_available_at = "2026-01-02T23:30:00-02:00"
    registry.append(
        Postmortem(
            postmortem_id="postmortem-offset",
            experiment_id="experiment-2",
            classification=ResearchOutcome.NULL,
            finding="Унікальний offset marker.",
            retest_conditions=("NEW_EVALUATION_BUNDLE",),
            created_at=postmortem_available_at,
        )
    )

    hits = search_negative_results(
        registry,
        "offset marker",
        as_of="2026-01-03T02:00:00+00:00",
    )

    assert len(hits) == 1
    assert hits[0].postmortem_ids == ("postmortem-offset",)
    assert hits[0].available_at == postmortem_available_at


def test_limit_and_tie_order_are_deterministic(tmp_path):
    _path, registry = _with_postmortem(tmp_path)
    _append_second_non_positive(registry)

    all_hits = search_negative_results(
        registry,
        "candidate primary metric",
        as_of=T3,
    )
    limited = search_negative_results(
        registry,
        "candidate primary metric",
        as_of=T3,
        limit=1,
    )

    assert [hit.experiment_id for hit in all_hits] == [
        "experiment-2",
        "experiment-3",
    ]
    assert limited == (all_hits[0],)


def test_query_tolerates_surrounding_operator_whitespace(tmp_path):
    _path, registry = _with_postmortem(tmp_path)

    canonical = search_negative_results(registry, "candidate", as_of=T3)
    padded = search_negative_results(registry, "  candidate  ", as_of=T3)

    assert padded == canonical


def test_search_is_advisory_and_exposes_no_repeat_or_promotion_authority(tmp_path):
    _path, registry = _with_postmortem(tmp_path)

    hit = search_negative_results(registry, "candidate", as_of=T3)[0]

    names = set(hit.__dataclass_fields__)
    assert "retest_authorized" not in names
    assert "repeat_authorized" not in names
    assert "promotion_authorized" not in names
    assert "real_money_execution" not in names


@pytest.mark.parametrize("query", ["", " ", "\x00", "_"])
def test_query_must_contain_canonical_searchable_text(tmp_path, query):
    _path, registry = _with_postmortem(tmp_path)

    with pytest.raises(ValueError, match="query"):
        search_negative_results(registry, query, as_of=T3)


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_limit_is_strictly_bounded_integer(tmp_path, limit):
    _path, registry = _with_postmortem(tmp_path)

    with pytest.raises(ValueError, match="limit"):
        search_negative_results(registry, "candidate", as_of=T3, limit=limit)


def test_self_consistent_unknown_experiment_outcome_tamper_fails_closed_at_registry_authority(
    tmp_path,
):
    path = tmp_path / "scientific_registry.json"
    _seed_registry(path)

    state = path.read_text(encoding="utf-8")
    tampered = state.replace(
        '"outcome":"NULL"',
        '"outcome":"UNKNOWN_RESULT"',
        1,
    )
    assert tampered != state
    path.write_text(tampered, encoding="utf-8")

    with pytest.raises(
        MonotonicAuthorityRollbackError,
        match="missing, rolled back, or unproven",
    ):
        ScientificRegistry(path)


def test_backfilled_question_hypothesis_protocol_lineage_fails_closed(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = ScientificRegistry.initialize_pristine(path)

    question = ResearchQuestion("late-question", "Late lineage question", SHA_A, T0)
    hypothesis = Hypothesis(
        "late-hypothesis",
        "late-question",
        "Late candidate improves the primary metric.",
        "primary > champion",
        "primary <= champion",
        "roi",
        ("drawdown",),
        T0,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id="late-protocol",
        research_question_id=question.question_id,
        research_question_sha256=_payload_sha(question),
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=_payload_sha(hypothesis),
        inclusion_criteria="frozen",
        exclusion_criteria="invalid provenance",
        lawful_source_requirements="lawful fixture",
        causal_cutoff=T1,
        evaluation_design="walk-forward holdout",
        feature_set_version="v1",
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split",),
        random_seed_policy="fixed",
        stopping_rule="one final evaluation",
        promotion_rule=_frozen_promotion_rule_text(),
        expected_artifacts=("evaluation bundle",),
        code_config_sha256=SHA_B,
        frozen_at_utc=T0,
    )
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, SHA_A, T0)
    experiment = ExperimentRecord(
        "experiment-before-lineage",
        "late-protocol",
        "dataset-late",
        "features-late",
        "strategy-late",
        "eval-late",
        17,
        SHA_B,
        ResearchOutcome.NULL,
        T2,
        completed_at=T2,
        notes="retroactive-lineage-marker",
    )

    registry.append(experiment)
    registry.append(question)
    registry.append(hypothesis)
    registry.append(protocol)

    with pytest.raises(RuntimeError, match="lineage does not causally precede experiment"):
        search_negative_results(
            registry,
            "retroactive-lineage-marker",
            as_of=T3,
        )


def test_postmortem_availability_must_not_precede_experiment_completion(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = _seed_registry(path)

    registry.append(
        Postmortem(
            postmortem_id="postmortem-backdated",
            experiment_id="experiment-2",
            classification=ResearchOutcome.NULL,
            finding="backdated-postmortem-marker",
            retest_conditions=("NEW_EVALUATION_BUNDLE",),
            created_at=T0,
        )
    )

    assert registry.causal_precedes(
        "Experiment",
        "experiment-2",
        "Postmortem",
        "postmortem-backdated",
    )

    with pytest.raises(RuntimeError, match="availability precedes"):
        search_negative_results(
            registry,
            "backdated-postmortem-marker",
            as_of=T3,
        )


def test_postmortem_must_causally_follow_referenced_experiment(tmp_path):
    path = tmp_path / "scientific_registry.json"
    registry = _seed_registry(path)

    protocol = registry.get("ResearchProtocol", "protocol-1")
    assert protocol is not None

    premature_postmortem = Postmortem(
        postmortem_id="postmortem-before-experiment-3",
        experiment_id="experiment-3",
        classification=ResearchOutcome.HARMFUL,
        finding="Передчасний causal marker для ще не завершеного експерименту.",
        retest_conditions=("NEW_EVALUATION_BUNDLE",),
        created_at=T2,
    )
    registry.append(premature_postmortem)

    bundle = EvaluationBundleRef(
        "eval-3",
        SHA_C,
        SHA_A,
        "dataset-1",
        protocol.payload["protocol_sha256"],
        (SHA_B,),
        T3,
        evaluated_strategy_version_id="strategy-2",
        evaluated_model_version_id="model-1",
    )
    experiment = ExperimentRecord(
        "experiment-3",
        "protocol-1",
        "dataset-1",
        "features-1",
        "strategy-2",
        "eval-3",
        8,
        SHA_B,
        ResearchOutcome.HARMFUL,
        T2,
        model_version_id="model-1",
        completed_at=T3,
        notes="Independent harmful result.",
    )
    registry.append(bundle)
    registry.append(experiment)

    assert registry.causal_precedes(
        "Postmortem",
        premature_postmortem.postmortem_id,
        "Experiment",
        experiment.experiment_id,
    )
    assert not registry.causal_precedes(
        "Experiment",
        experiment.experiment_id,
        "Postmortem",
        premature_postmortem.postmortem_id,
    )

    with pytest.raises(
        RuntimeError,
        match="causally follow",
    ):
        search_negative_results(
            registry,
            "передчасний",
            as_of=T3,
        )


def _append_negative_result_chronology_fixture(
    registry,
    *,
    prefix,
    question_at,
    hypothesis_at,
    protocol_at,
    experiment_created_at,
    experiment_completed_at,
    notes,
    research_question_sha256=None,
    hypothesis_sha256=None,
):
    question = ResearchQuestion(
        f"{prefix}-question",
        f"{prefix} frozen question",
        SHA_A,
        question_at,
    )
    hypothesis = Hypothesis(
        f"{prefix}-hypothesis",
        question.question_id,
        f"{prefix} candidate improves the primary metric.",
        "primary > champion",
        "primary <= champion",
        "roi",
        ("drawdown",),
        hypothesis_at,
    )
    binding = ScientificProtocolBinding(
        research_protocol_id=f"{prefix}-protocol",
        research_question_id=question.question_id,
        research_question_sha256=(
            _payload_sha(question)
            if research_question_sha256 is None
            else research_question_sha256
        ),
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=(
            _payload_sha(hypothesis)
            if hypothesis_sha256 is None
            else hypothesis_sha256
        ),
        inclusion_criteria="frozen",
        exclusion_criteria="invalid provenance",
        lawful_source_requirements="lawful fixture",
        causal_cutoff=question_at,
        evaluation_design="walk-forward holdout",
        feature_set_version="v1",
        uncertainty_method="bootstrap intervals",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split",),
        random_seed_policy="fixed",
        stopping_rule="one final evaluation",
        promotion_rule=_frozen_promotion_rule_text(),
        expected_artifacts=("evaluation bundle",),
        code_config_sha256=SHA_B,
        frozen_at_utc=protocol_at,
    )
    protocol = ResearchProtocol(binding, SHA_C, SHA_D, SHA_A, protocol_at)
    experiment = ExperimentRecord(
        f"{prefix}-experiment",
        protocol.record_id,
        f"{prefix}-dataset",
        f"{prefix}-features",
        f"{prefix}-strategy",
        f"{prefix}-evaluation",
        19,
        SHA_B,
        ResearchOutcome.NULL,
        experiment_created_at,
        completed_at=experiment_completed_at,
        notes=notes,
    )

    for record in (question, hypothesis, protocol, experiment):
        registry.append(record)
    return experiment


def test_declared_lineage_rejects_protocol_available_after_experiment_start(tmp_path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    experiment = _append_negative_result_chronology_fixture(
        registry,
        prefix="posthoc",
        question_at="2026-01-01T08:00:00+00:00",
        hypothesis_at="2026-01-01T09:00:00+00:00",
        protocol_at="2026-01-01T12:00:00+00:00",
        experiment_created_at="2026-01-01T10:00:00+00:00",
        experiment_completed_at="2026-01-01T11:00:00+00:00",
        notes="chronology inversion marker",
    )

    assert registry.causal_precedes(
        "ResearchProtocol",
        "posthoc-protocol",
        "Experiment",
        experiment.experiment_id,
    )

    with pytest.raises(RuntimeError, match="availability chronology"):
        search_negative_results(
            registry,
            "chronology inversion marker",
            as_of="2026-01-01T13:00:00+00:00",
        )


def test_declared_lineage_chronology_compares_timezone_offsets_by_utc_instant(
    tmp_path,
):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific_registry.json"
    )
    experiment = _append_negative_result_chronology_fixture(
        registry,
        prefix="offset",
        question_at="2026-01-01T08:00:00+02:00",
        hypothesis_at="2026-01-01T06:30:00+00:00",
        protocol_at="2026-01-01T08:00:00+01:00",
        experiment_created_at="2026-01-01T07:30:00+00:00",
        experiment_completed_at="2026-01-01T08:00:00+00:00",
        notes="offset chronology marker",
    )

    hits = search_negative_results(
        registry,
        "offset chronology marker",
        as_of="2026-01-01T09:00:00+00:00",
    )

    assert [hit.experiment_id for hit in hits] == [experiment.experiment_id]


@pytest.mark.parametrize(
    ("sha_field", "mismatched_sha", "message"),
    [
        ("research_question_sha256", "f" * 64, "research question hash"),
        ("hypothesis_sha256", "e" * 64, "hypothesis hash"),
    ],
)
def test_negative_result_retrieval_rejects_mismatched_frozen_lineage_hash(
    tmp_path,
    sha_field,
    mismatched_sha,
    message,
):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / f"scientific_registry_{sha_field}.json"
    )
    kwargs = {
        "research_question_sha256": None,
        "hypothesis_sha256": None,
    }
    kwargs[sha_field] = mismatched_sha
    experiment = _append_negative_result_chronology_fixture(
        registry,
        prefix=f"hash-{sha_field}",
        question_at="2026-01-01T06:00:00+00:00",
        hypothesis_at="2026-01-01T06:30:00+00:00",
        protocol_at="2026-01-01T07:00:00+00:00",
        experiment_created_at="2026-01-01T07:30:00+00:00",
        experiment_completed_at="2026-01-01T08:00:00+00:00",
        notes="frozen lineage hash marker",
        **kwargs,
    )

    assert registry.causal_precedes(
        "ResearchProtocol",
        f"hash-{sha_field}-protocol",
        "Experiment",
        experiment.experiment_id,
    )
    with pytest.raises(RuntimeError, match=message):
        search_negative_results(
            registry,
            "frozen lineage hash marker",
            as_of="2026-01-01T09:00:00+00:00",
        )
