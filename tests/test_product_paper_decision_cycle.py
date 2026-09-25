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
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.product_paper_decision_cycle import (
    ProductDecisionInput,
    ProductPaperDecisionCycle,
    ProductPaperDecisionCycleError,
)
from autosport.continuous_session import SessionState


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
    def __init__(self, workspace: Path, *, initial_bankroll: str = "100") -> None:
        self.workspace = workspace
        self.manifest = SimpleNamespace(initial_bankroll=initial_bankroll)
        self.log: list[str] = []
        self._product_tick = SimpleNamespace(
            source_provider_unavailable=False,
            invalidation_backlog=False,
        )
        self._status = SimpleNamespace(
            state=SessionState.RUNNING,
            source_provider_unavailable=False,
            source_unresolved_gap_delta_ids=(),
            source_state_projection_backlog=False,
            invalidation_full_refresh_required=False,
            invalidation_pending_count=0,
        )

    def tick(self):
        self.log.append("runtime.tick")
        return self._product_tick

    def status(self):
        self.log.append("runtime.status")
        return self._status


class _FakeLedger:
    created_paths: list[Path] = []

    def __init__(self, path: Path) -> None:
        path = Path(path)
        self.path = path
        type(self).created_paths.append(path)


class _FakeExecution:
    seen_balances: list[Decimal] = []
    seen_book_paths: list[Path] = []

    def __init__(self, *, book, ledger, config, max_quote_age, paper_book_path) -> None:
        self.book = book
        self.ledger = ledger
        self.config = config
        self.max_quote_age = max_quote_age
        self.paper_book_path = Path(paper_book_path)
        type(self).seen_balances.append(book.balance)
        type(self).seen_book_paths.append(self.paper_book_path)


class _FakeLoop:
    instances: list["_FakeLoop"] = []
    raise_on_run: BaseException | None = None

    def __init__(self, workspace, **kwargs) -> None:
        self.workspace = Path(workspace)
        self.kwargs = kwargs
        self.registrations: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        type(self).instances.append(self)

    def register_input(self, input_id: str, **selectors) -> None:
        self.registrations.append((input_id, dict(selectors)))

    def run_cycle(self):
        self.kwargs["observation_runner"](object())
        failure = type(self).raise_on_run
        if failure is not None:
            raise failure
        return SimpleNamespace(status="DECIDED")

    def close(self) -> None:
        self.closed = True


class ProductPaperDecisionCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeLedger.created_paths.clear()
        _FakeExecution.seen_balances.clear()
        _FakeExecution.seen_book_paths.clear()
        _FakeLoop.instances.clear()
        _FakeLoop.raise_on_run = None

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
            patch.object(cycle_module, "PersistentLiveDecisionLoop", _FakeLoop)
        )
        stack.enter_context(
            patch.object(
                cycle_module.LiveIntentProvenance,
                "from_registry",
                return_value=SimpleNamespace(strategy_version_id="strategy-v1"),
            )
        )
        return stack

    def _cycle(self, workspace: Path, runtime: _FakeRuntime | None = None):
        runtime = runtime or _FakeRuntime(workspace)
        registry = _FakeRegistry(workspace / "scientific_registry.json")
        return ProductPaperDecisionCycle(
            runtime,
            loop_id="product-paper-loop",
            authority=_FakeAuthority(),
            intent_factory=_FakeIntentFactory(),
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

    def test_tick_runs_product_first_then_one_no_io_decision_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            runtime = _FakeRuntime(workspace)
            cycle = self._cycle(workspace, runtime)

            result = cycle.tick()

            self.assertEqual(runtime.log[0:2], ["runtime.status", "runtime.tick"])
            self.assertIsNotNone(result.decision)
            self.assertIsNone(result.skipped_reason)
            self.assertEqual(len(_FakeLoop.instances), 1)
            loop = _FakeLoop.instances[0]
            self.assertTrue(loop.closed)
            self.assertEqual(loop.workspace, workspace)
            self.assertIs(loop.kwargs["mode"], cycle_module.LiveDecisionMode.PAPER)
            self.assertIs(loop.kwargs["book"], loop.kwargs["paper_execution"].book)
            self.assertIsNone(loop.kwargs["observation_runner"](object()))
            self.assertEqual(
                loop.registrations,
                [
                    (
                        "selection-input",
                        {
                            "source_ids": ("provider-a",),
                            "sports": None,
                            "event_ids": None,
                            "market_ids": None,
                            "selection_ids": ("selection-a",),
                        },
                    )
                ],
            )
            self.assertIn(workspace / "paper-execution.jsonl", _FakeLedger.created_paths)
            self.assertIn(workspace / "decisions.jsonl", _FakeLedger.created_paths)
            self.assertEqual(
                _FakeExecution.seen_book_paths,
                [workspace / "paper_book.json"],
            )
            self.assertTrue((workspace / "paper_book.json").exists())

    def test_provider_unavailable_skips_decision_without_execution_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            runtime = _FakeRuntime(workspace)
            runtime._product_tick = SimpleNamespace(
                source_provider_unavailable=True,
                invalidation_backlog=False,
            )
            cycle = self._cycle(workspace, runtime)

            result = cycle.tick()

            self.assertIsNone(result.decision)
            self.assertEqual(result.skipped_reason, "source_provider_unavailable")
            self.assertEqual(_FakeLoop.instances, [])
            self.assertEqual(_FakeExecution.seen_balances, [])
            self.assertFalse((workspace / "paper_book.json").exists())

    def test_unresolved_source_gap_skips_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            runtime = _FakeRuntime(workspace)
            runtime._status.source_unresolved_gap_delta_ids = ("delta-gap",)
            cycle = self._cycle(workspace, runtime)

            result = cycle.tick()

            self.assertIsNone(result.decision)
            self.assertEqual(result.skipped_reason, "source_gap_unresolved")
            self.assertEqual(_FakeLoop.instances, [])

    def test_each_decision_cycle_reloads_post_settlement_paper_book(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            runtime = _FakeRuntime(workspace)
            cycle = self._cycle(workspace, runtime)

            cycle._run_decision_cycle()
            durable = PaperBook.load(workspace / "paper_book.json")
            durable.open_ticket(
                [TicketLeg("event", "market", "selection", Decimal("2.0"))],
                Decimal("10"),
                reason="post-cycle settlement fixture",
                placed_at="2026-09-22T05:30:00+00:00",
            )
            durable.save(workspace / "paper_book.json")
            cycle._run_decision_cycle()

            self.assertEqual(
                _FakeExecution.seen_balances,
                [Decimal("100"), Decimal("90")],
            )

    def test_strategy_provenance_failure_precedes_book_or_execution_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            cycle = self._cycle(workspace)
            with patch.object(
                cycle_module.LiveIntentProvenance,
                "from_registry",
                side_effect=RuntimeError("strategy provenance unavailable"),
            ):
                with self.assertRaisesRegex(RuntimeError, "strategy provenance unavailable"):
                    cycle._run_decision_cycle()

            self.assertFalse((workspace / "paper_book.json").exists())
            self.assertEqual(_FakeExecution.seen_balances, [])
            self.assertEqual(_FakeLoop.instances, [])

    def test_pending_market_invalidation_skips_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            runtime = _FakeRuntime(workspace)
            runtime._status.invalidation_pending_count = 1
            cycle = self._cycle(workspace, runtime)

            result = cycle.tick()

            self.assertIsNone(result.decision)
            self.assertEqual(result.skipped_reason, "market_invalidation_pending")
            self.assertEqual(_FakeLoop.instances, [])
            self.assertFalse((workspace / "paper_book.json").exists())

    def test_durable_book_initial_bankroll_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            PaperBook("200").save(workspace / "paper_book.json")
            cycle = self._cycle(workspace, _FakeRuntime(workspace, initial_bankroll="100"))

            with self.assertRaisesRegex(
                ProductPaperDecisionCycleError,
                "initial bankroll conflicts",
            ):
                cycle._run_decision_cycle()

            self.assertEqual(_FakeExecution.seen_balances, [])
            self.assertEqual(_FakeLoop.instances, [])

    def test_loop_closes_when_decision_cycle_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            cycle = self._cycle(workspace)
            _FakeLoop.raise_on_run = RuntimeError("boom")

            with self.assertRaisesRegex(RuntimeError, "boom"):
                cycle._run_decision_cycle()

            self.assertEqual(len(_FakeLoop.instances), 1)
            self.assertTrue(_FakeLoop.instances[0].closed)

    def test_concurrent_tick_fails_closed_before_runtime_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            runtime = _FakeRuntime(workspace)
            cycle = self._cycle(workspace, runtime)
            self.assertTrue(cycle._cycle_lock.acquire(blocking=False))
            try:
                with self.assertRaisesRegex(
                    ProductPaperDecisionCycleError,
                    "product cycle in progress",
                ):
                    cycle.tick()
            finally:
                cycle._cycle_lock.release()

            self.assertEqual(runtime.log, [])
            self.assertFalse((workspace / "paper_book.json").exists())

    def test_non_running_product_runtime_fails_before_product_tick(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            runtime = _FakeRuntime(workspace)
            runtime._status.state = SessionState.PAUSED
            cycle = self._cycle(workspace, runtime)

            with self.assertRaisesRegex(
                ProductPaperDecisionCycleError,
                "requires the canonical product runtime to be running",
            ):
                cycle.tick()

            self.assertNotIn("runtime.tick", runtime.log)
            self.assertFalse((workspace / "paper_book.json").exists())

    def test_input_selectors_must_be_sorted_unique_canonical_tuples(self) -> None:
        with self.assertRaisesRegex(ValueError, "sorted and unique"):
            ProductDecisionInput(
                "bad-input",
                source_ids=("provider-b", "provider-a"),
            )
        with self.assertRaisesRegex(ValueError, "sorted and unique"):
            ProductDecisionInput(
                "bad-input",
                source_ids=("provider-a", "provider-a"),
            )
        with self.assertRaisesRegex(ValueError, "non-empty canonical tuple"):
            ProductDecisionInput("bad-input", source_ids=())

    def test_registry_outside_product_workspace_is_rejected_before_cycle(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            tempfile.TemporaryDirectory() as other_directory,
            self._patch_dependencies(),
        ):
            workspace = Path(directory)
            runtime = _FakeRuntime(workspace)
            with self.assertRaisesRegex(
                ProductPaperDecisionCycleError,
                "scientific registry must belong",
            ):
                ProductPaperDecisionCycle(
                    runtime,
                    loop_id="product-paper-loop",
                    authority=_FakeAuthority(),
                    intent_factory=_FakeIntentFactory(),
                    scientific_registry=_FakeRegistry(
                        Path(other_directory) / "scientific_registry.json"
                    ),
                    execution_config=_FakeExecutionConfig(),
                    max_quote_age=timedelta(seconds=5),
                    inputs=(ProductDecisionInput("input-a"),),
                )

    def test_economic_authority_subclass_is_rejected_before_cycle(self) -> None:
        class _AuthoritySubclass(_FakeAuthority):
            pass

        with tempfile.TemporaryDirectory() as directory, self._patch_dependencies():
            workspace = Path(directory)
            with self.assertRaisesRegex(
                TypeError,
                "canonical EconomicDecisionAuthority",
            ):
                ProductPaperDecisionCycle(
                    _FakeRuntime(workspace),
                    loop_id="product-paper-loop",
                    authority=_AuthoritySubclass(),
                    intent_factory=_FakeIntentFactory(),
                    scientific_registry=_FakeRegistry(
                        workspace / "scientific_registry.json"
                    ),
                    execution_config=_FakeExecutionConfig(),
                    max_quote_age=timedelta(seconds=5),
                    inputs=(ProductDecisionInput("input-a"),),
                )

    def test_runtime_lookalike_is_rejected_before_any_other_validation(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "canonical AutonomousProductRuntime",
        ):
            ProductPaperDecisionCycle(
                object(),
                loop_id="loop",
                authority=object(),
                intent_factory=lambda *_args: (),
                scientific_registry=object(),
                execution_config=object(),
                max_quote_age=timedelta(seconds=1),
                inputs=(ProductDecisionInput("input-a"),),
            )


if __name__ == "__main__":
    unittest.main()
