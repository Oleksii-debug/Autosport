from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.decision_ledger import EconomicDecisionAuthority, JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.economic_goal import EconomicGoalContract
from autosport.live_decision_loop import (
    LiveCycleStatus,
    LiveDecisionMode,
    LiveDecisionProgressError,
    LiveLoopBounds,
    PersistentLiveDecisionLoop,
)
from autosport.market_bus import MarketEventBus
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from autosport.storage import SQLiteMarketStore


class _ManualClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _DurableObserver:
    def __init__(
        self,
        workspace: Path,
        batches: list[tuple[MarketEvent, ...] | Exception],
    ) -> None:
        self.workspace = workspace
        self.batches = list(batches)
        self.calls = 0

    def __call__(self, updates) -> object:
        self.calls += 1
        item: tuple[MarketEvent, ...] | Exception
        if self.batches:
            item = self.batches.pop(0)
        else:
            item = ()
        if isinstance(item, Exception):
            raise item

        store = SQLiteMarketStore(self.workspace / "market.db")
        try:
            bus = MarketEventBus(store)
            bus.subscribe(updates.accept_persisted)
            bus.publish_many(item)
        finally:
            store.close()
        return object()


class _EmptyIntentFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[tuple[str, int, str], ...]]] = []

    def __call__(self, input_id, snapshot):
        self.calls.append(
            (
                input_id,
                tuple(
                    (event.selection_id, event.sequence, event.status)
                    for event in snapshot.events
                ),
            )
        )
        return ()


