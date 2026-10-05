import json
import os
import tempfile
import threading
import time
import unittest
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from autosport.domain import PaperTicket, TicketLeg, TicketStatus
import autosport.paper as paper_module
from autosport.paper import PaperBook


class _HostileDecimal(Decimal):
    def __mul__(self, other: object) -> Decimal:
        return Decimal("1000000")

    def __rmul__(self, other: object) -> Decimal:
        return Decimal("1000000")


class _TicketLegSubclass(TicketLeg):
    pass


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


class _HostilePaperBookIdentity(PaperBook):
    hash_calls = 0
    equality_calls = 0

    def __hash__(self) -> int:
        type(self).hash_calls += 1
        return 1

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




    def test_operation_lock_registry_uses_identity_without_hash_or_equality(self):
        book = object.__new__(_HostilePaperBookIdentity)
        _HostilePaperBookIdentity.hash_calls = 0
        _HostilePaperBookIdentity.equality_calls = 0

        paper_module._register_paperbook_operation_lock(book)
        lock = paper_module._require_paperbook_operation_lock(book)

        self.assertIsInstance(lock, type(threading.RLock()))
        self.assertEqual(_HostilePaperBookIdentity.hash_calls, 0)
        self.assertEqual(_HostilePaperBookIdentity.equality_calls, 0)


    def test_paperbook_economic_operations_ignore_registry_dispatch_rebinding(self):
        def forbidden_registry_dispatch(*args, **kwargs):
            raise AssertionError("rebound PaperBook registry dispatch executed")

        snapshot = Path(self._tmp.name) / "frozen-registry-dispatch.json"
        names = (
            "_register_paperbook_operation_lock",
            "_require_paperbook_operation_lock",
            "_register_ticket_opening_authority_book",
            "_record_ticket_opening_authority",
            "_revoke_ticket_opening_authority",
            "_install_validated_ticket_opening_authority",
            "_require_ticket_opening_authority",
            "_require_snapshot_candidate_opening_authority",
            "_register_paperbook_causal_history_authority_book",
            "_revoke_paperbook_causal_history_authority",
            "_install_validated_paperbook_causal_history_authority",
            "_require_paperbook_causal_history_authority",
            "_require_snapshot_candidate_causal_history_authority",
            "_advance_paperbook_causal_history_open",
            "_advance_paperbook_causal_history_settle",
        )
        replacements = {name: forbidden_registry_dispatch for name in names}

        with patch.multiple(paper_module, **replacements):
            book = PaperBook("100")
            leg = TicketLeg(
                "event-registry-freeze",
                "market-registry-freeze",
                "selection-registry-freeze",
                locked_odds=Decimal("2"),
            )
            ticket = book.open_ticket([leg], "10")
            self.assertEqual(book.committed_stake, Decimal("10"))
            book.save(snapshot)

            restored = PaperBook.load(snapshot)
            self.assertEqual(restored.committed_stake, Decimal("10"))

            decoded_only = PaperBook.load_bytes(snapshot.read_bytes())
            with self.assertRaisesRegex(ValueError, "lacks product-issued"):
                _ = decoded_only.committed_stake

            self.assertIn(ticket.ticket_id, restored.tickets)


    def test_paperbook_registries_reject_same_instance_rebinding(self):
        book = PaperBook("100")
        original_lock = paper_module._require_paperbook_operation_lock(book)

        with self.assertRaisesRegex(RuntimeError, "operation lock registry is already registered"):
            paper_module._register_paperbook_operation_lock(book)
        with self.assertRaisesRegex(RuntimeError, "opening authority registry is already registered"):
            paper_module._register_ticket_opening_authority_book(book)
        with self.assertRaisesRegex(RuntimeError, "causal history authority registry is already registered"):
            paper_module._register_paperbook_causal_history_authority_book(book)

        self.assertIs(
            paper_module._require_paperbook_operation_lock(book),
            original_lock,
        )
        paper_module._require_ticket_opening_authority(book)
        paper_module._require_paperbook_causal_history_authority(book)


    def test_product_authority_registries_use_identity_without_hash_or_equality(self):
        book = object.__new__(_HostilePaperBookIdentity)
        book.tickets = {}
        book._lifecycle = []
        book._settlement_times = {}
        _HostilePaperBookIdentity.hash_calls = 0
        _HostilePaperBookIdentity.equality_calls = 0

        paper_module._register_ticket_opening_authority_book(book)
        paper_module._register_paperbook_causal_history_authority_book(book)
        paper_module._require_ticket_opening_authority(book)
        paper_module._require_paperbook_causal_history_authority(book)

        self.assertEqual(_HostilePaperBookIdentity.hash_calls, 0)
        self.assertEqual(_HostilePaperBookIdentity.equality_calls, 0)


    def test_paperbook_constructor_rejects_subclass_before_registry_hooks(self):
        _HostilePaperBookIdentity.hash_calls = 0
        _HostilePaperBookIdentity.equality_calls = 0

        with self.assertRaisesRegex(TypeError, "exact book type"):
            _HostilePaperBookIdentity("100")

        self.assertEqual(_HostilePaperBookIdentity.hash_calls, 0)
        self.assertEqual(_HostilePaperBookIdentity.equality_calls, 0)

    def test_paperbook_runtime_class_swap_fails_closed_before_economic_readout(self):
        book = PaperBook("100")
        book.__class__ = _HostilePaperBookIdentity
        _HostilePaperBookIdentity.hash_calls = 0
        _HostilePaperBookIdentity.equality_calls = 0

        with self.assertRaisesRegex(TypeError, "exact book type"):
            _ = PaperBook.committed_stake.fget(book)

        self.assertEqual(_HostilePaperBookIdentity.hash_calls, 0)
        self.assertEqual(_HostilePaperBookIdentity.equality_calls, 0)

    def test_paperbook_subclass_load_bytes_rejected_before_decode(self):
        with self.assertRaisesRegex(TypeError, "exact book type"):
            _HostilePaperBookIdentity.load_bytes(b"not-json")


    def test_decimal_arithmetic_context_ignores_module_rebinding(self):
        def forbidden_decimal_dependency(*args, **kwargs):
            raise AssertionError("rebound Decimal arithmetic dependency executed")

        replacements = {
            "Context": forbidden_decimal_dependency,
            "DecimalException": RuntimeError,
            "Inexact": object(),
            "InvalidOperation": object(),
            "Overflow": object(),
            "Underflow": object(),
            "ROUND_HALF_EVEN": object(),
            "localcontext": forbidden_decimal_dependency,
            "_paper_decimal_context": forbidden_decimal_dependency,
        }
        with patch.multiple(paper_module, **replacements):
            book = PaperBook("100")
            leg = TicketLeg(
                "event-decimal-context",
                "market-decimal-context",
                "selection-decimal-context",
                locked_odds=Decimal("2"),
            )
            ticket = book.open_ticket([leg], "10")
            self.assertEqual(book.balance, Decimal("90"))
            settled = book.settle(ticket.ticket_id, {leg.quote_key})

        self.assertIs(settled.status, TicketStatus.WON)
        self.assertEqual(settled.payout, Decimal("20"))
        self.assertEqual(book.balance, Decimal("110"))


    def test_decimal_authority_ignores_module_decimal_rebinding(self):
        with patch.object(paper_module, "Decimal", _HostileDecimal):
            with self.assertRaisesRegex(ValueError, "exact built-in Decimal"):
                PaperBook(_HostileDecimal("100"))

            book = PaperBook("100")
            self.assertIs(type(book.balance), Decimal)

            object.__setattr__(book, "balance", _HostileDecimal("100"))
            with self.assertRaisesRegex(ValueError, "non-finite balance"):
                _ = book.committed_stake

    def test_snapshot_decimal_parser_ignores_module_decimal_rebinding(self):
        with patch.object(paper_module, "Decimal", _HostileDecimal):
            parsed = PaperBook._parse_snapshot_decimal("1.25", "balance")
            self.assertIs(type(parsed), Decimal)
            self.assertEqual(parsed, Decimal("1.25"))

    def test_snapshot_path_authority_ignores_path_method_rebinding(self):
        path = Path(self._tmp.name) / "pinned-path-methods.json"
        book = PaperBook("100")

        def forbidden(*args, **kwargs):
            raise AssertionError("rebound Path method executed")

        with patch.object(Path, "resolve", forbidden), patch.object(
            Path,
            "mkdir",
            forbidden,
        ), patch.object(
            Path,
            "unlink",
            forbidden,
        ):
            book.save(path)
            restored = PaperBook.load(path)

        self.assertEqual(restored.balance, Decimal("100"))


    def test_snapshot_path_authority_ignores_module_path_rebinding(self):
        path = Path(self._tmp.name) / "pinned-path.json"
        book = PaperBook("100")

        with patch.object(
            paper_module,
            "Path",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("rebound Path executed")
            ),
        ):
            book.save(path)
            restored = PaperBook.load(path)

        self.assertEqual(restored.balance, book.balance)


    def test_snapshot_json_authority_ignores_module_dependency_rebinding(self):
        def forbidden_json_dependency(*args, **kwargs):
            raise AssertionError("rebound snapshot JSON dependency executed")

        snapshot = Path(self._tmp.name) / "frozen-json-authority.json"
        book = PaperBook("100")

        with (
            patch.object(paper_module, "json", object()),
            patch.object(
                paper_module,
                "_reject_duplicate_json_keys",
                forbidden_json_dependency,
            ),
            patch.object(
                paper_module,
                "_reject_nonfinite_json_constant",
                forbidden_json_dependency,
            ),
        ):
            book.save(snapshot)
            restored = PaperBook.load(snapshot)

        self.assertEqual(restored.balance, Decimal("100"))


    def test_ticket_materialization_authority_ignores_module_dependency_rebinding(self):
        def forbidden_dependency(*args, **kwargs):
            raise AssertionError("rebound ticket/time dependency executed")

        snapshot = Path(self._tmp.name) / "frozen-ticket-authority.json"
        with (
            patch.object(paper_module, "PaperTicket", forbidden_dependency),
            patch.object(paper_module, "TicketLeg", forbidden_dependency),
            patch.object(paper_module, "utc_now_iso", forbidden_dependency),
            patch.object(paper_module, "parse_iso_timestamp", forbidden_dependency),
            patch.object(paper_module, "uuid", object()),
        ):
            book = PaperBook("100")
            leg = TicketLeg(
                "event-frozen-deps",
                "market-frozen-deps",
                "selection-frozen-deps",
                locked_odds=Decimal("2"),
            )
            ticket = book.open_ticket([leg], "10")
            self.assertIs(type(ticket), PaperTicket)
            book.save(snapshot)
            restored = PaperBook.load(snapshot)

        self.assertIs(type(restored.tickets[ticket.ticket_id]), PaperTicket)
        self.assertEqual(restored.balance, Decimal("90"))


    def test_ticket_status_authority_ignores_module_status_rebinding(self):
        with patch.object(paper_module, "TicketStatus", object()):
            book = PaperBook("100")
            leg = TicketLeg(
                "event-status-rebind",
                "market-status-rebind",
                "selection-status-rebind",
                locked_odds=Decimal("2"),
            )
            ticket = book.open_ticket([leg], "10")
            self.assertIs(ticket.status, TicketStatus.OPEN)
            settled = book.settle(ticket.ticket_id, {leg.quote_key})
            self.assertIs(settled.status, TicketStatus.WON)

            parsed = PaperBook._parse_snapshot_status("open", "ticket-status-rebind")
            self.assertIs(parsed, TicketStatus.OPEN)


    def test_ticket_and_leg_constructors_ignore_module_rebinding(self):
        path = Path(self._tmp.name) / "domain-type-witness.json"
        with patch.object(paper_module, "PaperTicket", object()), patch.object(
            paper_module,
            "TicketLeg",
            object(),
        ):
            book = PaperBook("100")
            leg = TicketLeg(
                "event-domain-witness",
                "market-domain-witness",
                "selection-domain-witness",
                locked_odds=Decimal("2"),
            )
            ticket = book.open_ticket([leg], "10")
            self.assertIs(type(ticket), PaperTicket)
            self.assertIs(type(ticket.legs[0]), TicketLeg)
            book.save(path)
            restored = PaperBook.load(path)

        restored_ticket = next(iter(restored.tickets.values()))
        self.assertIs(type(restored_ticket), PaperTicket)
        self.assertIs(type(restored_ticket.legs[0]), TicketLeg)

    def test_ticket_leg_subclass_not_made_canonical_by_export_rebinding(self):
        book = PaperBook("100")
        forged = _TicketLegSubclass(
            "event-forged-leg",
            "market-forged-leg",
            "selection-forged-leg",
            locked_odds=Decimal("2"),
        )

        with patch.object(paper_module, "TicketLeg", _TicketLegSubclass):
            with self.assertRaisesRegex(ValueError, "canonical TicketLeg"):
                book.open_ticket([forged], "1")

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})


    def test_public_economic_operations_ignore_helper_method_rebinding(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("rebound PaperBook helper executed")

        helper_names = (
            "_validate_loaded_state",
            "_canonical_decimal_input",
            "_debit_balance",
            "_validate_placed_at",
            "_require_utf8_string",
            "_require_finite",
            "_validate_timestamp",
            "_require_canonical_text",
            "_validate_ticket_provenance",
            "_validate_ticket_leg",
            "_normalize_resolution_keys",
            "_validate_settled_at",
            "_settlement_result",
            "_canonical_snapshot_path",
            "_from_raw_snapshot",
            "_lifecycle_to_json",
            "_parse_snapshot_decimal",
            "_parse_snapshot_status",
            "_parse_snapshot_legs",
            "_parse_snapshot_provider_accounts",
            "_required_snapshot_field",
            "_parse_lifecycle",
            "_parse_lifecycle_key_list",
            "_validate_lifecycle_entry",
            "_validate_lifecycle_reachability",
        )
        path = Path(self._tmp.name) / "method-witness.json"

        with patch.multiple(
            PaperBook,
            **{name: forbidden for name in helper_names},
        ):
            book = PaperBook("100")
            for name in helper_names:
                setattr(book, name, forbidden)

            leg = TicketLeg(
                "event-method-witness",
                "market-method-witness",
                "selection-method-witness",
                locked_odds=Decimal("2"),
            )
            ticket = book.open_ticket([leg], "10")
            self.assertEqual(book.committed_stake, Decimal("10"))
            settled = book.settle(ticket.ticket_id, {leg.quote_key})
            self.assertIs(settled.status, TicketStatus.WON)
            book.save(path)
            restored = PaperBook.load(path)

        self.assertEqual(restored.balance, Decimal("110"))


    def test_decimal_arithmetic_ignores_module_context_rebinding(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("rebound Decimal arithmetic dependency executed")

        with patch.multiple(
            paper_module,
            Context=forbidden,
            localcontext=forbidden,
            _paper_decimal_context=forbidden,
            ROUND_HALF_EVEN="forged-rounding",
        ):
            book = PaperBook("100")
            leg = TicketLeg(
                "event-decimal-context",
                "market-decimal-context",
                "selection-decimal-context",
                locked_odds=Decimal("2"),
            )
            ticket = book.open_ticket([leg], "10")
            self.assertEqual(book.balance, Decimal("90"))
            settled = book.settle(ticket.ticket_id, {leg.quote_key})
            self.assertEqual(settled.payout, Decimal("20"))
            self.assertEqual(book.balance, Decimal("110"))


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


    def test_snapshot_publication_ignores_module_io_rebinding(self):
        snapshot = Path(self._tmp.name) / "frozen-publication-io.json"
        book = PaperBook("100")

        class _ForbiddenModule:
            def __getattr__(self, name):
                raise AssertionError(f"rebound module I/O dependency executed: {name}")

        with (
            patch.object(paper_module, "tempfile", _ForbiddenModule()),
            patch.object(paper_module, "os", _ForbiddenModule()),
        ):
            book.save(snapshot)

        restored = PaperBook.load(snapshot)
        self.assertEqual(restored.balance, Decimal("100"))


    def test_save_relative_snapshot_path_survives_cwd_change_during_io(self):
        root = Path(self._tmp.name)
        first = root / "cwd-first"
        second = root / "cwd-second"
        first.mkdir()
        second.mkdir()
        old_cwd = Path.cwd()
        book = PaperBook("100")
        original_named_temporary_file = tempfile.NamedTemporaryFile

        def changing_cwd_named_temporary_file(*args, **kwargs):
            os.chdir(second)
            return original_named_temporary_file(*args, **kwargs)

        try:
            os.chdir(first)
            with patch.object(
                paper_module,
                "_CANONICAL_NAMED_TEMPORARY_FILE",
                changing_cwd_named_temporary_file,
            ):
                book.save(Path("workspace") / "snapshot.json")
        finally:
            os.chdir(old_cwd)

        self.assertTrue((first / "workspace" / "snapshot.json").exists())
        self.assertFalse((second / "workspace" / "snapshot.json").exists())

    def test_load_relative_snapshot_path_survives_cwd_change_before_read(self):
        root = Path(self._tmp.name)
        first = root / "load-cwd-first"
        second = root / "load-cwd-second"
        first.mkdir()
        second.mkdir()
        source = first / "snapshot.json"
        PaperBook("100").save(source)
        original_read_bytes = Path.read_bytes
        old_cwd = Path.cwd()

        def changing_cwd_read_bytes(path_self):
            os.chdir(second)
            return original_read_bytes(path_self)

        try:
            os.chdir(first)
            with patch.object(Path, "read_bytes", changing_cwd_read_bytes):
                restored = PaperBook.load("snapshot.json")
        finally:
            os.chdir(old_cwd)

        self.assertEqual(restored.balance, Decimal("100"))


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

    def test_save_flushes_published_snapshot_and_parent_directory(self):
        path = Path(self._tmp.name) / "durable-paper-book.json"
        book = PaperBook("100")
        original_fsync = paper_module._CANONICAL_OS_FSYNC
        fsync_calls = 0

        def counting_fsync(descriptor):
            nonlocal fsync_calls
            fsync_calls += 1
            return original_fsync(descriptor)

        with patch.object(
            paper_module,
            "_CANONICAL_OS_FSYNC",
            counting_fsync,
        ):
            book.save(path)

        minimum = 2 if paper_module._CANONICAL_OS_NAME == "nt" else 3
        self.assertGreaterEqual(fsync_calls, minimum)
        self.assertEqual(PaperBook.load(path).balance, Decimal("100"))


    def test_save_rejects_snapshot_too_large_to_reload_before_publication(self):
        book = PaperBook("100")
        leg = TicketLeg(
            "event-oversize-save",
            "market-oversize-save",
            "selection-oversize-save",
            locked_odds=Decimal("2"),
        )
        book.open_ticket(
            [leg],
            "1",
            reason="x" * (8 * 1024 * 1024),
        )
        path = Path(self._tmp.name) / "oversized-save.json"

        with self.assertRaisesRegex(ValueError, "byte-size limit"):
            book.save(path)

        self.assertFalse(path.exists())


    def test_load_bytes_rejects_oversized_snapshot_before_decode(self):
        payload = b" " * ((8 * 1024 * 1024) + 1)

        with self.assertRaisesRegex(ValueError, "byte-size limit"):
            PaperBook.load_bytes(payload)

    def test_load_rejects_oversized_snapshot_with_bounded_file_read(self):
        path = Path(self._tmp.name) / "oversized-paper-book.json"
        path.write_bytes(b" " * ((8 * 1024 * 1024) + 1))

        with self.assertRaisesRegex(ValueError, "byte-size limit"):
            PaperBook.load(path)


    def test_verified_load_ignores_public_fdopen_rebinding(self):
        path = Path(self._tmp.name) / "pinned-fdopen.json"
        PaperBook("100").save(path)

        def forbidden(*args, **kwargs):
            raise AssertionError("rebound os.fdopen executed")

        with patch.object(paper_module.os, "fdopen", forbidden):
            restored = PaperBook.load(path)

        self.assertEqual(restored.balance, Decimal("100"))


    def test_verified_load_ignores_public_os_primitive_rebinding(self):
        path = Path(self._tmp.name) / "pinned-os-primitives.json"
        PaperBook("100").save(path)

        def forbidden(*args, **kwargs):
            raise AssertionError("rebound OS primitive executed")

        with patch.object(paper_module.os, "open", forbidden), patch.object(
            paper_module.os,
            "fstat",
            forbidden,
        ), patch.object(
            paper_module.os,
            "stat",
            forbidden,
        ), patch.object(
            paper_module.os,
            "close",
            forbidden,
        ), patch.object(
            paper_module.os.path,
            "sameopenfile",
            forbidden,
        ), patch.object(
            paper_module.stat,
            "S_ISREG",
            forbidden,
        ):
            restored = PaperBook.load(path)

        self.assertEqual(restored.balance, Decimal("100"))


    def test_load_rejects_in_place_snapshot_mutation_during_verified_read(self):
        path = Path(self._tmp.name) / "racing-paper-book.json"
        PaperBook("100").save(path)
        original_open = os.open
        calls = 0

        def racing_open(target, flags, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_text('{"schema_version":7}', encoding="utf-8")
            return original_open(target, flags, *args, **kwargs)

        with patch.object(paper_module.os, "open", racing_open):
            with self.assertRaisesRegex(ValueError, "bytes changed"):
                PaperBook.load(path)


    def test_load_rejects_symlinked_snapshot_final_component(self):
        outside = Path(self._tmp.name) / "outside-paper-book.json"
        PaperBook("100").save(outside)
        link = Path(self._tmp.name) / "linked-paper-book.json"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "non-symlink"):
            PaperBook.load(link)

    def test_load_rejects_hardlinked_snapshot_final_component(self):
        outside = Path(self._tmp.name) / "outside-hardlink-paper-book.json"
        PaperBook("100").save(outside)
        link = Path(self._tmp.name) / "hardlinked-paper-book.json"
        try:
            os.link(outside, link)
        except OSError as exc:
            self.skipTest(f"hard-link creation unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "hard-link aliases"):
            PaperBook.load(link)


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


    def test_committed_stake_is_independent_of_ambient_decimal_precision(self):
        book = PaperBook("100")
        first = TicketLeg("ambient-1", "m", "a", locked_odds=Decimal("2"))
        second = TicketLeg("ambient-2", "m", "b", locked_odds=Decimal("2"))
        book.open_ticket([first], Decimal("12.34"))
        book.open_ticket([second], Decimal("5.67"))

        with localcontext() as context:
            context.prec = 2
            committed = book.committed_stake

        self.assertEqual(committed, Decimal("18.01"))


    def test_save_fsyncs_parent_directory_after_atomic_replace_when_supported(self):
        book = PaperBook("100")
        destination = Path(self._tmp.name) / "durable-snapshot.json"
        events: list[str] = []
        descriptors = iter((4001, 4242))

        def canonical_open(path: object, flags: int) -> int:
            return next(descriptors)

        def canonical_fsync(descriptor: int) -> None:
            events.append("directory-fsync" if descriptor == 4242 else "file-fsync")

        def canonical_replace(source: object, target: object) -> None:
            events.append("replace")
            os.replace(source, target)

        with (
            patch.object(paper_module, "_CANONICAL_OS_OPEN", side_effect=canonical_open) as open_mock,
            patch.object(paper_module, "_CANONICAL_OS_CLOSE") as close_mock,
            patch.object(paper_module, "_CANONICAL_OS_FSYNC", side_effect=canonical_fsync),
            patch.object(paper_module, "_CANONICAL_OS_REPLACE", side_effect=canonical_replace),
            patch.object(paper_module, "_CANONICAL_OS_NAME", "posix"),
            patch.object(paper_module, "_CANONICAL_OS_DIRECTORY", 0x10000),
        ):
            book.save(destination)

        self.assertIn("file-fsync", events)
        self.assertIn("replace", events)
        self.assertIn("directory-fsync", events)
        self.assertLess(events.index("file-fsync"), events.index("replace"))
        self.assertLess(events.index("replace"), events.index("directory-fsync"))
        self.assertEqual(open_mock.call_count, 2)
        open_mock.assert_any_call(destination, os.O_RDONLY)
        open_mock.assert_any_call(destination.parent, os.O_RDONLY | 0x10000)
        self.assertEqual(close_mock.call_count, 2)


    def test_save_surfaces_directory_fsync_failure_after_atomic_replace(self):
        book = PaperBook("100")
        destination = Path(self._tmp.name) / "directory-fsync-failure.json"
        descriptors = iter((4001, 4242))

        def canonical_open(path: object, flags: int) -> int:
            return next(descriptors)

        def canonical_fsync(descriptor: int) -> None:
            if descriptor == 4242:
                raise OSError("directory fsync failed")

        with (
            patch.object(paper_module, "_CANONICAL_OS_OPEN", side_effect=canonical_open),
            patch.object(paper_module, "_CANONICAL_OS_CLOSE"),
            patch.object(paper_module, "_CANONICAL_OS_FSYNC", side_effect=canonical_fsync),
            patch.object(paper_module, "_CANONICAL_OS_NAME", "posix"),
            patch.object(paper_module, "_CANONICAL_OS_DIRECTORY", 0x10000),
        ):
            with self.assertRaisesRegex(OSError, "directory fsync failed"):
                book.save(destination)

        self.assertTrue(destination.exists())
        restored = PaperBook.load(destination)
        self.assertEqual(restored.balance, Decimal("100"))


    def test_save_fsyncs_each_new_parent_entry_before_snapshot_replace(self):
        book = PaperBook("100")
        root = Path(self._tmp.name)
        destination = root / "new-parent" / "nested" / "snapshot.json"
        events: list[tuple[str, Path | None]] = []
        original_replace = paper_module._CANONICAL_OS_REPLACE

        def record_directory_fsync(directory: Path) -> None:
            events.append(("directory-fsync", directory))

        def record_replace(source: object, target: object) -> None:
            events.append(("replace", None))
            original_replace(source, target)

        with (
            patch.object(
                paper_module,
                "_CANONICAL_FSYNC_SNAPSHOT_DIRECTORY",
                side_effect=record_directory_fsync,
            ),
            patch.object(
                paper_module,
                "_CANONICAL_OS_REPLACE",
                side_effect=record_replace,
            ),
        ):
            book.save(destination)

        replace_index = events.index(("replace", None))
        pre_replace = events[:replace_index]
        self.assertIn(("directory-fsync", root), pre_replace)
        self.assertIn(("directory-fsync", root / "new-parent"), pre_replace)
        self.assertTrue(destination.exists())


    def test_save_aborts_before_snapshot_creation_when_parent_publication_fsync_fails(self):
        book = PaperBook("100")
        root = Path(self._tmp.name)
        destination = root / "new-parent-failure" / "nested" / "snapshot.json"
        calls: list[Path] = []

        def fail_first_parent_fsync(directory: Path) -> None:
            calls.append(directory)
            raise OSError("parent directory fsync failed")

        with patch.object(
            paper_module,
            "_CANONICAL_FSYNC_SNAPSHOT_DIRECTORY",
            side_effect=fail_first_parent_fsync,
        ):
            with self.assertRaisesRegex(OSError, "parent directory fsync failed"):
                book.save(destination)

        self.assertEqual(calls, [root])
        self.assertFalse(destination.exists())
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})


    def test_save_does_not_create_parent_for_rejected_snapshot_candidate(self):
        book = PaperBook("100")
        destination = Path(self._tmp.name) / "rejected-parent" / "nested" / "snapshot.json"

        with patch.object(
            paper_module,
            "_CANONICAL_FROM_RAW_SNAPSHOT",
            side_effect=ValueError("candidate rejected"),
        ):
            with self.assertRaisesRegex(ValueError, "candidate rejected"):
                book.save(destination)

        self.assertFalse(destination.parent.exists())
        self.assertFalse(destination.exists())

    def test_schema8_load_rejects_unexpected_root_field(self):
        book = PaperBook("100")
        leg = TicketLeg("e-root-extra", "m-root-extra", "s-root-extra", locked_odds=Decimal("2"))
        book.open_ticket([leg], "10")
        path = Path(self._tmp.name) / "paper_book.json"
        book.save(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["future_root_semantics"] = {"authority": "smuggled"}
        path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "schema 8 root contains unexpected fields"):
            PaperBook.load_bytes(path.read_bytes())

    def test_schema8_load_rejects_unexpected_ticket_field(self):
        book = PaperBook("100")
        leg = TicketLeg("e-ticket-extra", "m-ticket-extra", "s-ticket-extra", locked_odds=Decimal("2"))
        book.open_ticket([leg], "10")
        path = Path(self._tmp.name) / "paper_book.json"
        book.save(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["tickets"][0]["future_ticket_semantics"] = "smuggled"
        path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "schema 8 ticket contains unexpected fields"):
            PaperBook.load_bytes(path.read_bytes())

    def test_schema6_cannot_launder_schema7_exchange_side_field(self):
        book = PaperBook("100")
        leg = TicketLeg(
            "e-schema-downgrade",
            "m-schema-downgrade",
            "s-schema-downgrade",
            locked_odds=Decimal("2"),
            sport="tennis",
        )
        book.open_ticket([leg], "10")
        path = Path(self._tmp.name) / "paper_book.json"
        book.save(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["schema_version"] = 6
        raw["tickets"][0]["legs"][0].pop("market_semantics_id")
        path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "schema 6 ticket leg contains unexpected fields"):
            PaperBook.load_bytes(path.read_bytes())

    def test_schema8_load_rejects_unexpected_leg_field(self):
        book = PaperBook("100")
        leg = TicketLeg("e-leg-extra", "m-leg-extra", "s-leg-extra", locked_odds=Decimal("2"))
        book.open_ticket([leg], "10")
        path = Path(self._tmp.name) / "paper_book.json"
        book.save(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["tickets"][0]["legs"][0]["future_leg_semantics"] = "smuggled"
        path.write_text(json.dumps(raw), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "schema 8 ticket leg contains unexpected fields"):
            PaperBook.load_bytes(path.read_bytes())

    def test_settlement_rejects_inexact_decimal_payout_before_mutation(self):
        book = PaperBook("100")
        leg = TicketLeg(
            "event-inexact-settlement",
            "market-inexact-settlement",
            "selection-inexact-settlement",
            locked_odds=Decimal("1.12345678901234567890123456789"),
        )
        ticket = book.open_ticket([leg], "1")
        balance_before = book.balance
        lifecycle_before = tuple(book._lifecycle)

        with self.assertRaisesRegex(ValueError, "loses Decimal precision"):
            book.settle(ticket.ticket_id, {leg.quote_key})

        self.assertEqual(book.balance, balance_before)
        self.assertEqual(tuple(book._lifecycle), lifecycle_before)
        self.assertEqual(ticket.status.value, "open")
        self.assertEqual(ticket.payout, Decimal("0"))

if __name__ == "__main__":
    unittest.main()
