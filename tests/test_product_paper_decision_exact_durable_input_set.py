from __future__ import annotations

from contextlib import contextmanager
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import autosport.product_paper_decision_cycle as cycle_module
from autosport.continuous_session import SessionState
from autosport.live_decision_loop import LiveDecisionProgressError
from autosport.product_paper_decision_cycle import (
    ProductDecisionInput,
    ProductPaperDecisionCycle,
    ProductPaperDecisionCycleError,
)


class _FakeRuntime:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.manifest = SimpleNamespace(
            source_id="provider-a",
            initial_bankroll="100",
        )

    def status(self):
        return SimpleNamespace(state=SessionState.RUNNING)

    @contextmanager
    def decision_commit_fence(self):
        yield


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


class _FakeLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)


class _FakeExecution:
    def __init__(self, **_kwargs) -> None:
        pass


class _FakeDependencies:
    def __init__(self, owner: "_FakeLoop") -> None:
        self._owner = owner

    @property
    def input_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._owner.durable_input_ids))


class _FakeLoop:
    instances: list["_FakeLoop"] = []

    def __init__(self, _workspace, **_kwargs) -> None:
        # Simulate a durable registry restored from an earlier configuration A+B.
        self.durable_input_ids = {"input-a", "input-b"}
        self.dependencies = _FakeDependencies(self)
        self.closed = False
        type(self).instances.append(self)

    def register_input(self, input_id: str, **_selectors) -> None:
        self.durable_input_ids.add(input_id)

    def unregister_input(self, input_id: str) -> bool:
        if input_id not in self.durable_input_ids:
            return False
        self.durable_input_ids.remove(input_id)
        return True

    def run_cycle(self):
        return SimpleNamespace(status="DECIDED")

    def close(self) -> None:
        self.closed = True


class ProductPaperDecisionExactDurableInputSetTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeLoop.instances.clear()

    def _cycle(self, workspace: Path) -> ProductPaperDecisionCycle:
        runtime = _FakeRuntime(workspace)
        return ProductPaperDecisionCycle(
            runtime,
            loop_id="exact-durable-input-set-test",
            authority=_FakeAuthority(),
            intent_factory=_FakeIntentFactory(),
            scientific_registry=_FakeRegistry(
                workspace / "scientific_registry.json"
            ),
            execution_config=_FakeExecutionConfig(),
            max_quote_age=timedelta(seconds=5),
            inputs=(
                ProductDecisionInput(
                    "input-a",
                    source_ids=("provider-a",),
                    selection_ids=("selection-a",),
                ),
            ),
            clock=lambda: datetime(
                2026,
                9,
                22,
                6,
                0,
                tzinfo=timezone.utc,
            ),
        )

    def test_omitted_durable_input_cannot_silently_survive_reconfiguration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with (
                patch.object(
                    cycle_module,
                    "AutonomousProductRuntime",
                    _FakeRuntime,
                ),
                patch.object(
                    cycle_module,
                    "EconomicDecisionAuthority",
                    _FakeAuthority,
                ),
                patch.object(
                    cycle_module,
                    "ScientificRegistry",
                    _FakeRegistry,
                ),
                patch.object(
                    cycle_module,
                    "PaperExecutionModelConfig",
                    _FakeExecutionConfig,
                ),
                patch.object(
                    cycle_module,
                    "PaperExecutionLedger",
                    _FakeLedger,
                ),
                patch.object(
                    cycle_module,
                    "JsonlDecisionLedger",
                    _FakeLedger,
                ),
                patch.object(
                    cycle_module,
                    "PaperExecutionAdoptionRuntime",
                    _FakeExecution,
                ),
                patch.object(
                    cycle_module,
                    "PersistentLiveDecisionLoop",
                    _FakeLoop,
                ),
                patch.object(
                    cycle_module.LiveIntentProvenance,
                    "from_registry",
                    return_value=SimpleNamespace(
                        strategy_version_id="strategy-v1",
                    ),
                ),
            ):
                cycle = self._cycle(workspace)

                try:
                    cycle._run_decision_cycle()
                except (
                    ProductPaperDecisionCycleError,
                    LiveDecisionProgressError,
                ):
                    # Explicit config-drift fail-closed is an allowed repair.
                    return

                self.assertEqual(len(_FakeLoop.instances), 1)
                self.assertEqual(
                    _FakeLoop.instances[0].dependencies.input_ids,
                    ("input-a",),
                    "omitted durable input-b survived into the decision universe",
                )


if __name__ == "__main__":
    unittest.main()
