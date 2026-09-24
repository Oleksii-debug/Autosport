import threading
import unittest
from decimal import Decimal
from unittest.mock import patch

import autosport.domain as domain_module
import autosport.paper as paper_module
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

    def test_paperbook_module_ticket_status_rebind_fails_closed(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        engine = SettlementEngine({leg.quote_key: "win"})
        canonical_ticket_status = paper_module.TicketStatus

        class ForgedTicketStatus:
            OPEN = canonical_ticket_status.OPEN
            WON = canonical_ticket_status.LOST
            LOST = canonical_ticket_status.LOST
            VOID = canonical_ticket_status.VOID

        with patch.object(paper_module, "TicketStatus", ForgedTicketStatus):
            with self.assertRaisesRegex(
                ValueError,
                "PaperBook settlement authority globals changed",
            ):
                engine.settle_ready(book)

        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("90"))
        self.assertEqual(engine.settle_ready(book), [ticket.ticket_id])
        self.assertIs(ticket.status, TicketStatus.WON)
        self.assertEqual(ticket.payout, Decimal("20"))
        self.assertEqual(book.balance, Decimal("110"))

    def test_ticket_leg_quote_key_descriptor_rebind_fails_closed(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        forged_quote_key = "forged-event|winner|mallory"
        engine = SettlementEngine({forged_quote_key: "win"})

        with patch.object(
            TicketLeg,
            "quote_key",
            new=property(lambda _leg: forged_quote_key),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "settlement domain DTO authority dispatch changed",
            ):
                engine.settle_ready(book)

        ticket = next(iter(book.tickets.values()))
        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("90"))
        self.assertEqual(engine.settle_ready(book), [])

    def test_ticket_leg_locked_odds_descriptor_rebind_fails_closed(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        engine = SettlementEngine({leg.quote_key: "win"})

        with patch.object(
            TicketLeg,
            "locked_odds",
            new=property(lambda _leg: Decimal("99")),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "settlement domain DTO authority dispatch changed",
            ):
                engine.settle_ready(book)

        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("90"))
        self.assertEqual(engine.settle_ready(book), [ticket.ticket_id])
        self.assertEqual(ticket.payout, Decimal("20"))
        self.assertEqual(book.balance, Decimal("110"))

    def test_ticket_leg_quote_identity_global_rebind_fails_closed(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        forged_quote_key = "forged-event|winner|mallory"
        engine = SettlementEngine({forged_quote_key: "win"})

        with patch.object(
            domain_module,
            "_quote_identity",
            new=lambda *_args, **_kwargs: forged_quote_key,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "settlement quote identity authority globals changed",
            ):
                engine.settle_ready(book)

        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("90"))
        self.assertEqual(engine.settle_ready(book), [])

    def test_paperbook_module_decimal_context_helper_rebind_fails_closed(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        engine = SettlementEngine({leg.quote_key: "win"})
        canonical_context_factory = paper_module._paper_decimal_context

        with patch.object(
            paper_module,
            "_paper_decimal_context",
            lambda: canonical_context_factory(),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "PaperBook settlement authority globals changed",
            ):
                engine.settle_ready(book)

        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("90"))

    def test_constructor_snapshots_caller_owned_outcomes_dict(self) -> None:
        caller_owned = {"event-1|winner|alice": "win"}
        engine = SettlementEngine(caller_owned)

        caller_owned["event-1|winner|alice"] = "loss"
        caller_owned["event-2|winner|bob"] = "void"

        self.assertEqual(
            engine.outcomes,
            {"event-1|winner|alice": "win"},
        )

        before_record = engine.outcomes
        engine.record({"event-3|winner|carol": "void"})
        self.assertNotIn("event-3|winner|carol", caller_owned)
        self.assertIsNot(engine.outcomes, before_record)
        before_record["event-4|winner|dave"] = "loss"
        self.assertNotIn("event-4|winner|dave", engine.outcomes)

    def test_malformed_constructor_keeps_delayed_ingress_validation(self) -> None:
        engine = SettlementEngine(["not-a-dict"])  # type: ignore[arg-type]

        with self.assertRaisesRegex(
            ValueError,
            "settlement outcomes must be an exact dict",
        ):
            engine.record({})
        with self.assertRaisesRegex(
            ValueError,
            "settlement outcomes must be an exact dict",
        ):
            engine.settle_ready(PaperBook("100"))

    def test_direct_outcome_overwrite_cannot_bypass_record_conflict(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        engine = SettlementEngine({leg.quote_key: "win"})

        engine.outcomes[leg.quote_key] = "loss"

        with self.assertRaisesRegex(
            ValueError,
            "settlement outcome authority changed",
        ):
            engine.record({leg.quote_key: "loss"})
        with self.assertRaisesRegex(
            ValueError,
            "settlement outcome authority changed",
        ):
            engine.settle_ready(book)

        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("90"))

    def test_whole_outcome_dict_replacement_cannot_drive_settlement(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        engine = SettlementEngine({leg.quote_key: "win"})

        engine.outcomes = {leg.quote_key: "loss"}

        with self.assertRaisesRegex(
            ValueError,
            "settlement outcome authority changed",
        ):
            engine.settle_ready(book)

        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("90"))


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


    def test_public_settlement_entry_class_bindings_are_immutable(self) -> None:
        canonical_record = SettlementEngine.record
        canonical_settle_ready = SettlementEngine.settle_ready

        def forged_record(_engine, _outcomes) -> None:
            return None

        def forged_settle_ready(_engine, _book) -> list[str]:
            return ["forged-settlement"]

        for name, forged in (
            ("record", forged_record),
            ("settle_ready", forged_settle_ready),
        ):
            with self.assertRaisesRegex(
                TypeError,
                "canonical settlement public entry binding is immutable",
            ):
                setattr(SettlementEngine, name, forged)
            with self.assertRaisesRegex(
                TypeError,
                "canonical settlement public entry binding is immutable",
            ):
                delattr(SettlementEngine, name)

        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement public entry binding is immutable",
        ):
            SettlementEngine._public_entry_bindings_sealed = False

        self.assertIs(SettlementEngine.record, canonical_record)
        self.assertIs(SettlementEngine.settle_ready, canonical_settle_ready)

        # Calling the builtin metaclass mutator directly bypasses an ordinary
        # custom __setattr__ override.  The metaclass data descriptors must still
        # reject both replacement and deletion at that lower dispatch layer.
        for name, forged in (
            ("record", forged_record),
            ("settle_ready", forged_settle_ready),
        ):
            with self.assertRaisesRegex(
                TypeError,
                "canonical settlement public entry binding is immutable",
            ):
                type.__setattr__(SettlementEngine, name, forged)
            with self.assertRaisesRegex(
                TypeError,
                "canonical settlement public entry binding is immutable",
            ):
                type.__delattr__(SettlementEngine, name)

        self.assertIs(SettlementEngine.record, canonical_record)
        self.assertIs(SettlementEngine.settle_ready, canonical_settle_ready)

        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        engine = SettlementEngine({leg.quote_key: "win"})
        self.assertEqual(engine.settle_ready(book), [ticket.ticket_id])
        self.assertIs(ticket.status, TicketStatus.WON)
        self.assertEqual(ticket.payout, Decimal("20"))
        self.assertEqual(book.balance, Decimal("110"))


    def test_coherent_outcomes_and_authority_slot_rewrite_cannot_mint_truth(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        engine = SettlementEngine({leg.quote_key: "win"})

        # Reproduce the predecessor bypass exactly: rewrite public truth and then
        # rewrite the nominal authority slot to a matching caller-built snapshot.
        engine.outcomes[leg.quote_key] = "loss"
        engine._outcomes_authority = tuple(engine.outcomes.items())

        with self.assertRaisesRegex(
            ValueError,
            "settlement outcome authority changed",
        ):
            engine.settle_ready(book)

        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(ticket.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("90"))

    def test_cross_engine_outcome_authority_token_cannot_be_transplanted(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event-1", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-09-23T12:00:00+00:00",
        )
        target = SettlementEngine({leg.quote_key: "win"})
        donor = SettlementEngine({leg.quote_key: "loss"})

        target.outcomes = {leg.quote_key: "loss"}
        target._outcomes_authority = donor._outcomes_authority

        with self.assertRaisesRegex(
            ValueError,
            "settlement outcome authority changed",
        ):
            target.settle_ready(book)

        self.assertIs(ticket.status, TicketStatus.OPEN)
        self.assertEqual(book.balance, Decimal("90"))

    def test_fabricated_outcome_authority_token_is_not_registered(self) -> None:
        engine = SettlementEngine({"event-1|winner|alice": "win"})
        token_type = type(engine._outcomes_authority)
        engine._outcomes_authority = token_type()

        with self.assertRaisesRegex(
            ValueError,
            "settlement outcome authority changed",
        ):
            engine.record({})

    def test_canonical_record_revokes_predecessor_outcome_authority_token(self) -> None:
        key = "event-1|winner|alice"
        engine = SettlementEngine({key: "win"})
        stale_outcomes = engine.outcomes
        stale_token = engine._outcomes_authority

        engine.record({"event-2|winner|bob": "void"})
        self.assertIsNot(engine._outcomes_authority, stale_token)

        # Even if caller retained both predecessor objects, the token registry
        # revokes that generation when canonical record() advances authority.
        engine.outcomes = stale_outcomes
        engine._outcomes_authority = stale_token

        with self.assertRaisesRegex(
            ValueError,
            "settlement outcome authority changed",
        ):
            engine.record({})

    def test_constructor_outcome_authority_initializer_binding_is_immutable(self) -> None:
        canonical_post_init = SettlementEngine.__post_init__

        def forged_post_init(_engine) -> None:
            return None

        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement public entry binding is immutable",
        ):
            SettlementEngine.__post_init__ = forged_post_init
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement public entry binding is immutable",
        ):
            del SettlementEngine.__post_init__
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement public entry binding is immutable",
        ):
            type.__setattr__(SettlementEngine, "__post_init__", forged_post_init)
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement public entry binding is immutable",
        ):
            type.__delattr__(SettlementEngine, "__post_init__")

        self.assertIs(SettlementEngine.__post_init__, canonical_post_init)
        engine = SettlementEngine({"event-1|winner|alice": "win"})
        self.assertIsNotNone(engine._outcomes_authority)


if __name__ == "__main__":
    unittest.main()
