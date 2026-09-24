from __future__ import annotations

import tempfile
from contextlib import ExitStack
from pathlib import Path
from threading import Event, RLock, Thread
from unittest.mock import patch

from autosport.continuous_session import SessionState
import autosport.product_paper_decision_cycle as cycle_module
from autosport.product_paper_decision_cycle import ProductPaperDecisionCycle
import test_product_paper_decision_cycle as base


class _FencedFakeRuntime(base._FakeRuntime):
    def __init__(self, workspace: Path, *, initial_bankroll: str = "100") -> None:
        super().__init__(workspace, initial_bankroll=initial_bankroll)
        self._operation_fence = RLock()

    def tick(self):
        with self._operation_fence:
            return super().tick()

    def status(self):
        with self._operation_fence:
            return super().status()


class _BlockingLoop(base._FakeLoop):
    entered = Event()
    allow_return = Event()

    def run_cycle(self):
        type(self).entered.set()
        if not type(self).allow_return.wait(timeout=5):
            raise AssertionError("test did not release the in-flight decision cycle")
        return super().run_cycle()


def _patch_dependencies() -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch.object(cycle_module, "AutonomousProductRuntime", _FencedFakeRuntime))
    stack.enter_context(patch.object(cycle_module, "EconomicDecisionAuthority", base._FakeAuthority))
    stack.enter_context(patch.object(cycle_module, "ScientificRegistry", base._FakeRegistry))
    stack.enter_context(patch.object(cycle_module, "PaperExecutionModelConfig", base._FakeExecutionConfig))
    stack.enter_context(patch.object(cycle_module, "PaperExecutionLedger", base._FakeLedger))
    stack.enter_context(patch.object(cycle_module, "JsonlDecisionLedger", base._FakeLedger))
    stack.enter_context(patch.object(cycle_module, "PaperExecutionAdoptionRuntime", base._FakeExecution))
    stack.enter_context(patch.object(cycle_module, "PersistentLiveDecisionLoop", _BlockingLoop))
    stack.enter_context(
        patch.object(
            cycle_module.LiveIntentProvenance,
            "from_registry",
            return_value=object(),
        )
    )
    return stack


def test_runtime_lifecycle_cannot_cross_admitted_paper_decision_phase() -> None:
    """The runtime process lease must cover durable decision effects, not only product tick."""

    _BlockingLoop.entered = Event()
    _BlockingLoop.allow_return = Event()
    base._FakeLoop.instances.clear()
    base._FakeLoop.raise_on_run = None
    base._FakeLedger.created_paths.clear()
    base._FakeExecution.seen_balances.clear()
    base._FakeExecution.seen_book_paths.clear()

    with tempfile.TemporaryDirectory() as directory, _patch_dependencies():
        workspace = Path(directory)
        runtime = _FencedFakeRuntime(workspace)
        cycle = ProductPaperDecisionCycle(
            runtime,
            loop_id="product-paper-loop",
            authority=base._FakeAuthority(),
            intent_factory=base._FakeIntentFactory(),
            scientific_registry=base._FakeRegistry(workspace / "scientific_registry.json"),
            execution_config=base._FakeExecutionConfig(),
            max_quote_age=base.timedelta(seconds=5),
            inputs=(base.ProductDecisionInput("selection-input"),),
        )

        tick_errors: list[BaseException] = []
        lifecycle_errors: list[BaseException] = []
        lifecycle_entered = Event()
        lifecycle_finished = Event()

        def run_cycle() -> None:
            try:
                cycle.tick()
            except BaseException as exc:  # pragma: no cover - asserted below
                tick_errors.append(exc)

        def run_lifecycle_handoff() -> None:
            lifecycle_entered.set()
            try:
                with runtime._operation_fence:
                    runtime._status.state = SessionState.STOPPED
            except BaseException as exc:  # pragma: no cover - asserted below
                lifecycle_errors.append(exc)
            finally:
                lifecycle_finished.set()

        tick_thread = Thread(target=run_cycle, name="paper-decision-cycle")
        lifecycle_thread = Thread(target=run_lifecycle_handoff, name="runtime-lifecycle")
        tick_thread.start()
        assert _BlockingLoop.entered.wait(timeout=2)
        lifecycle_thread.start()
        assert lifecycle_entered.wait(timeout=2)

        try:
            assert not lifecycle_finished.wait(timeout=1), (
                "runtime lifecycle crossed an admitted PAPER decision phase"
            )
        finally:
            _BlockingLoop.allow_return.set()
            tick_thread.join(timeout=2)
            lifecycle_thread.join(timeout=2)

        assert not tick_thread.is_alive()
        assert not lifecycle_thread.is_alive()
        assert tick_errors == []
        assert lifecycle_errors == []
        assert lifecycle_finished.is_set()
