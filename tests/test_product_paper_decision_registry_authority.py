from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import autosport.product_paper_decision_cycle as cycle_module
from autosport.product_paper_decision_cycle import (
    ProductDecisionInput,
    ProductPaperDecisionCycle,
)


class _Runtime:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.manifest = SimpleNamespace(initial_bankroll="100")


class _Authority:
    def __init__(self) -> None:
        self.contract = SimpleNamespace(max_quote_age_seconds=Decimal("5"))


class _IntentFactory:
    strategy_version_id = "strategy-v1"

    def __call__(self, *_args, **_kwargs):
        return ()


class _ExecutionConfig:
    max_quote_age_ms = 5_000


class _Registry:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)


class _RegistrySubclass(_Registry):
    def get(self, *_args, **_kwargs):
        return SimpleNamespace(payload={"caller_controlled": True})


def _build(
    workspace: Path,
    registry: _Registry,
) -> ProductPaperDecisionCycle:
    with (
        patch.object(cycle_module, "AutonomousProductRuntime", _Runtime),
        patch.object(cycle_module, "EconomicDecisionAuthority", _Authority),
        patch.object(cycle_module, "ScientificRegistry", _Registry),
        patch.object(cycle_module, "PaperExecutionModelConfig", _ExecutionConfig),
    ):
        return ProductPaperDecisionCycle(
            _Runtime(workspace),
            loop_id="product-paper-loop",
            authority=_Authority(),
            intent_factory=_IntentFactory(),
            scientific_registry=registry,
            execution_config=_ExecutionConfig(),
            max_quote_age=timedelta(seconds=5),
            inputs=(ProductDecisionInput("input-a"),),
        )


def test_rejects_isinstance_compatible_registry_subclass(tmp_path: Path) -> None:
    with (
        patch.object(cycle_module, "AutonomousProductRuntime", _Runtime),
        patch.object(cycle_module, "EconomicDecisionAuthority", _Authority),
        patch.object(cycle_module, "ScientificRegistry", _Registry),
        patch.object(cycle_module, "PaperExecutionModelConfig", _ExecutionConfig),
    ):
        with pytest.raises(
            TypeError,
            match="canonical ScientificRegistry",
        ):
            ProductPaperDecisionCycle(
                _Runtime(tmp_path),
                loop_id="product-paper-loop",
                authority=_Authority(),
                intent_factory=_IntentFactory(),
                scientific_registry=_RegistrySubclass(
                    tmp_path / "scientific_registry.json"
                ),
                execution_config=_ExecutionConfig(),
                max_quote_age=timedelta(seconds=5),
                inputs=(ProductDecisionInput("input-a"),),
            )


def test_cycle_binds_registry_path_not_caller_instance(tmp_path: Path) -> None:
    original_path = tmp_path / "scientific_registry.json"
    caller_registry = _Registry(original_path)
    cycle = _build(tmp_path, caller_registry)

    caller_registry.path = tmp_path / "caller-mutated-registry.json"

    with patch.object(cycle_module, "ScientificRegistry", _Registry):
        reconstructed = cycle._load_current_scientific_registry()

    assert reconstructed is not caller_registry
    assert type(reconstructed) is _Registry
    assert reconstructed.path.resolve(strict=False) == original_path.resolve(strict=False)
