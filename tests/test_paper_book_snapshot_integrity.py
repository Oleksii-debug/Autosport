import json
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


class _HostileDecimal(Decimal):
    def __mul__(self, other: object) -> Decimal:
        return Decimal("1000000")

    def __rmul__(self, other: object) -> Decimal:
        return Decimal("1000000")


class _StringSubclass(str):
    pass


class _BytesSubclass(bytes):
    pass


class _HostileStringConvertible:
    calls = 0

    def __str__(self) -> str:
        type(self).calls += 1
        return "10"


class _HostilePathSubclass(str):
    fspath_calls = 0

    def __fspath__(self) -> str:
        type(self).fspath_calls += 1
        return str(self)


class _HostileIterable:
    calls = 0

    def __iter__(self):
        type(self).calls += 1
        return iter(())


class _HostileLifecycleAction(str):
    comparisons = 0

    def __hash__(self) -> int:
        type(self).comparisons += 1
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)


class _HostileHashValue:
    calls = 0

    def __hash__(self) -> int:
        type(self).calls += 1
        return 1


class _HostileComparableString(str):
    comparisons = 0

    def __hash__(self) -> int:
        type(self).comparisons += 1
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)


class _HostilePaperBook(PaperBook):
    hash_calls = 0
    equality_calls = 0

    def __hash__(self) -> int:
        type(self).hash_calls += 1
        return object.__hash__(self)

    def __eq__(self, other: object) -> bool:
        type(self).equality_calls += 1
        return self is other


