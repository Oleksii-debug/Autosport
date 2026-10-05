from decimal import Decimal
import threading
import time

import pytest

import autosport.paper as paper_module
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-10-05T00:00:00+00:00"


def _leg(*, odds: str = "2.00", sport: str = "soccer") -> TicketLeg:
    return TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal(odds),
        sport=sport,
        exchange_side="back",
    )


def test_in_place_locked_odds_mutation_cannot_move_opening_authority() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    leg = ticket.legs[0]
    original_quote_key = leg.quote_key
    object.__setattr__(leg, "locked_odds", Decimal("100"))

    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)
    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        book.settle(ticket.ticket_id, {original_quote_key}, settled_at=_TS)

    assert book.balance == balance_before
    assert tuple(book._lifecycle) == lifecycle_before
    assert ticket.status.value == "open"
    assert ticket.payout == Decimal("0")


def test_in_place_leg_mutation_cannot_replace_durable_snapshot(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    book.save(path)
    durable_before = path.read_bytes()

    object.__setattr__(ticket.legs[0], "locked_odds", Decimal("100"))

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        book.save(path)

    assert path.read_bytes() == durable_before


def test_trusted_restart_installs_detached_leg_opening_authority(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    book.save(path)

    restored = PaperBook.load(path)
    ticket = next(iter(restored.tickets.values()))
    object.__setattr__(ticket.legs[0], "sport", "tennis")

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        _ = restored.committed_stake


def test_unchanged_leg_remains_authorized_after_trusted_restart(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    book.save(path)

    restored = PaperBook.load(path)

    assert restored.committed_stake == Decimal("10")


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("event_id", "event-2"),
        ("market_id", "market-2"),
        ("selection_id", "selection-2"),
        ("locked_odds", Decimal("3.00")),
        ("sport", "tennis"),
        ("exchange_side", None),
    ],
)
def test_each_canonical_leg_identity_mutation_is_rejected(
    field: str,
    replacement: object,
) -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    object.__setattr__(ticket.legs[0], field, replacement)

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        _ = book.committed_stake


def test_leg_tuple_reordering_cannot_move_opening_authority() -> None:
    book = PaperBook("100")
    first = _leg()
    second = TicketLeg(
        "event-2",
        "market-2",
        "selection-2",
        Decimal("2.50"),
        sport="tennis",
        exchange_side="back",
    )
    ticket = book.open_ticket([first, second], "10", placed_at=_TS)
    ticket.legs = tuple(reversed(ticket.legs))

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        _ = book.committed_stake


def test_leg_tuple_replacement_with_equal_quote_but_changed_odds_is_rejected() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    original = ticket.legs[0]
    ticket.legs = (
        TicketLeg(
            original.event_id,
            original.market_id,
            original.selection_id,
            Decimal("2.25"),
            sport=original.sport,
            exchange_side=original.exchange_side,
        ),
    )

    assert ticket.legs[0].quote_key == original.quote_key
    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        _ = book.committed_stake


class _HostileComparableString(str):
    comparisons = 0

    def __eq__(self, _other):
        type(self).comparisons += 1
        raise AssertionError("hostile string comparison must never execute")

    __hash__ = str.__hash__


class _HostileLifecycleAction(str):
    comparisons = 0

    def __eq__(self, _other):
        type(self).comparisons += 1
        raise AssertionError("hostile lifecycle comparison must never execute")

    __hash__ = str.__hash__


class _HostileSettlementKey(str):
    hash_calls = 0

    def __hash__(self):
        type(self).hash_calls += 1
        return super().__hash__()


@pytest.mark.parametrize(
    "operation",
    ("committed_stake", "open_ticket", "settle", "save"),
)
def test_public_operations_validate_hostile_ticket_state_before_authority_comparison(
    operation: str,
    tmp_path,
) -> None:
    book = PaperBook("100")
    leg = _leg()
    ticket = book.open_ticket([leg], "10", reason="safe", placed_at=_TS)
    balance_before = book.balance
    _HostileComparableString.comparisons = 0
    ticket.strategy_reason = _HostileComparableString("safe")

    with pytest.raises(ValueError, match="strategy_reason must be a string"):
        if operation == "committed_stake":
            _ = book.committed_stake
        elif operation == "open_ticket":
            book.open_ticket([_leg(odds="2.50")], "1", placed_at=_TS)
        elif operation == "settle":
            book.settle(ticket.ticket_id, {leg.quote_key}, settled_at=_TS)
        else:
            book.save(tmp_path / "hostile-authority.json")

    assert _HostileComparableString.comparisons == 0
    assert book.balance == balance_before
    assert book.tickets[ticket.ticket_id].status.value == "open"


def test_lifecycle_action_subclass_rejected_before_membership_or_authority_comparison() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    _HostileLifecycleAction.comparisons = 0
    book._lifecycle[0] = (
        _HostileLifecycleAction("open"),
        ticket.ticket_id,
        (),
        (),
    )

    with pytest.raises(ValueError, match="canonical open or settle text"):
        _ = book.committed_stake

    assert _HostileLifecycleAction.comparisons == 0


def test_settlement_witness_key_rejected_before_rehash() -> None:
    book = PaperBook("100")
    hostile = _HostileSettlementKey("ticket-hostile")
    book._settlement_times[hostile] = None
    _HostileSettlementKey.hash_calls = 0

    with pytest.raises(ValueError, match="keys must be canonical strings"):
        _ = book.committed_stake

    assert _HostileSettlementKey.hash_calls == 0


def test_opening_registry_ignores_rebound_commitment_module_dispatch(monkeypatch) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_called = False

    def hostile_commitment(_ticket):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound opening commitment executed")

    monkeypatch.setattr(paper_module, "_ticket_opening_commitment", hostile_commitment)

    assert book.committed_stake == Decimal("10")
    assert attacker_called is False


def test_opening_registry_rejects_in_place_commitment_code_mutation_before_execution() -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    commitment = paper_module._ticket_opening_commitment
    original_code = commitment.__code__
    attacker_called = False

    def hostile_commitment(_ticket):
        nonlocal attacker_called
        attacker_called = True
        return ()

    try:
        commitment.__code__ = hostile_commitment.__code__
        with pytest.raises(
            ValueError,
            match="opening commitment authority changed",
        ):
            _ = book.committed_stake
    finally:
        commitment.__code__ = original_code

    assert attacker_called is False
    assert book.balance == Decimal("90")


@pytest.mark.parametrize("method_name", ["__hash__", "__eq__"])
def test_hidden_registries_reject_rebound_paperbook_key_methods_before_execution(
    monkeypatch, method_name: str,
) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    if method_name == "__hash__":
        def hostile(self):
            nonlocal attacker_calls
            attacker_calls += 1
            raise AssertionError("rebound PaperBook hash executed")
    else:
        def hostile(self, other):
            nonlocal attacker_calls
            attacker_calls += 1
            raise AssertionError("rebound PaperBook equality executed")

    monkeypatch.setattr(PaperBook, method_name, hostile)

    with pytest.raises(ValueError, match="registry key authority changed"):
        _ = book.committed_stake

    assert attacker_calls == 0
    assert book.balance == Decimal("90")


def test_hidden_registries_ignore_rebound_registry_key_validator(monkeypatch) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile_validator(_book):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound registry key validator executed")

    monkeypatch.setattr(
        paper_module,
        "_require_registry_book_key_authority",
        hostile_validator,
    )

    assert book.committed_stake == Decimal("10")
    assert attacker_calls == 0


def test_hidden_registries_reject_in_place_registry_key_validator_code_mutation() -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    validator = paper_module._require_registry_book_key_authority
    original_code = validator.__code__
    attacker_calls = 0

    def hostile_validator(_book):
        nonlocal attacker_calls
        attacker_calls += 1
        return None

    try:
        validator.__code__ = hostile_validator.__code__
        with pytest.raises(
            ValueError,
            match="registry key validator authority changed",
        ):
            _ = book.committed_stake
    finally:
        validator.__code__ = original_code

    assert attacker_calls == 0
    assert book.balance == Decimal("90")


def test_settlement_resolution_keys_reject_str_subclass_before_internal_hashing() -> None:
    class HostileResolutionKey(str):
        hash_calls = 0

        def __hash__(self) -> int:
            type(self).hash_calls += 1
            return super().__hash__()

    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    hostile = HostileResolutionKey(ticket.legs[0].quote_key)
    HostileResolutionKey.hash_calls = 0

    with pytest.raises(
        ValueError,
        match="winning_quote_keys must contain non-empty string quote keys",
    ):
        book.settle(ticket.ticket_id, [hostile], settled_at=_TS)

    assert HostileResolutionKey.hash_calls == 0
    assert ticket.status.value == "open"
    assert ticket.payout == Decimal("0")


@pytest.mark.parametrize(
    "operation",
    ("committed_stake", "open_ticket", "settle", "save"),
)
def test_public_operations_ignore_rebound_opening_authority_dispatch(
    monkeypatch,
    operation: str,
    tmp_path,
) -> None:
    book = PaperBook("100")
    leg = _leg()
    ticket = book.open_ticket([leg], "10", placed_at=_TS)
    object.__setattr__(leg, "locked_odds", Decimal("3.00"))

    monkeypatch.setattr(
        paper_module,
        "_require_ticket_opening_authority",
        lambda _book: None,
    )

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        if operation == "committed_stake":
            _ = book.committed_stake
        elif operation == "open_ticket":
            book.open_ticket([_leg(odds="2.50")], "1", placed_at=_TS)
        elif operation == "settle":
            book.settle(ticket.ticket_id, {leg.quote_key}, settled_at=_TS)
        else:
            book.save(tmp_path / "rebound-opening-authority.json")


def test_public_operation_rejects_in_place_opening_authority_code_mutation_before_execution() -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    authority = paper_module._require_ticket_opening_authority
    original_code = authority.__code__
    attacker_calls = 0

    def hostile_authority(_book):
        nonlocal attacker_calls
        attacker_calls += 1
        return None

    try:
        authority.__code__ = hostile_authority.__code__
        with pytest.raises(
            ValueError,
            match="opening authority dispatch changed",
        ):
            _ = book.committed_stake
    finally:
        authority.__code__ = original_code

    assert attacker_calls == 0
    assert book.balance == Decimal("90")


def test_public_operation_ignores_rebound_causal_history_authority_dispatch(
    monkeypatch,
) -> None:
    book = PaperBook("100")
    first = book.open_ticket([_leg()], "10", placed_at=_TS)
    second_leg = TicketLeg(
        "event-2",
        "market-2",
        "selection-2",
        Decimal("2.50"),
        sport="tennis",
        exchange_side="back",
    )
    second = book.open_ticket([second_leg], "10", placed_at=_TS)

    book.tickets = {
        second.ticket_id: second,
        first.ticket_id: first,
    }
    book._lifecycle = [
        ("open", second.ticket_id, (), ()),
        ("open", first.ticket_id, (), ()),
    ]

    # The visible state remains internally replayable; only the hidden
    # product-issued chronology witness distinguishes the coherent rewrite.
    PaperBook._validate_loaded_state(book)

    monkeypatch.setattr(
        paper_module,
        "_require_paperbook_causal_history_authority",
        lambda _book: None,
    )

    with pytest.raises(
        ValueError,
        match="causal history changed outside product-issued transitions",
    ):
        _ = book.committed_stake




def test_public_operation_rejects_in_place_causal_authority_code_mutation_before_execution() -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    authority = paper_module._require_paperbook_causal_history_authority
    original_code = authority.__code__
    attacker_calls = 0

    def hostile_authority(_book):
        nonlocal attacker_calls
        attacker_calls += 1
        return None

    try:
        authority.__code__ = hostile_authority.__code__
        with pytest.raises(
            ValueError,
            match="causal-history authority dispatch changed",
        ):
            _ = book.committed_stake
    finally:
        authority.__code__ = original_code

    assert attacker_calls == 0
    assert book.balance == Decimal("90")



def test_causal_registry_ignores_rebound_snapshot_module_dispatch(monkeypatch) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile_snapshot(_book):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound causal snapshot executed")

    monkeypatch.setattr(
        paper_module,
        "_paperbook_causal_history_snapshot",
        hostile_snapshot,
    )

    assert book.committed_stake == Decimal("10")
    assert attacker_calls == 0


def test_causal_registry_rejects_in_place_snapshot_code_mutation_before_execution() -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    snapshot = paper_module._paperbook_causal_history_snapshot
    original_code = snapshot.__code__
    attacker_calls = 0

    def hostile_snapshot(_book):
        nonlocal attacker_calls
        attacker_calls += 1
        return ((), ())

    try:
        snapshot.__code__ = hostile_snapshot.__code__
        with pytest.raises(
            ValueError,
            match="causal-history snapshot authority changed",
        ):
            _ = book.committed_stake
    finally:
        snapshot.__code__ = original_code

    assert attacker_calls == 0
    assert book.balance == Decimal("90")


class _HostileSnapshotText(str):
    comparisons = 0
    hash_calls = 0

    def __eq__(self, _other):
        type(self).comparisons += 1
        raise AssertionError("hostile snapshot text comparison must never execute")

    def __hash__(self):
        type(self).hash_calls += 1
        raise AssertionError("hostile snapshot text hash must never execute")


def test_snapshot_decimal_rejects_str_subclass_before_decimal_parsing() -> None:
    hostile = _HostileSnapshotText("2.00")
    _HostileSnapshotText.comparisons = 0
    _HostileSnapshotText.hash_calls = 0

    with pytest.raises(ValueError, match="non-empty trimmed decimal string"):
        PaperBook._parse_snapshot_decimal(hostile, "locked_odds")

    assert _HostileSnapshotText.comparisons == 0
    assert _HostileSnapshotText.hash_calls == 0


def test_snapshot_status_rejects_str_subclass_before_enum_lookup() -> None:
    hostile = _HostileSnapshotText("open")
    _HostileSnapshotText.comparisons = 0
    _HostileSnapshotText.hash_calls = 0

    with pytest.raises(ValueError, match="canonical string"):
        PaperBook._parse_snapshot_status(hostile, "ticket-1")

    assert _HostileSnapshotText.comparisons == 0
    assert _HostileSnapshotText.hash_calls == 0


def test_snapshot_lifecycle_rejects_action_str_subclass_before_comparison() -> None:
    hostile = _HostileSnapshotText("open")
    raw = [{"action": hostile, "ticket_id": "ticket-1"}]
    _HostileSnapshotText.comparisons = 0
    _HostileSnapshotText.hash_calls = 0

    with pytest.raises(ValueError, match="lifecycle action must be canonical text"):
        PaperBook._parse_lifecycle(raw, 7)

    assert _HostileSnapshotText.comparisons == 0
    assert _HostileSnapshotText.hash_calls == 0



class _HostileSnapshotBytes(bytes):
    decode_calls = 0

    def decode(self, *args, **kwargs):
        type(self).decode_calls += 1
        raise AssertionError("hostile bytes decode must never execute")


def test_load_bytes_rejects_bytes_subclass_before_decode() -> None:
    hostile = _HostileSnapshotBytes(b"{}")
    _HostileSnapshotBytes.decode_calls = 0

    with pytest.raises(TypeError, match="canonical bytes"):
        PaperBook.load_bytes(hostile)

    assert _HostileSnapshotBytes.decode_calls == 0



def test_settle_rejects_ticket_id_str_subclass_before_mapping_hash() -> None:
    class HostileTicketId(str):
        hash_calls = 0

        def __hash__(self):
            type(self).hash_calls += 1
            raise AssertionError("hostile ticket_id hash must never execute")

    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    hostile = HostileTicketId(ticket.ticket_id)
    HostileTicketId.hash_calls = 0

    with pytest.raises(ValueError, match="settlement ticket_id must be a string"):
        book.settle(hostile, {ticket.legs[0].quote_key}, settled_at=_TS)

    assert HostileTicketId.hash_calls == 0
    assert ticket.status.value == "open"
    assert book.balance == Decimal("90")



class _HostileDecimalInput:
    str_calls = 0

    def __str__(self):
        type(self).str_calls += 1
        raise AssertionError("hostile monetary __str__ must never execute")


class _HostileLegIterable:
    iter_calls = 0

    def __iter__(self):
        type(self).iter_calls += 1
        raise AssertionError("hostile leg iterator must never execute")


def test_initial_bankroll_rejects_non_builtin_before_string_conversion() -> None:
    _HostileDecimalInput.str_calls = 0

    with pytest.raises(ValueError, match="exact built-in Decimal"):
        PaperBook(_HostileDecimalInput())

    assert _HostileDecimalInput.str_calls == 0


def test_open_ticket_rejects_non_builtin_stake_before_string_conversion() -> None:
    book = PaperBook("100")
    _HostileDecimalInput.str_calls = 0

    with pytest.raises(ValueError, match="exact built-in Decimal"):
        book.open_ticket([_leg()], _HostileDecimalInput(), placed_at=_TS)

    assert _HostileDecimalInput.str_calls == 0
    assert book.balance == Decimal("100")
    assert book.tickets == {}


def test_decimal_text_size_limit_is_fail_closed() -> None:
    oversized = "1" * 513
    with pytest.raises(ValueError, match="decimal text exceeds the canonical size limit"):
        PaperBook(oversized)

    book = PaperBook("100")
    with pytest.raises(ValueError, match="decimal text exceeds the canonical size limit"):
        book.open_ticket([_leg()], oversized, placed_at=_TS)

    assert book.balance == Decimal("100")
    assert book.tickets == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("100.25"), Decimal("100.25")),
        ("100.25", Decimal("100.25")),
        (100, Decimal("100")),
        (100.25, Decimal("100.25")),
    ],
)
def test_canonical_builtin_monetary_inputs_remain_supported(value, expected) -> None:
    book = PaperBook(value)
    assert book.initial_bankroll == expected


