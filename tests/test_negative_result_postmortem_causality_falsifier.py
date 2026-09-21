from __future__ import annotations

import pytest

from test_scientific_registry_index import SHA_A, SHA_B, SHA_C, T2, T3, _seed_registry

from autosport.negative_result_retrieval import (
    NegativeResultRetrievalError,
    search_negative_results,
)
from autosport.scientific_registry import (
    EvaluationBundleRef,
    ExperimentRecord,
    Postmortem,
    ResearchOutcome,
)


def test_postmortem_must_not_predate_referenced_experiment(tmp_path):
    """A postmortem cannot become semantic evidence before its experiment exists."""

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
        NegativeResultRetrievalError,
        match="causal|preced|postmortem|experiment",
    ):
        search_negative_results(
            registry,
            "передчасний",
            as_of=T3,
        )
