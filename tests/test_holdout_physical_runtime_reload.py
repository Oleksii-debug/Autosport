"""Reload regressions run in isolated interpreters.

Reloading point_in_time_evidence replaces dataclasses imported by other tests.
The product must retain the physical-holdout fence on reload, but pytest's
shared interpreter must not be left with stale class identities.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


_PROBE = r"""
import importlib

from autosport import _holdout_physical_content_guard as physical
from autosport import _point_in_time_authority_runtime_repair as runtime
from autosport import _strategy_model_factory_impl as factory
from autosport import point_in_time_evidence as evidence

target = {"runtime": runtime, "evidence": evidence}[TARGET]
importlib.reload(target)

assert evidence._holdout_freshness_id is physical._physical_freshness_id
assert evidence.HoldoutConsumptionLedger._load is physical._load_with_physical_reindex
assert factory._holdout_consumed_by_other_evidence is physical._factory_holdout_consumed
assert (
    factory.ExperimentRunner.run_baseline_candidate
    is physical._run_with_physical_holdout_context
)
assert runtime._install_runtime_guards is physical._runtime_install_with_physical_guard

kwargs = {
    "dataset_manifest_sha256": "a" * 64,
    "confirmation_trial_family_id": "family-a",
    "source_identity": "source-a",
    "license_identity": "license-a",
}
first = evidence._holdout_freshness_id(**kwargs)
kwargs.update(
    source_identity="source-b",
    license_identity="license-b",
    confirmation_trial_family_id="family-b",
)
assert evidence._holdout_freshness_id(**kwargs) == first
"""


def _assert_reload_preserves_physical_guard(module: str) -> None:
    assert module in {"runtime", "evidence"}
    script = "TARGET = " + repr(module) + "\n" + _PROBE
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_runtime_repair_reload_reinstalls_physical_holdout_guard() -> None:
    _assert_reload_preserves_physical_guard("runtime")


def test_point_in_time_module_reload_reinstalls_physical_holdout_guard() -> None:
    _assert_reload_preserves_physical_guard("evidence")