def test_open_ticket_rejects_hostile_leg_iterable_before_iteration() -> None:
    book = PaperBook("100")
    _HostileLegIterable.iter_calls = 0

    with pytest.raises(ValueError, match="exact list or tuple"):
        book.open_ticket(_HostileLegIterable(), "10", placed_at=_TS)

    assert _HostileLegIterable.iter_calls == 0
    assert book.balance == Decimal("100")
    assert book.tickets == {}


def test_concurrent_open_ticket_cannot_overcommit_single_bankroll(monkeypatch) -> None:
    book = PaperBook("100")
    start = threading.Barrier(3)
    active_guard = threading.Lock()
    active_debits = 0
    max_active_debits = 0
    successes: list[str] = []
    failures: list[BaseException] = []
    original_debit = PaperBook._debit_balance.__func__

    def slow_debit(cls, balance: Decimal, amount: Decimal) -> Decimal:
        nonlocal active_debits, max_active_debits
        with active_guard:
            active_debits += 1
            max_active_debits = max(max_active_debits, active_debits)
        try:
            time.sleep(0.05)
            return original_debit(cls, balance, amount)
        finally:
            with active_guard:
                active_debits -= 1

    monkeypatch.setattr(PaperBook, "_debit_balance", classmethod(slow_debit))

    def worker(index: int) -> None:
        leg = TicketLeg(
            f"event-concurrent-{index}",
            "market-concurrent-overcommit",
            f"selection-{index}",
            Decimal("2"),
            sport="soccer",
            exchange_side="back",
        )
        start.wait()
        try:
            ticket = book.open_ticket([leg], "60", placed_at=_TS)
        except BaseException as exc:
            failures.append(exc)
        else:
            successes.append(ticket.ticket_id)

    threads = [
        threading.Thread(target=worker, args=(1,)),
        threading.Thread(target=worker, args=(2,)),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert max_active_debits == 1
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert "insufficient virtual bankroll" in str(failures[0])
    assert book.balance == Decimal("40")
    assert len(book.tickets) == 1
    assert book.committed_stake == Decimal("60")


def test_inexact_settlement_fails_before_economic_mutation() -> None:
    book = PaperBook("100")
    leg = TicketLeg(
        "event-inexact",
        "market-inexact",
        "selection-inexact",
        Decimal("1.12345678901234567890123456789"),
        sport="soccer",
        exchange_side="back",
    )
    ticket = book.open_ticket([leg], "1", placed_at=_TS)
    before_balance = book.balance
    before_lifecycle = tuple(book._lifecycle)

    with pytest.raises(ValueError, match="loses Decimal precision"):
        book.settle(ticket.ticket_id, {leg.quote_key}, settled_at=_TS)

    assert book.balance == before_balance
    assert tuple(book._lifecycle) == before_lifecycle
    assert ticket.status.value == "open"
    assert ticket.payout == Decimal("0")


def test_operation_lock_dispatch_ignores_module_rebinding(monkeypatch) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    def hostile_lock(_book):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound lock authority executed")

    monkeypatch.setattr(paper_module, "_require_paperbook_operation_lock", hostile_lock)

    assert book.committed_stake == Decimal("0")
    assert attacker_calls == 0


def test_operation_lock_dispatch_rejects_in_place_code_mutation() -> None:
    book = PaperBook("100")
    authority = paper_module._require_paperbook_operation_lock
    original_code = authority.__code__
    attacker_calls = 0

    def hostile_lock(_book):
        nonlocal attacker_calls
        attacker_calls += 1
        return threading.RLock()

    try:
        authority.__code__ = hostile_lock.__code__
        with pytest.raises(ValueError, match="operation lock authority changed"):
            _ = book.committed_stake
    finally:
        authority.__code__ = original_code

    assert attacker_calls == 0
