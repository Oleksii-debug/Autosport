from __future__ import annotations

import importlib

from autosport import _holdout_physical_content_guard as physical
from autosport import _strategy_model_factory_impl as factory


def test_factory_impl_reload_reinstalls_physical_holdout_guard() -> None:
    importlib.reload(factory)

    assert factory._holdout_consumed_by_other_evidence is physical._factory_holdout_consumed
    assert (
        factory.ExperimentRunner.run_baseline_candidate
        is physical._run_with_physical_holdout_context
    )
    assert physical._ORIGINAL_FACTORY_HELPER is not physical._factory_holdout_consumed
    assert physical._ORIGINAL_FACTORY_RUN is not physical._run_with_physical_holdout_context