class PersistentLiveDecisionLoopTests(unittest.TestCase):
    START = datetime(2026, 9, 18, 18, 0, 0, tzinfo=timezone.utc)

    @staticmethod
    def _event(
        *,
        selection: str = "selection-a",
        sequence: int = 1,
        odds: str = "2.00",
        status: str = "open",
        observed: datetime | None = None,
    ) -> MarketEvent:
        timestamp = (observed or PersistentLiveDecisionLoopTests.START).isoformat()
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id=selection,
            decimal_odds=Decimal(odds),
            observed_ts=timestamp,
            source_id="provider-a",
            sequence=sequence,
            status=status,
            source_ts=timestamp,
            ingest_ts=timestamp,
        )

    @staticmethod
    def _authority() -> EconomicDecisionAuthority:
        goal = EconomicGoalContract(
            goal_id="goal-live-test",
            revision=1,
            bankroll_id="bankroll-live-test",
            currency="EUR",
        )
        return EconomicDecisionAuthority(
            goal,
            PaperRiskPolicy(economic_goal=goal),
        )

    def _loop(
        self,
        workspace: Path,
        *,
        observer: _DurableObserver,
        factory: _EmptyIntentFactory,
        clock: _ManualClock,
        bounds: LiveLoopBounds | None = None,
        post_append_hook=None,
    ) -> PersistentLiveDecisionLoop:
        return PersistentLiveDecisionLoop(
            workspace,
            loop_id="live-test-loop",
            mode=LiveDecisionMode.PAPER,
            book=PaperBook("1000"),
            authority=self._authority(),
            intent_factory=factory,
            observation_runner=observer,
            bounds=bounds,
            max_quote_age=timedelta(seconds=5),
            clock=clock,
            post_append_hook=post_append_hook,
        )

    @staticmethod
    def _register_two(loop: PersistentLiveDecisionLoop) -> None:
        loop.register_input(
            "input-a",
            source_ids="provider-a",
            event_ids="event-1",
            market_ids="market-1",
            selection_ids="selection-a",
        )
        loop.register_input(
            "input-b",
            source_ids="provider-a",
            event_ids="event-1",
            market_ids="market-1",
            selection_ids="selection-b",
        )

    def test_first_cycle_rebuilds_all_then_only_affected_input_recomputes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(selection="selection-a", sequence=1),
                        self._event(selection="selection-b", sequence=1),
                    ),
                    (
                        self._event(
                            selection="selection-a",
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    ),
                ],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            self._register_two(loop)

            first = loop.run_cycle()
            self.assertEqual(first.status, LiveCycleStatus.DECIDED)
            self.assertEqual(first.plan.action.value, "zero")
            self.assertEqual([item[0] for item in factory.calls], ["input-a", "input-b"])

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=3)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual([item[0] for item in factory.calls], ["input-a"])
            self.assertEqual(second.affected_input_ids, ("input-a",))
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                2,
            )

    def test_duplicate_redelivery_does_not_emit_second_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            duplicate = self._event(selection="selection-a", sequence=1)
            observer = _DurableObserver(workspace, [(duplicate,), (duplicate,)])
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()

            self.assertEqual(second.status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(factory.calls, [])
            self.assertEqual(
                len(JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()),
                1,
            )

    def test_crash_after_ledger_append_recovers_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [(self._event(selection="selection-a", sequence=1),)],
            )
            factory = _EmptyIntentFactory()
            crashes = {"remaining": 1}

            def crash_after_append() -> None:
                if crashes["remaining"]:
                    crashes["remaining"] -= 1
                    raise RuntimeError("simulated process loss after ledger append")

            first_loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
                post_append_hook=crash_after_append,
            )
            first_loop.register_input("input-a", selection_ids="selection-a")

            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first_loop.run_cycle()

            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            first_records = ledger.verified_records()
            self.assertEqual(len(first_records), 1)
            first_id = first_records[0].decision_id

            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=4)),
            )
            resumed.register_input("input-a", selection_ids="selection-a")

            result = resumed.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(result.decision_id, first_id)
            self.assertEqual(len(ledger.verified_records()), 1)
            self.assertEqual([item[0] for item in resumed_factory.calls], ["input-a"])

    def test_backpressure_never_emits_from_partial_dirty_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(selection="selection-a", sequence=1),
                        self._event(selection="selection-b", sequence=1),
                    ),
                    (),
                ],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
                bounds=LiveLoopBounds(max_dirty_per_cycle=1),
            )
            self._register_two(loop)

            first = loop.run_cycle()
            self.assertEqual(first.status, LiveCycleStatus.BACKPRESSURE)
            self.assertFalse((workspace / "decisions.jsonl").exists())

            clock.value = self.START + timedelta(seconds=2)
            second = loop.run_cycle()
            self.assertEqual(second.status, LiveCycleStatus.DECIDED)
            self.assertEqual(set(second.affected_input_ids), {"input-a", "input-b"})
            self.assertEqual([item[0] for item in factory.calls], ["input-a", "input-b"])

    def test_provider_gap_persists_zero_and_forces_rebuild_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    RuntimeError("provider unavailable"),
                    (self._event(selection="selection-a", sequence=1),),
                ],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            gap = loop.run_cycle()
            self.assertEqual(gap.status, LiveCycleStatus.PROVIDER_GAP)
            self.assertEqual(gap.plan.action.value, "zero")
            self.assertIn("RuntimeError", gap.detail)
            records = JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()
            self.assertEqual(records[-1].payload["gate"], "provider_gap")

            clock.value = self.START + timedelta(seconds=2)
            recovered = loop.run_cycle()
            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual([item[0] for item in factory.calls], ["input-a"])

    def test_suspended_quote_recomputes_input_with_empty_active_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    (self._event(sequence=1),),
                    (
                        self._event(
                            sequence=2,
                            status="suspended",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    ),
                ],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            loop.run_cycle()
            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=3)
            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", ())])

    def test_freshness_expiry_recomputes_without_market_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [(self._event(sequence=1),), ()],
            )
            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            loop.run_cycle()
            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=7)
            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(factory.calls, [("input-a", ())])

    def test_pause_and_stop_do_not_poll_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            observer = _DurableObserver(workspace, [()])
            loop = self._loop(
                workspace,
                observer=observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            loop.pause()
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.PAUSED)
            self.assertEqual(observer.calls, 0)

            loop.resume()
            loop.stop()
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.STOPPED)
            self.assertEqual(observer.calls, 0)
            with self.assertRaises(RuntimeError):
                loop.resume()

    def test_corrupted_progress_fails_closed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).write_text(
                '{"schema":"autosport.live_decision_progress","schema":"duplicate"}\n',
                encoding="utf-8",
            )

            with self.assertRaises(LiveDecisionProgressError):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START),
                )


if __name__ == "__main__":
    unittest.main()
