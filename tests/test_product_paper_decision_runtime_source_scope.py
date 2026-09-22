from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import autosport.product_paper_decision_cycle as cycle_module
from autosport.product_paper_decision_cycle import (
    ProductDecisionInput,
    ProductPaperDecisionCycle,
    ProductPaperDecisionCycleError,
)


class _FakeRuntime:
    def __init__(self, workspace: Path, *, source_id: str = "provider-a") -> None:
        self.workspace = workspace
        self.manifest = SimpleNamespace(
            source_id=source_id,
            initial_bankroll="100",
        )


class _FakeAuthority:
    def __init__(self) -> None:
        self.contract = SimpleNamespace(max_quote_age_seconds=Decimal("5"))


class _FakeRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path


class _FakeExecutionConfig:
    max_quote_age_ms = 5_000


class _FakeIntentFactory:
    strategy_version_id = "strategy-v1"

    def __call__(self, *_args, **_kwargs):
        return ()


class ProductPaperDecisionRuntimeSourceScopeTests(unittest.TestCase):
    def _construct(
        self,
        workspace: Path,
        *,
        source_ids: tuple[str, ...] | None,
    ) -> ProductPaperDecisionCycle:
        runtime = _FakeRuntime(workspace)
        authority = _FakeAuthority()
        registry = _FakeRegistry(workspace / "scientific_registry.json")
        execution_config = _FakeExecutionConfig()
        factory = _FakeIntentFactory()

        with (
            patch.object(cycle_module, "AutonomousProductRuntime", _FakeRuntime),
            patch.object(cycle_module, "EconomicDecisionAuthority", _FakeAuthority),
            patch.object(cycle_module, "ScientificRegistry", _FakeRegistry),
            patch.object(
                cycle_module,
                "PaperExecutionModelConfig",
                _FakeExecutionConfig,
            ),
        ):
            return ProductPaperDecisionCycle(
                runtime,
                loop_id="product-source-scope-test",
                authority=authority,
                intent_factory=factory,
                scientific_registry=registry,
                execution_config=execution_config,
                max_quote_age=timedelta(seconds=5),
                inputs=(
                    ProductDecisionInput(
                        "input-a",
                        source_ids=source_ids,
                        selection_ids=("selection-a",),
                    ),
                ),
            )

    def test_unscoped_input_cannot_escape_runtime_source_health_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ProductPaperDecisionCycleError,
                "runtime source",
            ):
                self._construct(Path(directory), source_ids=None)

    def test_foreign_source_input_cannot_escape_runtime_source_health_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ProductPaperDecisionCycleError,
                "runtime source",
            ):
                self._construct(Path(directory), source_ids=("provider-b",))

    def test_mixed_source_input_cannot_expand_beyond_runtime_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ProductPaperDecisionCycleError,
                "runtime source",
            ):
                self._construct(
                    Path(directory),
                    source_ids=("provider-a", "provider-b"),
                )

    def test_exact_runtime_source_input_remains_admissible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cycle = self._construct(
                Path(directory),
                source_ids=("provider-a",),
            )
            self.assertEqual(
                cycle.inputs[0].source_ids,
                ("provider-a",),
            )


if __name__ == "__main__":
    unittest.main()
