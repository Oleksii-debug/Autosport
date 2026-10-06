from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import autosport.product_paper_decision_cycle as cycle_module
from autosport.continuous_session import SessionState
from autosport.product_paper_decision_cycle import (
    ProductDecisionInput,
    ProductPaperDecisionCycle,
)


CANONICAL_STRATEGY_SOURCE_SHA = "a" * 64
IMPOSTOR_FACTORY_SOURCE_SHA = "b" * 64


class _FakeAuthority:
    def __init__(self) -> None:
        self.contract = SimpleNamespace(max_quote_age_seconds=Decimal("5"))


class _ImpostorIntentFactory:
    """Different executable implementation wearing a valid StrategyVersion ID."""

    strategy_version_id = "strategy-v1"
    source_sha256 = IMPOSTOR_FACTORY_SOURCE_SHA

    def __call__(self, *_args, **_kwargs):
        return ()


class _FakeRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path


class _FakeExecutionConfig:
    max_quote_age_ms = 5_000


class _FakeRuntime:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.manifest = SimpleNamespace(initial_bankroll="100", source_id="provider-a")
        self._status = SimpleNamespace(
            state=SessionState.RUNNING,
            source_provider_unavailable=False,
            source_unresolved_gap_delta_ids=(),
            source_state_projection_backlog=False,
            invalidation_full_refresh_required=False,
            invalidation_pending_count=0,
        )

    def status(self):
        return self._status


class _FakeLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)


class _FakeExecution:
    def __init__(self, *, book, ledger, config, max_quote_age, paper_book_path) -> None:
        self.book = book
        self.ledger = ledger
        self.config = config
        self.max_quote_age = max_quote_age
        self.paper_book_path = Path(paper_book_path)


class _CapturingLoop:
    instances: list["_CapturingLoop"] = []

    def __init__(self, workspace, **kwargs) -> None:
        self.workspace = Path(workspace)
        self.kwargs = kwargs
        type(self).instances.append(self)

    def register_input(self, _input_id: str, **_selectors) -> None:
        return None

    def run_cycle(self):
        return SimpleNamespace(status="DECIDED")

    def close(self) -> None:
        return None


class ProductPaperDecisionFactorySourceBindingFalsifier(unittest.TestCase):
    def setUp(self) -> None:
        _CapturingLoop.instances.clear()

    def _patch_dependencies(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(
            patch.object(cycle_module, "AutonomousProductRuntime", _FakeRuntime)
        )
        stack.enter_context(
            patch.object(cycle_module, "EconomicDecisionAuthority", _FakeAuthority)
        )
        stack.enter_context(
            patch.object(cycle_module, "ScientificRegistry", _FakeRegistry)
        )
        stack.enter_context(
            patch.object(cycle_module, "PaperExecutionModelConfig", _FakeExecutionConfig)
        )
        stack.enter_context(
            patch.object(cycle_module, "PaperExecutionLedger", _FakeLedger)
        )
        stack.enter_context(
            patch.object(cycle_module, "JsonlDecisionLedger", _FakeLedger)
        )
        stack.enter_context(
            patch.object(cycle_module, "PaperExecutionAdoptionRuntime", _FakeExecution)
        )
        stack.enter_context(
            patch.object(cycle_module, "PersistentLiveDecisionLoop", _CapturingLoop)
        )
        stack.enter_context(
            patch.object(
                cycle_module.LiveIntentProvenance,
                "from_registry",
                return_value=SimpleNamespace(
                    strategy_version_id="strategy-v1",
                    source_sha256=CANONICAL_STRATEGY_SOURCE_SHA,
                ),
            )
        )
        return stack

    def test_impostor_callable_cannot_mint_registered_strategy_execution_authority(self) -> None:
        """Matching strategy_version_id is not executable-strategy provenance.

        ScientificRegistry says strategy-v1 is bound to CANONICAL_STRATEGY_SOURCE_SHA.
        The supplied callable is deliberately a different implementation.  The
        supported product composition must reject this before creating canonical
        PAPER economic state or handing the callable to the decision loop.

        This falsifier is intentionally neutral about the final binding mechanism:
        #1420 may consume #968 factory reproducibility evidence or a stronger
        canonical product-issued executable-artifact authority.  It only forbids
        caller identity strings from being sufficient authority.
        """

        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            runtime = _FakeRuntime(workspace)
            registry = _FakeRegistry(workspace / "scientific_registry.json")
            cycle = ProductPaperDecisionCycle(
                runtime,
                loop_id="product-paper-loop",
                authority=_FakeAuthority(),
                intent_factory=_ImpostorIntentFactory(),
                scientific_registry=registry,
                execution_config=_FakeExecutionConfig(),
                max_quote_age=timedelta(seconds=5),
                inputs=(
                    ProductDecisionInput(
                        "selection-input",
                        source_ids=("provider-a",),
                        selection_ids=("selection-a",),
                    ),
                ),
            )

            with self.assertRaises(Exception):
                cycle._run_decision_cycle()

            self.assertEqual(
                _CapturingLoop.instances,
                [],
                "unverified executable factory reached the durable decision composition",
            )
            self.assertFalse(
                (workspace / "paper_book.json").exists(),
                "unverified executable factory caused canonical PAPER state creation",
            )


if __name__ == "__main__":
    unittest.main()
