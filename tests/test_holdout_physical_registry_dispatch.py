from __future__ import annotations

from pathlib import Path

import pytest

from autosport import _holdout_physical_content_guard as guard
from autosport import _strategy_model_factory_impl as factory
from autosport.scientific_registry import ScientificRegistry


class _CallerControlledRegistry(ScientificRegistry):
    def get(self, record_type: str, record_id: str):
        raise AssertionError("caller-controlled registry dispatch must not run")


def _run_until_registry_fence(runner: factory.ExperimentRunner) -> None:
    # The physical-holdout wrapper must reject the registry before any candidate,
    # evaluator, artifact, or scientific-foundation input can influence execution.
    runner.run_baseline_candidate(  # type: ignore[arg-type]
        None,
        (),
        rule=None,
    )


def test_product_runner_rejects_scientific_registry_subclass(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "scientific-registry.json"
    ScientificRegistry.initialize_pristine(registry_path)
    forged = _CallerControlledRegistry(registry_path)
    runner = factory.ExperimentRunner(
        forged,
        factory.FactoryArtifactStore(tmp_path / "artifacts"),
    )

    with pytest.raises(
        ValueError,
        match="exact canonical ScientificRegistry",
    ):
        _run_until_registry_fence(runner)


def test_product_runner_rejects_instance_read_dispatch_shadow(
    tmp_path: Path,
) -> None:
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    registry.get = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runner = factory.ExperimentRunner(
        registry,
        factory.FactoryArtifactStore(tmp_path / "artifacts"),
    )

    with pytest.raises(
        ValueError,
        match="instance dispatch changed",
    ):
        _run_until_registry_fence(runner)


def test_product_runner_rejects_class_read_dispatch_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    runner = factory.ExperimentRunner(
        registry,
        factory.FactoryArtifactStore(tmp_path / "artifacts"),
    )

    monkeypatch.setattr(
        ScientificRegistry,
        "get",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        ValueError,
        match="read dispatch changed",
    ):
        _run_until_registry_fence(runner)