class PaperBookSnapshotIntegrityTests(unittest.TestCase):
    def _snapshot(self, raw: dict) -> Path:
        root = Path(self._tmp.name)
        path = root / "paper_book.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trip_preserves_valid_open_and_settled_state(self):
        book = PaperBook("100")
        won_leg = TicketLeg("e1", "m", "a", locked_odds=Decimal("2"))
        open_leg = TicketLeg("e2", "m", "b", locked_odds=Decimal("1.5"))
        won = book.open_ticket([won_leg], "10")
        book.settle(won.ticket_id, {won_leg.quote_key})
        book.open_ticket([open_leg], "5")
        path = Path(self._tmp.name) / "paper_book.json"
        book.save(path)

        restored = PaperBook.load(path)

        self.assertEqual(restored.balance, book.balance)
        self.assertEqual(restored.committed_stake, book.committed_stake)
        self.assertEqual(set(restored.tickets), set(book.tickets))

    def test_open_ticket_rejects_duplicate_quote_key_without_mutating_bankroll(self):
        book = PaperBook("100")
        first = TicketLeg("e", "m", "a", locked_odds=Decimal("2"))
        duplicate = TicketLeg("e", "m", "a", locked_odds=Decimal("3"))

        with self.assertRaisesRegex(ValueError, "duplicate quote_key"):
            book.open_ticket((leg for leg in [first, duplicate]), "10")

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})




    def test_hidden_authority_registries_do_not_execute_paperbook_hash_or_equality(self):
        _HostilePaperBook.hash_calls = 0
        _HostilePaperBook.equality_calls = 0

        book = _HostilePaperBook("100")
        self.assertEqual(_HostilePaperBook.hash_calls, 0)
        self.assertEqual(_HostilePaperBook.equality_calls, 0)

        leg = TicketLeg(
            "event-hostile-book-identity",
            "market-hostile-book-identity",
            "selection-hostile-book-identity",
            locked_odds=Decimal("2"),
        )
        ticket = book.open_ticket([leg], "10")
        self.assertEqual(book.committed_stake, Decimal("10"))
        self.assertEqual(book.tickets[ticket.ticket_id].stake, Decimal("10"))
        self.assertEqual(_HostilePaperBook.hash_calls, 0)
        self.assertEqual(_HostilePaperBook.equality_calls, 0)

    def test_constructor_rejects_arbitrary_decimal_input_before_str(self):
        _HostileStringConvertible.calls = 0

        with self.assertRaisesRegex(ValueError, "exact built-in Decimal"):
            PaperBook(_HostileStringConvertible())

        self.assertEqual(_HostileStringConvertible.calls, 0)

    def test_open_ticket_rejects_arbitrary_stake_before_str_and_mutation(self):
        _HostileStringConvertible.calls = 0
        book = PaperBook("100")
        leg = TicketLeg(
            "event-hostile-stake",
            "market-hostile-stake",
            "selection-hostile-stake",
            locked_odds=Decimal("2"),
        )

        with self.assertRaisesRegex(ValueError, "exact built-in Decimal"):
            book.open_ticket([leg], _HostileStringConvertible())

        self.assertEqual(_HostileStringConvertible.calls, 0)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_exact_decimal_ingress_preserves_object_without_text_roundtrip(self):
        value = Decimal("123.4500")
        book = PaperBook(value)

        self.assertIs(book.initial_bankroll, value)
        self.assertIs(book.balance, value)
        self.assertEqual(book.balance.as_tuple(), value.as_tuple())

    def test_decimal_ingress_preserves_supported_exact_builtin_inputs(self):
        for value in (
            Decimal("100"),
            "100",
            100,
            100.0,
        ):
            book = PaperBook(value)
            self.assertEqual(book.balance, Decimal("100"))


    def test_constructor_rejects_oversized_decimal_text_before_decimal_parse(self):
        with self.assertRaisesRegex(ValueError, "canonical size limit"):
            PaperBook("1" * 513)

    def test_open_ticket_rejects_oversized_stake_text_before_mutation(self):
        book = PaperBook("100")
        leg = TicketLeg(
            "event-oversized-stake",
            "market-oversized-stake",
            "selection-oversized-stake",
            locked_odds=Decimal("2"),
        )

        with self.assertRaisesRegex(ValueError, "canonical size limit"):
            book.open_ticket([leg], "1" * 513)

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})


    def test_open_ticket_rejects_arbitrary_leg_iterable_before_iteration(self):
        _HostileIterable.calls = 0
        book = PaperBook("100")

        with self.assertRaisesRegex(ValueError, "exact list or tuple"):
            book.open_ticket(_HostileIterable(), "10")

        self.assertEqual(_HostileIterable.calls, 0)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})


    def test_settle_rejects_hostile_key_element_before_hashing(self):
        _HostileHashValue.calls = 0
        book = PaperBook("100")
        leg = TicketLeg(
            "event-hostile-key",
            "market-hostile-key",
            "selection-hostile-key",
            locked_odds=Decimal("2"),
        )
        ticket = book.open_ticket([leg], "10")
        balance_before = book.balance

        with self.assertRaisesRegex(ValueError, "non-empty string quote keys"):
            book.settle(ticket.ticket_id, [_HostileHashValue()])

        self.assertEqual(_HostileHashValue.calls, 0)
        self.assertEqual(book.balance, balance_before)
        self.assertEqual(book.tickets[ticket.ticket_id].status.value, "open")

    def test_settle_rejects_arbitrary_key_iterable_before_iteration(self):
        _HostileIterable.calls = 0
        book = PaperBook("100")
        leg = TicketLeg(
            "event-hostile-iterable",
            "market-hostile-iterable",
            "selection-hostile-iterable",
            locked_odds=Decimal("2"),
        )
        ticket = book.open_ticket([leg], "10")
        balance_before = book.balance

        with self.assertRaisesRegex(ValueError, "exact built-in collection"):
            book.settle(ticket.ticket_id, _HostileIterable())

        self.assertEqual(_HostileIterable.calls, 0)
        self.assertEqual(book.balance, balance_before)
        self.assertEqual(book.tickets[ticket.ticket_id].status.value, "open")

    def test_open_ticket_rejects_decimal_subclass_odds_before_mutation(self):
        book = PaperBook("100")
        leg = TicketLeg(
            "event-hostile-decimal",
            "market-hostile-decimal",
            "selection-hostile-decimal",
            locked_odds=_HostileDecimal("2"),
        )

        with self.assertRaisesRegex(ValueError, "non-finite locked_odds"):
            book.open_ticket([leg], "10")

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_open_ticket_rejects_string_subclass_identity_before_mutation(self):
        book = PaperBook("100")
        leg = TicketLeg(
            _StringSubclass("event-hostile-string"),
            "market-hostile-string",
            "selection-hostile-string",
            locked_odds=Decimal("2"),
        )

        with self.assertRaisesRegex(ValueError, "event_id"):
            book.open_ticket([leg], "10")

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})


    def test_settle_rejects_string_subclass_quote_key_before_mutation(self):
        book = PaperBook("100")
        leg = TicketLeg(
            "event-settle-subclass",
            "market-settle-subclass",
            "selection-settle-subclass",
            locked_odds=Decimal("2"),
        )
        ticket = book.open_ticket([leg], "10")
        balance_before = book.balance

        with self.assertRaisesRegex(ValueError, "non-empty string quote keys"):
            book.settle(
                ticket.ticket_id,
                {_StringSubclass(leg.quote_key)},
            )

        self.assertEqual(book.balance, balance_before)
        self.assertEqual(book.tickets[ticket.ticket_id].status.value, "open")



    def test_settle_rejects_ticket_id_subclass_before_lookup(self):
        book = PaperBook("100")
        leg = TicketLeg(
            "event-ticket-id-subclass",
            "market-ticket-id-subclass",
            "selection-ticket-id-subclass",
            locked_odds=Decimal("2"),
        )
        ticket = book.open_ticket([leg], "10")
        balance_before = book.balance

        with self.assertRaisesRegex(ValueError, "ticket_id"):
            book.settle(
                _StringSubclass(ticket.ticket_id),
                {leg.quote_key},
            )

        self.assertEqual(book.balance, balance_before)
        self.assertEqual(book.tickets[ticket.ticket_id].status.value, "open")

    def test_settle_rejects_string_subclass_used_as_key_collection(self):
        book = PaperBook("100")
        leg = TicketLeg(
            "event-settle-string-container",
            "market-settle-string-container",
            "selection-settle-string-container",
            locked_odds=Decimal("2"),
        )
        ticket = book.open_ticket([leg], "10")
        balance_before = book.balance

        with self.assertRaisesRegex(ValueError, "collection of quote keys"):
            book.settle(
                ticket.ticket_id,
                _StringSubclass(leg.quote_key),
            )

        self.assertEqual(book.balance, balance_before)
        self.assertEqual(book.tickets[ticket.ticket_id].status.value, "open")

    def test_snapshot_decimal_parser_rejects_oversized_text(self):
        with self.assertRaisesRegex(ValueError, "canonical size limit"):
            PaperBook._parse_snapshot_decimal("1" * 513, "balance")

    def test_snapshot_decimal_parser_rejects_string_subclass(self):
        with self.assertRaisesRegex(ValueError, "decimal string"):
            PaperBook._parse_snapshot_decimal(
                _StringSubclass("1.25"),
                "balance",
            )

    def test_snapshot_status_parser_rejects_string_subclass(self):
        with self.assertRaisesRegex(ValueError, "must be a string"):
            PaperBook._parse_snapshot_status(
                _StringSubclass("open"),
                "ticket-subclass-status",
            )


    def test_save_rejects_snapshot_path_subclass_before_fspath(self):
        _HostilePathSubclass.fspath_calls = 0
        book = PaperBook("100")

        with self.assertRaisesRegex(TypeError, "exact str or exact Path"):
            book.save(_HostilePathSubclass("snapshot.json"))

        self.assertEqual(_HostilePathSubclass.fspath_calls, 0)

    def test_load_rejects_snapshot_path_subclass_before_fspath(self):
        _HostilePathSubclass.fspath_calls = 0

        with self.assertRaisesRegex(TypeError, "exact str or exact Path"):
            PaperBook.load(_HostilePathSubclass("snapshot.json"))

        self.assertEqual(_HostilePathSubclass.fspath_calls, 0)

    def test_load_bytes_rejects_bytes_subclass_before_decode(self):
        with self.assertRaisesRegex(TypeError, "exact bytes"):
            PaperBook.load_bytes(_BytesSubclass(b"{}"))



    def test_settlement_witness_key_rejected_before_rehash(self):
        book = PaperBook("100")
        hostile = _HostileHashValue()
        book._settlement_times[hostile] = None
        _HostileHashValue.calls = 0

        with self.assertRaisesRegex(ValueError, "keys must be canonical strings"):
            PaperBook._validate_lifecycle_reachability(book)

        self.assertEqual(_HostileHashValue.calls, 0)

    def test_lifecycle_action_subclass_rejected_before_membership(self):
        _HostileLifecycleAction.comparisons = 0

        with self.assertRaisesRegex(ValueError, "canonical open or settle text"):
            PaperBook._validate_lifecycle_entry(
                (_HostileLifecycleAction("open"), "ticket-1", (), ())
            )

        self.assertEqual(_HostileLifecycleAction.comparisons, 0)

    def test_public_operations_validate_hostile_ticket_state_before_authority_comparison(self):
        operations = ("committed_stake", "open_ticket", "settle", "save")
        for operation in operations:
            with self.subTest(operation=operation):
                book = PaperBook("100")
                leg = TicketLeg(
                    f"event-{operation}",
                    "market-hostile-authority-order",
                    "selection-hostile-authority-order",
                    locked_odds=Decimal("2"),
                )
                ticket = book.open_ticket([leg], "10", reason="safe")
                balance_before = book.balance
                _HostileComparableString.comparisons = 0
                ticket.strategy_reason = _HostileComparableString("safe")

                with self.assertRaisesRegex(ValueError, "strategy_reason must be a string"):
                    if operation == "committed_stake":
                        _ = book.committed_stake
                    elif operation == "open_ticket":
                        another = TicketLeg(
                            f"event-new-{operation}",
                            "market-hostile-authority-order",
                            "selection-new",
                            locked_odds=Decimal("2"),
                        )
                        book.open_ticket([another], "1")
                    elif operation == "settle":
                        book.settle(ticket.ticket_id, {leg.quote_key})
                    else:
                        book.save(Path(self._tmp.name) / "hostile-authority.json")

                self.assertEqual(_HostileComparableString.comparisons, 0)
                self.assertEqual(book.balance, balance_before)
                self.assertEqual(book.tickets[ticket.ticket_id].status.value, "open")

    def test_public_operation_rejects_hostile_lifecycle_action_before_authority_comparison(self):
        book = PaperBook("100")
        leg = TicketLeg(
            "event-hostile-lifecycle-authority",
            "market-hostile-lifecycle-authority",
            "selection-hostile-lifecycle-authority",
            locked_odds=Decimal("2"),
        )
        ticket = book.open_ticket([leg], "10")
        _HostileLifecycleAction.comparisons = 0
        book._lifecycle[0] = (
            _HostileLifecycleAction("open"),
            ticket.ticket_id,
            (),
            (),
        )

        with self.assertRaisesRegex(ValueError, "canonical open or settle text"):
            _ = book.committed_stake

        self.assertEqual(_HostileLifecycleAction.comparisons, 0)

    def test_concurrent_open_ticket_cannot_overcommit_one_bankroll(self):
        book = PaperBook("100")
        start = threading.Barrier(3)
        active_guard = threading.Lock()
        active_debits = 0
        max_active_debits = 0
        successes: list[str] = []
        failures: list[BaseException] = []
        original_debit = PaperBook._debit_balance

        def slow_debit(cls, balance: Decimal, amount: Decimal) -> Decimal:
            nonlocal active_debits, max_active_debits
            with active_guard:
                active_debits += 1
                max_active_debits = max(max_active_debits, active_debits)
            try:
                time.sleep(0.05)
                return original_debit(balance, amount)
            finally:
                with active_guard:
                    active_debits -= 1

        def worker(index: int) -> None:
            leg = TicketLeg(
                f"event-concurrent-{index}",
                "market-concurrent-overcommit",
                f"selection-{index}",
                locked_odds=Decimal("2"),
            )
            start.wait()
            try:
                ticket = book.open_ticket([leg], "60")
            except BaseException as exc:
                failures.append(exc)
            else:
                successes.append(ticket.ticket_id)

        with patch.object(PaperBook, "_debit_balance", classmethod(slow_debit)):
            threads = [
                threading.Thread(target=worker, args=(1,)),
                threading.Thread(target=worker, args=(2,)),
            ]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

        self.assertEqual(max_active_debits, 1)
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ValueError)
        self.assertIn("insufficient virtual bankroll", str(failures[0]))
        self.assertEqual(book.balance, Decimal("40"))
        self.assertEqual(len(book.tickets), 1)
        self.assertEqual(book.committed_stake, Decimal("60"))

    def test_load_rejects_balance_not_explained_by_ticket_economics(self):
        path = self._snapshot(
            {
                "initial_bankroll": "100",
                "balance": "99",
                "tickets": [],
            }
        )
        with self.assertRaisesRegex(ValueError, "balance is inconsistent"):
            PaperBook.load_bytes(path.read_bytes())

    def test_load_rejects_duplicate_ticket_identity_instead_of_overwriting(self):
        ticket = {
            "ticket_id": "same",
            "stake": "10",
            "placed_at": "2026-01-01T00:00:00+00:00",
            "status": "open",
            "payout": "0",
            "strategy_reason": "test",
            "legs": [
                {
                    "event_id": "e",
                    "market_id": "m",
                    "selection_id": "a",
                    "locked_odds": "2",
                }
            ],
        }
        path = self._snapshot(
            {
                "initial_bankroll": "100",
                "balance": "80",
                "tickets": [ticket, dict(ticket)],
            }
        )
        with self.assertRaisesRegex(ValueError, "duplicate ticket_id"):
            PaperBook.load_bytes(path.read_bytes())

    def test_load_rejects_duplicate_quote_key_legs(self):
        leg = {
            "event_id": "e",
            "market_id": "m",
            "selection_id": "a",
            "locked_odds": "2",
        }
        path = self._snapshot(
            {
                "initial_bankroll": "100",
                "balance": "90",
                "tickets": [
                    {
                        "ticket_id": "duplicate-leg",
                        "stake": "10",
                        "placed_at": "2026-01-01T00:00:00+00:00",
                        "status": "open",
                        "payout": "0",
                        "strategy_reason": "test",
                        "legs": [leg, dict(leg)],
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "duplicate quote_key"):
            PaperBook.load_bytes(path.read_bytes())

    def test_load_rejects_impossible_status_payout(self):
        path = self._snapshot(
            {
                "initial_bankroll": "100",
                "balance": "95",
                "tickets": [
                    {
                        "ticket_id": "lost-with-payout",
                        "stake": "10",
                        "placed_at": "2026-01-01T00:00:00+00:00",
                        "status": "lost",
                        "payout": "5",
                        "strategy_reason": "test",
                        "legs": [
                            {
                                "event_id": "e",
                                "market_id": "m",
                                "selection_id": "a",
                                "locked_odds": "2",
                            }
                        ],
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "open/lost ticket payout must be zero"):
            PaperBook.load_bytes(path.read_bytes())

    def test_load_rejects_non_finite_economic_values(self):
        path = self._snapshot(
            {
                "initial_bankroll": "100",
                "balance": "Infinity",
                "tickets": [],
            }
        )
        with self.assertRaisesRegex(ValueError, "non-finite balance"):
            PaperBook.load_bytes(path.read_bytes())


if __name__ == "__main__":
    unittest.main()
