from __future__ import annotations

import pytest

from autosport._strategy_model_factory_impl import MeanBaselineModel
from autosport.registered_strategy_model_runtime import (
    RegisteredStrategyModelRuntime,
    RegisteredStrategyModelRuntimeError,
)


def test_direct_constructor_cannot_mint_promoted_runtime_authority() -> None:
    model = MeanBaselineModel(
        model_id="model-v1",
        training_cutoff="2026-01-01T00:00:00Z",
        mean_target=0.5,
        training_count=10,
    )

    with pytest.raises(
        RegisteredStrategyModelRuntimeError,
        match="must be issued by the canonical durable resolver",
    ):
        RegisteredStrategyModelRuntime(
            strategy_version_id="strategy-v1",
            strategy_record_sha256="a" * 64,
            model_version_id="model-v1",
            model_record_sha256="b" * 64,
            model_artifact_sha256="c" * 64,
            model_identity_sha256=model.identity_sha256,
            promotion_record_sha256="d" * 64,
            experiment_id="experiment-v1",
            reproducibility_bundle_sha256="e" * 64,
            canonical_strategy_id="strategy",
            research_protocol_id="protocol-v1",
            dataset_snapshot_id="dataset-v1",
            feature_set_id="features-v1",
            authority_as_of="2026-01-02T00:00:00Z",
            training_cutoff=model.training_cutoff,
            _model=model,
        )
