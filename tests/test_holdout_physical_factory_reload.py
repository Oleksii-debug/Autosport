"""Factory reload may not reopen holdout freshness or poison other tests."""

from __future__ import annotations

import subprocess
import sys


def test_factory_impl_reload_reinstalls_physical_holdout_guard() -> None:
    # Reloading _strategy_model_factory_impl replaces TrainingPoint, PromotionRule,
    # and other exact-type authorities. Exercise the real reload in an independent
    # interpreter rather than changing class identities for the rest of pytest.
    script = r"""
import importlib
from autosport import _holdout_physical_content_guard as physical
from autosport import _strategy_model_factory_impl as factory

importlib.reload(factory)

assert factory._holdout_consumed_by_other_evidence is physical._factory_holdout_consumed
assert (
    factory.ExperimentRunner.run_baseline_candidate
    is physical._run_with_physical_holdout_context
)
assert physical._ORIGINAL_FACTORY_HELPER is not physical._factory_holdout_consumed
assert physical._ORIGINAL_FACTORY_RUN is not physical._run_with_physical_holdout_context
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
