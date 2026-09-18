from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.decision_ledger import (
    DecisionLedgerIntegrityError,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
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
from autosport.providers import ProviderUnavailableError
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
    INTENT_CONTEXT_SHA256 = "11" * 32
    ALT_INTENT_CONTEXT_SHA256 = "22" * 32

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
        intent_context_sha256: str | None = None,
        book: PaperBook | None = None,
        authority: EconomicDecisionAuthority | None = None,
    ) -> PersistentLiveDecisionLoop:
        return PersistentLiveDecisionLoop(
            workspace,
            loop_id="live-test-loop",
            mode=LiveDecisionMode.PAPER,
            book=PaperBook("1000") if book is None else book,
            authority=self._authority() if authority is None else authority,
            intent_factory=factory,
            intent_context_sha256=(
                self.INTENT_CONTEXT_SHA256
                if intent_context_sha256 is None
                else intent_context_sha256
            ),
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

    def test_decision_timestamp_follows_observation_receipt_clock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            durable = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            selection="selection-a",
                            sequence=1,
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )

            def observer(updates):
                result = durable(updates)
                clock.value = self.START + timedelta(seconds=3)
                return result

            factory = _EmptyIntentFactory()
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            result = loop.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                factory.calls,
                [("input-a", (("selection-a", 1, "open"),))],
            )
            record = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()[0]
            self.assertEqual(
                record.observed_ts,
                (self.START + timedelta(seconds=3)).isoformat(),
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
            self.assertEqual(resumed.dependencies.input_ids, ("input-a",))

            with patch.object(
                JsonlDecisionLedger,
                "verified_records",
                side_effect=AssertionError("full Decision Ledger scan is forbidden"),
            ):
                result = resumed.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(result.decision_id, first_id)
            self.assertEqual(len(ledger.verified_records()), 1)
            self.assertEqual([item[0] for item in resumed_factory.calls], ["input-a"])

    def test_append_pending_restart_recovers_before_polling_new_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            crashes = {"remaining": 1}

            def crash_after_append() -> None:
                if crashes["remaining"]:
                    crashes["remaining"] -= 1
                    raise RuntimeError("simulated process loss after ledger append")

            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=clock,
                post_append_hook=crash_after_append,
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()

            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            first_id = ledger.verified_records()[0].decision_id
            resumed_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            selection="selection-a",
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )

            recovered = resumed.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(recovered.decision_id, first_id)
            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(len(ledger.verified_records()), 1)

            advanced = resumed.run_cycle()
            self.assertEqual(advanced.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 1)
            self.assertEqual(len(ledger.verified_records()), 2)

    def test_pending_restart_recovers_before_polling_new_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first_observer = _DurableObserver(
                workspace,
                [(self._event(selection="selection-a", sequence=1),)],
            )

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            first = self._loop(
                workspace,
                observer=first_observer,
                factory=fail_after_pending,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()
            self.assertFalse((workspace / "decisions.jsonl").exists())

            resumed_observer = _DurableObserver(
                workspace,
                [
                    (
                        self._event(
                            selection="selection-a",
                            sequence=2,
                            odds="2.10",
                            observed=self.START + timedelta(seconds=2),
                        ),
                    )
                ],
            )
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )

            recovered = resumed.run_cycle()

            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 0)
            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            self.assertEqual(len(ledger.verified_records()), 1)

            advanced = resumed.run_cycle()
            self.assertEqual(advanced.status, LiveCycleStatus.DECIDED)
            self.assertEqual(resumed_observer.calls, 1)
            self.assertEqual(len(ledger.verified_records()), 2)

    def test_pending_restart_rejects_replayed_market_state_mismatch_before_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=fail_after_pending,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()

            store = SQLiteMarketStore(workspace / "market.db")
            try:
                store.append(
                    self._event(
                        selection="selection-a",
                        sequence=2,
                        odds="2.10",
                        observed=self.START + timedelta(milliseconds=500),
                    )
                )
            finally:
                store.close()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=2)),
            )

            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "replayed market state changed",
            ):
                resumed.run_cycle()

            self.assertEqual(resumed_observer.calls, 0)
            self.assertEqual(resumed_factory.calls, [])
            self.assertFalse((workspace / "decisions.jsonl").exists())

    def test_pending_restart_rejects_changed_intent_context_before_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=fail_after_pending,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
                intent_context_sha256=self.ALT_INTENT_CONTEXT_SHA256,
            )
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "runtime context changed",
            ):
                resumed.run_cycle()
            self.assertEqual(resumed_observer.calls, 0)
            self.assertFalse((workspace / "decisions.jsonl").exists())

    def test_pending_restart_rejects_changed_paper_book_before_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            def fail_after_pending(input_id, snapshot):
                raise RuntimeError("simulated process loss after pending cursor")

            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=fail_after_pending,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
                first.run_cycle()

            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=2)),
                book=PaperBook("900"),
            )
            with self.assertRaisesRegex(
                LiveDecisionProgressError,
                "runtime context changed",
            ):
                resumed.run_cycle()
            self.assertEqual(resumed_observer.calls, 0)
            self.assertFalse((workspace / "decisions.jsonl").exists())

    def test_multi_input_crash_rebuilds_all_but_preserves_affected_evidence(self) -> None:
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
            append_count = {"value": 0}

            def crash_on_second_append() -> None:
                append_count["value"] += 1
                if append_count["value"] == 2:
                    raise RuntimeError("simulated second-cycle process loss")

            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
                post_append_hook=crash_on_second_append,
            )
            self._register_two(loop)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            factory.calls.clear()
            clock.value = self.START + timedelta(seconds=3)
            with self.assertRaisesRegex(RuntimeError, "second-cycle process loss"):
                loop.run_cycle()

            records = JsonlDecisionLedger(
                workspace / "decisions.jsonl"
            ).verified_records()
            self.assertEqual(len(records), 2)
            self.assertEqual(records[-1].payload["affected_input_ids"], ("input-a",))

            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=4)),
            )
            self.assertEqual(
                resumed.dependencies.input_ids,
                ("input-a", "input-b"),
            )
            with patch.object(
                JsonlDecisionLedger,
                "verified_records",
                side_effect=AssertionError("full Decision Ledger scan is forbidden"),
            ):
                result = resumed.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.DUPLICATE_DECISION)
            self.assertEqual(result.affected_input_ids, ("input-a",))
            self.assertEqual(
                [item[0] for item in resumed_factory.calls],
                ["input-a", "input-b"],
            )
            self.assertEqual(
                len(
                    JsonlDecisionLedger(
                        workspace / "decisions.jsonl"
                    ).verified_records()
                ),
                2,
            )

    def test_clean_restart_rebuilds_cache_without_duplicate_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first_factory = _EmptyIntentFactory()
            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=first_factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(first.run_cycle().status, LiveCycleStatus.DECIDED)

            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            first_id = ledger.verified_records()[0].decision_id
            resumed_factory = _EmptyIntentFactory()
            resumed_observer = _DurableObserver(workspace, [()])
            resumed = self._loop(
                workspace,
                observer=resumed_observer,
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )
            self.assertEqual(resumed.dependencies.input_ids, ("input-a",))

            result = resumed.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(len(ledger.verified_records()), 1)
            self.assertEqual(ledger.verified_records()[0].decision_id, first_id)
            self.assertEqual([item[0] for item in resumed_factory.calls], ["input-a"])

    def test_clean_restart_ignores_unrelated_persisted_market_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            first.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(first.run_cycle().status, LiveCycleStatus.DECIDED)

            store = SQLiteMarketStore(workspace / "market.db")
            try:
                store.append(
                    self._event(
                        selection="selection-b",
                        sequence=1,
                        odds="3.00",
                        observed=self.START + timedelta(seconds=2),
                    )
                )
            finally:
                store.close()

            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            first_id = ledger.verified_records()[0].decision_id
            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=3)),
            )

            result = resumed.run_cycle()

            self.assertEqual(result.status, LiveCycleStatus.NO_CHANGE)
            self.assertEqual(len(ledger.verified_records()), 1)
            self.assertEqual(ledger.verified_records()[0].decision_id, first_id)
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

    def test_quote_age_cannot_exceed_owner_economic_goal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            goal = EconomicGoalContract(
                goal_id="goal-live-age",
                revision=1,
                bankroll_id="bankroll-live-age",
                currency="EUR",
            )
            authority = EconomicDecisionAuthority(
                goal,
                PaperRiskPolicy(economic_goal=goal),
            )
            with self.assertRaisesRegex(ValueError, "cannot exceed EconomicGoalContract"):
                PersistentLiveDecisionLoop(
                    workspace,
                    loop_id="live-age-test",
                    mode=LiveDecisionMode.PAPER,
                    book=PaperBook("1000"),
                    authority=authority,
                    intent_factory=_EmptyIntentFactory(),
                    intent_context_sha256=self.INTENT_CONTEXT_SHA256,
                    observation_runner=_DurableObserver(workspace, [()]),
                    max_quote_age=timedelta(seconds=6),
                    clock=_ManualClock(self.START),
                )

    def test_provider_gap_persists_zero_and_forces_rebuild_on_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            observer = _DurableObserver(
                workspace,
                [
                    ProviderUnavailableError("provider unavailable"),
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
            self.assertIn("ProviderUnavailableError", gap.detail)
            records = JsonlDecisionLedger(workspace / "decisions.jsonl").verified_records()
            self.assertEqual(records[-1].payload["gate"], "provider_gap")

            clock.value = self.START + timedelta(seconds=2)
            recovered = loop.run_cycle()
            self.assertEqual(recovered.status, LiveCycleStatus.DECIDED)
            self.assertEqual([item[0] for item in factory.calls], ["input-a"])

    def test_local_observation_failure_propagates_without_provider_gap_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [RuntimeError("local SQLite/integrity failure")],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")

            with self.assertRaisesRegex(RuntimeError, "SQLite/integrity failure"):
                loop.run_cycle()

            self.assertFalse((workspace / "decisions.jsonl").exists())
            self.assertFalse(
                (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).exists()
            )

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

    def test_pause_and_stop_are_durable_and_do_not_poll_provider(self) -> None:
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

            paused_observer = _DurableObserver(workspace, [()])
            paused_restart = self._loop(
                workspace,
                observer=paused_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            self.assertEqual(paused_restart.dependencies.input_ids, ("input-a",))
            self.assertEqual(
                paused_restart.run_cycle().status,
                LiveCycleStatus.PAUSED,
            )
            self.assertEqual(paused_observer.calls, 0)

            paused_restart.resume()
            paused_restart.stop()
            self.assertEqual(
                paused_restart.run_cycle().status,
                LiveCycleStatus.STOPPED,
            )
            stopped_observer = _DurableObserver(workspace, [()])
            stopped_restart = self._loop(
                workspace,
                observer=stopped_observer,
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            self.assertEqual(stopped_restart.dependencies.input_ids, ("input-a",))
            self.assertEqual(
                stopped_restart.run_cycle().status,
                LiveCycleStatus.STOPPED,
            )
            self.assertEqual(stopped_observer.calls, 0)
            with self.assertRaises(RuntimeError):
                stopped_restart.resume()

    def test_durable_dependency_registry_restores_exact_selectors_without_manual_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = self._loop(
                workspace,
                observer=_DurableObserver(workspace, [()]),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START),
            )
            first.register_input(
                "exact-input",
                source_ids="provider-a",
                event_ids="event-1",
                market_ids="market-1",
                selection_ids="selection-a",
            )

            resumed_factory = _EmptyIntentFactory()
            resumed = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(selection="selection-a", sequence=1),)],
                ),
                factory=resumed_factory,
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )

            self.assertEqual(resumed.dependencies.input_ids, ("exact-input",))
            result = resumed.run_cycle()
            self.assertEqual(result.status, LiveCycleStatus.DECIDED)
            self.assertEqual(
                resumed_factory.calls,
                [("exact-input", (("selection-a", 1, "open"),))],
            )

    def test_hot_path_does_not_scan_complete_historical_decision_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            batches = []
            for sequence in range(1, 17):
                batches.append(
                    (
                        self._event(
                            sequence=sequence,
                            odds=f"2.{sequence:02d}",
                            observed=self.START + timedelta(milliseconds=sequence),
                        ),
                    )
                )
            observer = _DurableObserver(workspace, batches)
            factory = _EmptyIntentFactory()
            clock = _ManualClock(self.START + timedelta(seconds=1))
            loop = self._loop(
                workspace,
                observer=observer,
                factory=factory,
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")

            for index in range(15):
                clock.value = self.START + timedelta(
                    seconds=1,
                    milliseconds=index,
                )
                self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            before_size = (workspace / "decisions.jsonl").stat().st_size
            clock.value = self.START + timedelta(seconds=2)
            with patch.object(
                JsonlDecisionLedger,
                "verified_records",
                side_effect=AssertionError("full Decision Ledger scan is forbidden"),
            ):
                final = loop.run_cycle()

            self.assertEqual(final.status, LiveCycleStatus.DECIDED)
            self.assertGreater((workspace / "decisions.jsonl").stat().st_size, before_size)

    def test_restart_verifies_complete_historical_decision_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [
                        (self._event(sequence=1),),
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=2),
                            ),
                        ),
                    ],
                ),
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            clock.value = self.START + timedelta(seconds=3)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            ledger_path = workspace / "decisions.jsonl"
            lines = ledger_path.read_bytes().splitlines(keepends=True)
            self.assertEqual(len(lines), 2)
            ledger_path.write_bytes(b"".join(lines + [lines[0]]))

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "duplicate decision_id",
            ):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=4)),
                )

    def test_truncated_committed_decision_fails_closed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            clock = _ManualClock(self.START + timedelta(seconds=1))
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [
                        (self._event(sequence=1),),
                        (
                            self._event(
                                sequence=2,
                                odds="2.10",
                                observed=self.START + timedelta(seconds=2),
                            ),
                        ),
                    ],
                ),
                factory=_EmptyIntentFactory(),
                clock=clock,
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            clock.value = self.START + timedelta(seconds=3)
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)

            ledger_path = workspace / "decisions.jsonl"
            lines = ledger_path.read_bytes().splitlines(keepends=True)
            self.assertEqual(len(lines), 2)
            ledger_path.write_bytes(lines[0])

            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "committed live decision is missing",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=4)),
                )
            self.assertEqual(resumed_observer.calls, 0)

    def test_missing_decision_ledger_with_durable_progress_fails_closed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            loop = self._loop(
                workspace,
                observer=_DurableObserver(
                    workspace,
                    [(self._event(sequence=1),)],
                ),
                factory=_EmptyIntentFactory(),
                clock=_ManualClock(self.START + timedelta(seconds=1)),
            )
            loop.register_input("input-a", selection_ids="selection-a")
            self.assertEqual(loop.run_cycle().status, LiveCycleStatus.DECIDED)
            self.assertTrue(
                (workspace / PersistentLiveDecisionLoop.PROGRESS_FILE_NAME).exists()
            )
            (workspace / "decisions.jsonl").unlink()

            resumed_observer = _DurableObserver(workspace, [()])
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "missing or unreadable",
            ):
                self._loop(
                    workspace,
                    observer=resumed_observer,
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START + timedelta(seconds=2)),
                )
            self.assertEqual(resumed_observer.calls, 0)

    def test_corrupted_dependency_registry_fails_closed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / PersistentLiveDecisionLoop.INPUTS_FILE_NAME).write_text(
                '{"schema":"autosport.live_decision_inputs","schema":"duplicate"}\n',
                encoding="utf-8",
            )

            with self.assertRaises(LiveDecisionProgressError):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START),
                )

    def test_corrupted_control_fails_closed_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / PersistentLiveDecisionLoop.CONTROL_FILE_NAME).write_text(
                '{"schema":"autosport.live_decision_control","schema":"duplicate"}\n',
                encoding="utf-8",
            )

            with self.assertRaises(LiveDecisionProgressError):
                self._loop(
                    workspace,
                    observer=_DurableObserver(workspace, [()]),
                    factory=_EmptyIntentFactory(),
                    clock=_ManualClock(self.START),
                )

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
