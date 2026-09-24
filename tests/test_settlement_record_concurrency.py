import threading
import unittest
from decimal import Decimal
from unittest.mock import patch

import autosport.settlement as settlement_module
from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook
from autosport.settlement import SettlementEngine


def _closure_value(function, name: str):
    cells = dict(
        zip(
            function.__code__.co_freevars,
            function.__closure__ or (),
            strict=True,
        )
    )
    return cells[name].cell_contents


class SettlementRecordConcurrencyTests(unittest.TestCase):
    def test_module_lock_rebind_cannot_split_record_from_settlement_commit(
        self,
    ) -> None:
        engine = SettlementEngine()
        serialization_lock = _closure_value(
            SettlementEngine.record,
            "serialization_lock",
        )
        self.assertIs(
            serialization_lock,
            _closure_value(
                SettlementEngine.settle_ready,
                "serialization_lock",
            ),
        )

        started = threading.Event()
        done = threading.Event()
        result: list[BaseException] = []

        def record() -> None:
            started.set()
            try:
                engine.record({"event-1|winner|alice": "win"})
            except BaseException as exc:
                result.append(exc)
            finally:
                done.set()

        serialization_lock.acquire()
        try:
            with patch.object(
                settlement_module,
                "_SETTLEMENT_OUTCOME_LOCK",
                threading.Lock(),
                create=True,
            ):
                worker = threading.Thread(target=record)
                worker.start()
                self.assertTrue(started.wait(5))
                self.assertFalse(done.wait(0.2))
        finally:
            serialization_lock.release()

        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [])
        self.assertEqual(
            engine.outcomes,
            {"event-1|winner|alice": "win"},
        )

    def test_concurrent_settle_ready_serializes_through_paperbook_commit(
        self,
    ) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        engine = SettlementEngine({leg.quote_key: "win"})
        serialization_lock = _closure_value(
            SettlementEngine.settle_ready,
            "serialization_lock",
        )
        started = [threading.Event(), threading.Event()]
        results: list[list[str] | BaseException | None] = [None, None]

        def settle(index: int) -> None:
            started[index].set()
            try:
                results[index] = engine.settle_ready(book)
            except BaseException as exc:
                results[index] = exc

        serialization_lock.acquire()
        try:
            workers = [
                threading.Thread(target=settle, args=(index,))
                for index in range(2)
            ]
            for worker in workers:
                worker.start()
            self.assertTrue(started[0].wait(5))
            self.assertTrue(started[1].wait(5))
            self.assertTrue(all(worker.is_alive() for worker in workers))
        finally:
            serialization_lock.release()

        for worker in workers:
            worker.join(5)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertIn([ticket.ticket_id], results)
        self.assertIn([], results)
        self.assertFalse(
            any(isinstance(value, BaseException) for value in results)
        )
        self.assertIs(ticket.status, TicketStatus.WON)
        self.assertEqual(ticket.payout, Decimal("20"))
        self.assertEqual(book.balance, Decimal("110"))
        settle_events = [
            event
            for event in book._lifecycle
            if event[0] == "settle" and event[1] == ticket.ticket_id
        ]
        self.assertEqual(len(settle_events), 1)

    def test_paperbook_class_settle_rebind_fails_closed_before_positive_result(
        self,
    ) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        engine = SettlementEngine({leg.quote_key: "win"})

        def no_op_settle(
            _book,
            _ticket_id,
            _winning_quote_keys,
            _void_quote_keys=None,
            *,
            settled_at=None,
        ):
            return None

        with patch.object(PaperBook, "settle", new=no_op_settle):
            with self.assertRaisesRegex(
                ValueError,
                "PaperBook settlement authority dispatch changed",
            ):
                engine.settle_ready(book)

        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("90"))
        self.assertEqual(
            [event[0] for event in book._lifecycle],
            ["open"],
        )

        self.assertEqual(engine.settle_ready(book), [ticket.ticket_id])
        self.assertIs(ticket.status, TicketStatus.WON)
        self.assertEqual(book.balance, Decimal("110"))

    def test_constructor_snapshots_caller_owned_outcomes_dict(self) -> None:
        caller_owned = {"event-1|winner|alice": "win"}
        engine = SettlementEngine(caller_owned)

        caller_owned["event-1|winner|alice"] = "loss"
        caller_owned["event-2|winner|bob"] = "void"

        self.assertEqual(
            engine.outcomes,
            {"event-1|winner|alice": "win"},
        )

        engine.record({"event-3|winner|carol": "void"})
        self.assertNotIn("event-3|winner|carol", caller_owned)


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
