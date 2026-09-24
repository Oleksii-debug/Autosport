from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, RLock, Thread
from types import SimpleNamespace
from unittest.mock import patch

import autosport.product_paper_decision_cycle as cycle_module
from autosport.continuous_session import SessionState
from autosport.product_paper_decision_cycle import (
    ProductDecisionInput,
    ProductPaperDecisionCycle,
)


class _FakeAuthority:
    def __init__(self) -> None:
        self.contract = SimpleNamespace(max_quote_age_seconds=Decimal("5"))


class _FakeIntentFactory:
    strategy_version_id = "strategy-v1"

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
        self.manifest = SimpleNamespace(initial_bankroll="100")
        self._operation_fence = RLock()
        self.close_entered = Event()
        self._status = SimpleNamespace(
            state=SessionState.RUNNING,
            source_provider_unavailable=False,
            source_unresolved_gap_delta_ids=(),
            source_state_projection_backlog=False,
            invalidation_full_refresh_required=False,
            invalidation_pending_count=0,
        )
        self._product_tick = SimpleNamespace(
            source_provider_unavailable=False,
            invalidation_backlog=False,
        )

    def status(self):
        with self._operation_fence:
            return self._status

    def tick(self):
        with self._operation_fence:
            return self._product_tick

    def close(self) -> None:
        with self._operation_fence:
            self.close_entered.set()


class _FakeLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)


class _FakeExecution:
    def __init__(self, **_kwargs) -> None:
        pass


class _BlockingLoop:
    run_entered = Event()
    allow_run_return = Event()

    def __init__(self, workspace, **kwargs) -> None:
        self.workspace = Path(workspace)
        self.kwargs = kwargs

    def register_input(self, _input_id: str, **_selectors) -> None:
        return None

    def run_cycle(self):
        type(self).run_entered.set()
        if not type(self).allow_run_return.wait(timeout=5):
            raise AssertionError("test did not release blocked PAPER decision")
        return SimpleNamespace(status="DECIDED")

    def close(self) -> None:
        return None


class ProductPaperDecisionCloseSerializationTests(unittest.TestCase):
    def setUp(self) -> None:
        _BlockingLoop.run_entered = Event()
        _BlockingLoop.allow_run_return = Event()

    def test_runtime_close_cannot_release_authority_during_paper_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            runtime = _FakeRuntime(workspace)
            registry = _FakeRegistry(workspace / "scientific_registry.json")

            with (
                patch.object(cycle_module, "AutonomousProductRuntime", _FakeRuntime),
                patch.object(cycle_module, "EconomicDecisionAuthority", _FakeAuthority),
                patch.object(cycle_module, "ScientificRegistry", _FakeRegistry),
                patch.object(cycle_module, "PaperExecutionModelConfig", _FakeExecutionConfig),
                patch.object(cycle_module, "PaperExecutionLedger", _FakeLedger),
                patch.object(cycle_module, "JsonlDecisionLedger", _FakeLedger),
                patch.object(cycle_module, "PaperExecutionAdoptionRuntime", _FakeExecution),
                patch.object(cycle_module, "PersistentLiveDecisionLoop", _BlockingLoop),
                patch.object(
                    cycle_module.LiveIntentProvenance,
                    "from_registry",
                    return_value=SimpleNamespace(strategy_version_id="strategy-v1"),
                ),
            ):
                cycle = ProductPaperDecisionCycle(
                    runtime,
                    loop_id="product-paper-loop",
                    authority=_FakeAuthority(),
                    intent_factory=_FakeIntentFactory(),
                    scientific_registry=registry,
                    execution_config=_FakeExecutionConfig(),
                    max_quote_age=timedelta(seconds=5),
                    inputs=(ProductDecisionInput("input-a"),),
                )

                cycle_errors: list[BaseException] = []
                close_errors: list[BaseException] = []

                def run_cycle() -> None:
                    try:
                        cycle.tick()
                    except BaseException as exc:  # pragma: no cover - asserted below
                        cycle_errors.append(exc)

                def run_close() -> None:
                    try:
                        runtime.close()
                    except BaseException as exc:  # pragma: no cover - asserted below
                        close_errors.append(exc)

                cycle_thread = Thread(target=run_cycle, name="paper-cycle")
                close_thread = Thread(target=run_close, name="runtime-close")
                cycle_thread.start()
                self.assertTrue(_BlockingLoop.run_entered.wait(timeout=2))
                close_thread.start()

                try:
                    self.assertFalse(
                        runtime.close_entered.wait(timeout=0.2),
                        "runtime close entered while PAPER effects still held product authority",
                    )
                finally:
                    _BlockingLoop.allow_run_return.set()
                    cycle_thread.join(timeout=2)
                    close_thread.join(timeout=2)

                self.assertFalse(cycle_thread.is_alive())
                self.assertFalse(close_thread.is_alive())
                self.assertEqual(cycle_errors, [])
                self.assertEqual(close_errors, [])
                self.assertTrue(runtime.close_entered.is_set())


if __name__ == "__main__":
    unittest.main()
