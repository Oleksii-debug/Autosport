import threading
import unittest
from decimal import Decimal
from unittest.mock import patch

import autosport.settlement as settlement_module
from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook
from autosport.settlement import SettlementEngine


class _ObservedSerializationLock:
    """Expose acquisition ordering while retaining real mutual exclusion."""

    def __init__(self) -> None:
        self._inner = threading.Lock()
        self._count_lock = threading.Lock()
        self._attempts = 0
        self.first_acquired = threading.Event()
        self.second_attempted = threading.Event()
        self.release_first = threading.Event()

    def __enter__(self):
        with self._count_lock:
            self._attempts += 1
            attempt = self._attempts
        if attempt == 2:
            self.second_attempted.set()
        self._inner.acquire()
        if attempt == 1:
            self.first_acquired.set()
            if not self.release_first.wait(5):
                self._inner.release()
                raise AssertionError(
                    "timed out waiting to release first settlement record"
                )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._inner.release()


class SettlementRecordConcurrencyTests(unittest.TestCase):
    def test_conflicting_concurrent_records_cannot_both_commit(self) -> None:
        engine = SettlementEngine()
        observed_lock = _ObservedSerializationLock()
        results: dict[str, Exception | None] = {}
        results_lock = threading.Lock()

        def record(name: str, outcome: str) -> None:
            error: Exception | None = None
            try:
                engine.record({"event-1|winner|alice": outcome})
            except Exception as exc:
                error = exc
            with results_lock:
                results[name] = error

        with patch.object(
            settlement_module,
            "_SETTLEMENT_OUTCOME_LOCK",
            observed_lock,
        ):
            first = threading.Thread(target=record, args=("first", "win"))
            first.start()
            self.assertTrue(observed_lock.first_acquired.wait(5))

            second = threading.Thread(target=record, args=("second", "loss"))
            second.start()
            self.assertTrue(observed_lock.second_attempted.wait(5))

            observed_lock.release_first.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertIsNone(results["first"])
        self.assertIsInstance(results["second"], ValueError)
        self.assertEqual(
            str(results["second"]),
            "conflicting settlement for event-1|winner|alice",
        )
        self.assertEqual(engine.outcomes, {"event-1|winner|alice": "win"})


    def test_concurrent_settle_ready_serializes_through_paperbook_commit(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        engine = SettlementEngine({leg.quote_key: "win"})
        observed_lock = _ObservedSerializationLock()
        observed_lock.release_first.set()

        first_settle_entered = threading.Event()
        second_settle_entered = threading.Event()
        release_first_settle = threading.Event()
        settle_count_lock = threading.Lock()
        settle_count = 0
        real_settle = PaperBook.settle
        results: dict[str, list[str] | Exception] = {}
        results_lock = threading.Lock()

        def gated_settle(
            target: PaperBook,
            ticket_id: str,
            winning_quote_keys: set[str],
            void_quote_keys: set[str] | None = None,
            *,
            settled_at: str | None = None,
        ):
            nonlocal settle_count
            with settle_count_lock:
                settle_count += 1
                call = settle_count
            if call == 1:
                first_settle_entered.set()
                if not release_first_settle.wait(5):
                    raise AssertionError("timed out waiting to release first settlement")
            elif call == 2:
                second_settle_entered.set()
            return real_settle(
                target,
                ticket_id,
                winning_quote_keys,
                void_quote_keys,
                settled_at=settled_at,
            )

        def settle(name: str) -> None:
            try:
                value: list[str] | Exception = engine.settle_ready(book)
            except Exception as exc:
                value = exc
            with results_lock:
                results[name] = value

        with (
            patch.object(
                settlement_module,
                "_SETTLEMENT_OUTCOME_LOCK",
                observed_lock,
            ),
            patch.object(PaperBook, "settle", new=gated_settle),
        ):
            first = threading.Thread(target=settle, args=("first",))
            first.start()
            self.assertTrue(first_settle_entered.wait(5))

            second = threading.Thread(target=settle, args=("second",))
            second.start()
            self.assertTrue(observed_lock.second_attempted.wait(5))
            self.assertFalse(second_settle_entered.wait(0.2))

            release_first_settle.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results["first"], [ticket.ticket_id])
        self.assertEqual(results["second"], [])
        self.assertEqual(settle_count, 1)
        self.assertIs(ticket.status, TicketStatus.WON)
        self.assertEqual(ticket.payout, Decimal("20"))
        self.assertEqual(book.balance, Decimal("110"))
        settle_events = [
            event
            for event in book._lifecycle
            if event[0] == "settle" and event[1] == ticket.ticket_id
        ]
        self.assertEqual(len(settle_events), 1)


    def test_same_outcome_concurrent_replay_remains_idempotent(self) -> None:
        engine = SettlementEngine()
        start = threading.Barrier(3)
        errors: list[Exception] = []
        errors_lock = threading.Lock()

        def record() -> None:
            start.wait()
            try:
                engine.record({"event-1|winner|alice": "void"})
            except Exception as exc:
                with errors_lock:
                    errors.append(exc)

        workers = [threading.Thread(target=record) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join(5)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(engine.outcomes, {"event-1|winner|alice": "void"})


if __name__ == "__main__":
    unittest.main()
