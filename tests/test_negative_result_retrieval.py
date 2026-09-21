from __future__ import annotations

import hashlib
import json

import pytest

from test_scientific_registry_index import SHA_A, SHA_B, SHA_C, T2, T3, _seed_registry

from autosport.negative_result_retrieval import search_negative_results
from autosport.scientific_registry import (
    EvaluationBundleRef,
    ExperimentRecord,
    Postmortem,
    ResearchOutcome,
    ScientificRegistry,
)


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


def test_self_consistent_unknown_experiment_outcome_fails_closed(tmp_path):
    path = tmp_path / "scientific_registry.json"
    _seed_registry(path)

    state = json.loads(path.read_text(encoding="utf-8"))
    experiment = next(
        entry
        for entry in state["records"]
        if entry["record_type"] == "Experiment"
        and entry["record_id"] == "experiment-2"
    )
    experiment["payload"]["outcome"] = "UNKNOWN_RESULT"
    envelope = {
        "record_type": experiment["record_type"],
        "record_id": experiment["record_id"],
        "available_at": experiment["available_at"],
        "payload": experiment["payload"],
    }
    canonical = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    experiment["record_sha256"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    path.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    registry = ScientificRegistry(path)
    with pytest.raises(RuntimeError, match="unsupported outcome"):
        search_negative_results(registry, "candidate", as_of=T3)
