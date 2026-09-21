from __future__ import annotations

import importlib

from autosport import _holdout_physical_content_guard as physical
from autosport import _point_in_time_authority_runtime_repair as runtime
from autosport import _strategy_model_factory_impl as factory
from autosport import point_in_time_evidence as evidence


SHA_A = "a" * 64


def _assert_physical_guard_installed() -> None:
    assert evidence._holdout_freshness_id is physical._physical_freshness_id
    assert evidence.HoldoutConsumptionLedger._load is physical._load_with_physical_reindex
    assert factory._holdout_consumed_by_other_evidence is physical._factory_holdout_consumed
    assert (
        factory.ExperimentRunner.run_baseline_candidate
        is physical._run_with_physical_holdout_context
    )
    assert (
        runtime._install_runtime_guards
        is physical._runtime_install_with_physical_guard
    )
    first = evidence._holdout_freshness_id(
        dataset_manifest_sha256=SHA_A,
        source_identity="source-a",
        license_identity="license-a",
        confirmation_trial_family_id="family-a",
    )
    relabelled = evidence._holdout_freshness_id(
        dataset_manifest_sha256=SHA_A,
        source_identity="source-b",
        license_identity="license-b",
        confirmation_trial_family_id="family-b",
    )
    assert first == relabelled


def test_runtime_repair_reload_reinstalls_physical_holdout_guard() -> None:
    importlib.reload(runtime)
    _assert_physical_guard_installed()


def test_point_in_time_module_reload_reinstalls_physical_holdout_guard() -> None:
    importlib.reload(evidence)
    _assert_physical_guard_installed()
