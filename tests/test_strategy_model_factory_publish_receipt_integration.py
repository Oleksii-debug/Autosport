from pathlib import Path
import runpy

import autosport.strategy_model_factory as factory_module
from autosport.strategy_model_factory import ExperimentRunner


def _canonical_factory_test_helpers():
    return runpy.run_path(
        str(Path(__file__).with_name("test_strategy_model_factory.py"))
    )


def test_normal_baseline_candidate_success_publishes_transaction_receipt(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "machine-authority").resolve()),
    )
    helpers = _canonical_factory_test_helpers()
    registry, _, rule, store, _, _ = helpers["_factory_foundation"](tmp_path)

    result = helpers["_run_candidate"](
        ExperimentRunner(registry, store),
        helpers["_candidate_points"](),
        rule,
    )

    model_sha256 = store.sha256("model", "model-v2")
    receipt = store.publication_receipt(
        "model",
        "model-v2",
        expected_sha256=model_sha256,
    )

    assert result.candidate_model_version_id == "model-v2"
    assert receipt["final_registry_sha256"] == factory_module._registry_state_sha256(
        registry._read()
    )
    assert {
        (artifact["kind"], artifact["identity"], artifact["sha256"])
        for artifact in receipt["artifacts"]
    } >= {("model", "model-v2", model_sha256)}
    assert not factory_module._publish_transaction_path(registry).exists()
