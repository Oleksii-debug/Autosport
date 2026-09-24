import threading
import unittest
from decimal import Decimal
from unittest.mock import patch

import autosport.settlement as settlement_module
from autosport.domain import TicketLeg, TicketStatus
from autosport.paper import PaperBook
from autosport.settlement import SettlementEngine


class SettlementRecordConcurrencyTests(unittest.TestCase):
    def test_module_lock_rebind_cannot_split_record_from_settlement_commit(
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
        settle_entered = threading.Event()
        release_settle = threading.Event()
        record_started = threading.Event()
        record_done = threading.Event()
        real_settle = PaperBook.settle
        results: dict[str, object] = {}
        results_lock = threading.Lock()

        def gated_settle(
            target: PaperBook,
            ticket_id: str,
            winning_quote_keys: set[str],
            void_quote_keys: set[str] | None = None,
            *,
            settled_at: str | None = None,
        ):
            settle_entered.set()
            if not release_settle.wait(5):
                raise AssertionError(
                    "timed out waiting to release settlement commit"
                )
            return real_settle(
                target,
                ticket_id,
                winning_quote_keys,
                void_quote_keys,
                settled_at=settled_at,
            )

        def settle() -> None:
            try:
                value: object = engine.settle_ready(book)
            except BaseException as exc:
                value = exc
            with results_lock:
                results["settle"] = value

        def record() -> None:
            record_started.set()
            try:
                value: object = engine.record(
                    {leg.quote_key: "loss"}
                )
            except BaseException as exc:
                value = exc
            with results_lock:
                results["record"] = value
            record_done.set()

        with patch.object(PaperBook, "settle", new=gated_settle):
            first = threading.Thread(target=settle)
            first.start()
            self.assertTrue(settle_entered.wait(5))

            with patch.object(
                settlement_module,
                "_SETTLEMENT_OUTCOME_LOCK",
                threading.Lock(),
                create=True,
            ):
                second = threading.Thread(target=record)
                second.start()
                self.assertTrue(record_started.wait(5))
                self.assertFalse(record_done.wait(0.2))

                release_settle.set()
                first.join(5)
                second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results["settle"], [ticket.ticket_id])
        self.assertIsInstance(results["record"], ValueError)
        self.assertEqual(
            str(results["record"]),
            f"conflicting settlement for {leg.quote_key}",
        )
        self.assertEqual(engine.outcomes, {leg.quote_key: "win"})
        self.assertIs(ticket.status, TicketStatus.WON)
        self.assertEqual(book.balance, Decimal("110"))


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

        first_settle_entered = threading.Event()
        second_settle_entered = threading.Event()
        second_started = threading.Event()
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
                    raise AssertionError(
                        "timed out waiting to release first settlement"
                    )
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
            if name == "second":
                second_started.set()
            try:
                value: list[str] | Exception = engine.settle_ready(book)
            except Exception as exc:
                value = exc
            with results_lock:
                results[name] = value

        with patch.object(PaperBook, "settle", new=gated_settle):
            first = threading.Thread(target=settle, args=("first",))
            first.start()
            self.assertTrue(first_settle_entered.wait(5))

            second = threading.Thread(target=settle, args=("second",))
            second.start()
            self.assertTrue(second_started.wait(5))
            self.assertFalse(second_settle_entered.wait(0.2))
            self.assertTrue(second.is_alive())

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
