import json
from decimal import Decimal
import threading
import time
from pathlib import Path

import pytest

import autosport.paper as paper_module
import autosport.domain as domain_module
from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-10-05T00:00:00+00:00"


def _hostile_function_with_freevars(count: int):
    names = [f"captured_{index}" for index in range(count)]
    args = ", ".join(names)
    references = "\n".join(f"        _ = {name}" for name in names)
    if references:
        references += "\n"
    source = (
        f"def factory({args}):\n"
        "    def hostile(_value):\n"
        f"{references}"
        "        raise AssertionError('mutated authority code executed')\n"
        "    return hostile\n"
    )
    namespace: dict[str, object] = {}
    exec(source, namespace)
    return namespace["factory"](*([None] * count))


def _leg(*, odds: str = "2.00", sport: str = "soccer") -> TicketLeg:
    return TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal(odds),
        sport=sport,
        exchange_side="back",
    )


def _lay_leg(
    *,
    odds: str = "3.00",
    semantics: str = "exchange.match.odds",
) -> TicketLeg:
    return TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal(odds),
        sport="soccer",
        exchange_side="lay",
        market_semantics_id=semantics,
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
        ("market_semantics_id", "exchange.match.odds.v2"),
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


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("stake", Decimal("11")),
        ("placed_at", "2026-10-05T00:01:00+00:00"),
        ("strategy_reason", "tampered"),
        ("provider_source_ids", ("another-provider",)),
        ("provider_accounts", (("betfair", "another-account"),)),
        ("bankroll_id", "another-bankroll"),
        ("currency", "EUR"),
    ),
)
def test_each_ticket_opening_economic_identity_mutation_is_rejected(
    field: str,
    replacement: object,
) -> None:
    book = PaperBook("100")
    ticket = book.open_ticket(
        [_leg()],
        "10",
        reason="safe",
        placed_at=_TS,
        provider_source_ids=("betfair",),
        provider_accounts=(("betfair", "account-1"),),
        bankroll_id="paper-bankroll",
        currency="USD",
    )

    expected_balance = book.balance
    if field == "stake":
        # Keep visible lifecycle replay internally coherent so this case reaches
        # the hidden product-issued opening witness instead of failing early.
        object.__setattr__(ticket, field, replacement)
        object.__setattr__(book, "balance", Decimal("89"))
        expected_balance = Decimal("89")
    elif field == "provider_source_ids":
        object.__setattr__(ticket, field, replacement)
        object.__setattr__(
            ticket,
            "provider_accounts",
            (("another-provider", "account-2"),),
        )
    else:
        object.__setattr__(ticket, field, replacement)

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        _ = book.committed_stake

    assert book.balance == expected_balance
    assert ticket.status.value == "open"



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


def test_opening_registry_ignores_ticket_mapping_descriptor_rebinding(monkeypatch) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)

    class HostileDescriptor:
        def __get__(self, instance, owner):
            raise AssertionError("rebound PaperBook.tickets descriptor executed")

        def __set__(self, instance, value):
            raise AssertionError("rebound PaperBook.tickets setter executed")

    monkeypatch.setattr(PaperBook, "tickets", HostileDescriptor(), raising=False)

    paper_module._require_ticket_opening_authority(book)


def test_opening_commitment_ignores_rebound_slot_descriptors(monkeypatch) -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    expected = paper_module._ticket_opening_commitment(ticket)

    class HostileDescriptor:
        def __get__(self, instance, owner):
            raise AssertionError("rebound opening slot descriptor executed")

        def __set__(self, instance, value):
            raise AssertionError("rebound opening slot descriptor setter executed")

    monkeypatch.setattr(PaperBook, "tickets", HostileDescriptor(), raising=False)
    monkeypatch.setattr(paper_module.PaperTicket, "stake", HostileDescriptor(), raising=False)
    monkeypatch.setattr(TicketLeg, "locked_odds", HostileDescriptor(), raising=False)

    assert paper_module._ticket_opening_commitment(ticket) == expected
    paper_module._require_ticket_opening_authority(book)


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


def test_opening_registry_rejects_commitment_default_mutation_before_execution() -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)

    commitment = paper_module._ticket_opening_commitment
    original_defaults = commitment.__defaults__
    assert original_defaults is not None

    class HostileDescriptor:
        def __get__(self, _instance, _owner):
            raise AssertionError("mutated opening commitment default executed")

    hostile_fields = (HostileDescriptor(),) + tuple(original_defaults[1][1:])
    commitment.__defaults__ = (
        original_defaults[0],
        hostile_fields,
        original_defaults[2],
        original_defaults[3],
    )

    try:
        with pytest.raises(ValueError, match="opening commitment authority changed"):
            _ = book.committed_stake
    finally:
        commitment.__defaults__ = original_defaults

    assert book.balance == Decimal("90")


def test_opening_registry_rejects_in_place_commitment_code_mutation_before_execution() -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    commitment = paper_module._ticket_opening_commitment
    original_code = commitment.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        commitment.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="opening commitment authority changed"):
            _ = book.committed_stake
    finally:
        commitment.__code__ = original_code

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
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        validator.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="registry key validator authority changed"):
            _ = book.committed_stake
    finally:
        validator.__code__ = original_code

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
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="opening authority dispatch changed"):
            _ = book.committed_stake
    finally:
        authority.__code__ = original_code

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
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="causal-history authority dispatch changed"):
            _ = book.committed_stake
    finally:
        authority.__code__ = original_code

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
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        snapshot.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="causal-history snapshot authority changed"):
            _ = book.committed_stake
    finally:
        snapshot.__code__ = original_code

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


def test_operation_lock_dispatch_rejects_outer_closure_retarget() -> None:
    authority = paper_module._require_paperbook_operation_lock
    target_cell = next(
        cell
        for cell in authority.__closure__ or ()
        if type(cell.cell_contents) is dict
    )
    original_value = target_cell.cell_contents

    try:
        target_cell.cell_contents = {}
        book = PaperBook("100")
        with pytest.raises(
            ValueError,
            match=r"operation lock authority closure changed",
        ):
            _ = book.committed_stake
    finally:
        target_cell.cell_contents = original_value


def test_operation_lock_dispatch_rejects_in_place_code_mutation() -> None:
    book = PaperBook("100")
    authority = paper_module._require_paperbook_operation_lock
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="operation lock authority changed"):
            _ = book.committed_stake
    finally:
        authority.__code__ = original_code


@pytest.mark.parametrize(
    "registrar_name",
    (
        "_register_paperbook_operation_lock",
        "_register_ticket_opening_authority_book",
        "_register_paperbook_causal_history_authority_book",
    ),
)
def test_constructor_never_executes_rebound_authority_registrar(
    monkeypatch, registrar_name: str
) -> None:
    attacker_calls = 0

    def hostile(_book):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound constructor authority executed")

    monkeypatch.setattr(paper_module, registrar_name, hostile)

    book = PaperBook("100")

    assert book.balance == Decimal("100")
    assert attacker_calls == 0


@pytest.mark.parametrize(
    ("registrar_name", "label"),
    (
        ("_register_paperbook_operation_lock", "operation lock"),
        ("_register_ticket_opening_authority_book", "opening registry"),
        (
            "_register_paperbook_causal_history_authority_book",
            "causal-history registry",
        ),
    ),
)
def test_constructor_rejects_in_place_registrar_code_mutation(
    registrar_name: str, label: str
) -> None:
    registrar = getattr(paper_module, registrar_name)
    original_code = registrar.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        registrar.__code__ = hostile.__code__
        with pytest.raises(ValueError, match=f"{label} constructor authority changed"):
            PaperBook("100")
    finally:
        registrar.__code__ = original_code


@pytest.mark.parametrize(
    ("registrar_name", "label"),
    (
        ("_register_paperbook_operation_lock", "operation lock"),
        ("_register_ticket_opening_authority_book", "opening registry"),
        (
            "_register_paperbook_causal_history_authority_book",
            "causal-history registry",
        ),
    ),
)
def test_constructor_rejects_registrar_closure_retarget(
    registrar_name: str,
    label: str,
) -> None:
    registrar = getattr(paper_module, registrar_name)
    closure = registrar.__closure__ or ()
    if registrar_name == "_register_paperbook_operation_lock":
        target_cell = next(
            cell for cell in closure if cell.cell_contents is paper_module.ref
        )
    elif registrar_name == "_register_ticket_opening_authority_book":
        target_cell = next(
            cell
            for cell in closure
            if cell.cell_contents is paper_module._ticket_opening_commitment
        )
    else:
        target_cell = next(
            cell
            for cell in closure
            if type(cell.cell_contents).__name__ == "WeakKeyDictionary"
        )
    original_value = target_cell.cell_contents
    try:
        target_cell.cell_contents = object()
        with pytest.raises(
            ValueError,
            match=f"PaperBook {label} constructor authority closure changed",
        ):
            PaperBook("100")
    finally:
        target_cell.cell_contents = original_value


def test_operation_lock_registration_ignores_rebound_weakref_factory(
    monkeypatch,
) -> None:
    attacker_calls = 0

    def hostile_ref(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound weakref factory executed")

    monkeypatch.setattr(paper_module, "ref", hostile_ref)

    book = PaperBook("100")

    assert book.committed_stake == Decimal("0")
    assert attacker_calls == 0


def test_operation_lock_registration_ignores_rebound_threading_module(
    monkeypatch,
) -> None:
    attacker_calls = 0

    class HostileThreading:
        @staticmethod
        def RLock():
            nonlocal attacker_calls
            attacker_calls += 1
            raise AssertionError("rebound lock factory executed")

    monkeypatch.setattr(paper_module, "threading", HostileThreading)

    book = PaperBook("100")

    assert book.committed_stake == Decimal("0")
    assert attacker_calls == 0


def test_operation_lock_registration_rejects_in_place_lock_factory_mutation() -> None:
    factory = paper_module.threading.RLock
    original_code = factory.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        factory.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="operation lock factory authority changed"):
            PaperBook("100")
    finally:
        factory.__code__ = original_code


@pytest.mark.parametrize(
    "guarded_callable",
    (
        PaperBook.__init__,
        PaperBook.open_ticket,
        PaperBook.settle,
        PaperBook.save,
        PaperBook.committed_stake.fget,
    ),
)
def test_public_authority_wrappers_do_not_expose_unwrapped_bypass(
    guarded_callable,
) -> None:
    assert not hasattr(guarded_callable, "__wrapped__")


@pytest.mark.parametrize(
    "operation",
    ("committed_stake", "open_ticket", "settle", "save"),
)
def test_public_operations_never_execute_rebound_opening_authority(
    monkeypatch, operation: str, tmp_path
) -> None:
    book = PaperBook("100")
    leg = _leg()
    ticket = book.open_ticket([leg], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(_book):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound opening authority executed")

    monkeypatch.setattr(paper_module, "_require_ticket_opening_authority", hostile)

    if operation == "committed_stake":
        assert book.committed_stake == Decimal("10")
    elif operation == "open_ticket":
        book.open_ticket([_leg(odds="2.50")], "1", placed_at=_TS)
    elif operation == "settle":
        book.settle(ticket.ticket_id, {leg.quote_key}, settled_at=_TS)
    else:
        book.save(tmp_path / "rebound-opening-never-runs.json")

    assert attacker_calls == 0


@pytest.mark.parametrize(
    "operation",
    ("committed_stake", "open_ticket", "settle", "save"),
)
def test_public_operations_never_execute_rebound_causal_authority(
    monkeypatch, operation: str, tmp_path
) -> None:
    book = PaperBook("100")
    leg = _leg()
    ticket = book.open_ticket([leg], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(_book):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound causal authority executed")

    monkeypatch.setattr(
        paper_module, "_require_paperbook_causal_history_authority", hostile
    )

    if operation == "committed_stake":
        assert book.committed_stake == Decimal("10")
    elif operation == "open_ticket":
        book.open_ticket([_leg(odds="2.50")], "1", placed_at=_TS)
    elif operation == "settle":
        book.settle(ticket.ticket_id, {leg.quote_key}, settled_at=_TS)
    else:
        book.save(tmp_path / "rebound-causal-never-runs.json")

    assert attacker_calls == 0



class _HostileResolutionIterable:
    iter_calls = 0

    def __iter__(self):
        type(self).iter_calls += 1
        raise AssertionError("hostile settlement iterable executed")


def test_settlement_rejects_nonbuiltin_collection_before_iteration() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    _HostileResolutionIterable.iter_calls = 0

    with pytest.raises(ValueError, match="exact built-in collection"):
        book.settle(ticket.ticket_id, _HostileResolutionIterable(), settled_at=_TS)

    assert _HostileResolutionIterable.iter_calls == 0
    assert ticket.status.value == "open"
    assert book.balance == Decimal("90")


class _HostilePathSubclass(type(Path("."))):
    fspath_calls = 0

    def __fspath__(self):
        type(self).fspath_calls += 1
        raise AssertionError("hostile path coercion executed")


def test_save_rejects_path_subclass_before_fspath(tmp_path) -> None:
    book = PaperBook("100")
    hostile = _HostilePathSubclass(tmp_path / "snapshot.json")
    _HostilePathSubclass.fspath_calls = 0

    with pytest.raises(TypeError, match="exact str or exact Path"):
        book.save(hostile)

    assert _HostilePathSubclass.fspath_calls == 0


def test_load_rejects_path_subclass_before_fspath(tmp_path) -> None:
    hostile = _HostilePathSubclass(tmp_path / "snapshot.json")
    _HostilePathSubclass.fspath_calls = 0

    with pytest.raises(TypeError, match="exact str or exact Path"):
        PaperBook.load(hostile)

    assert _HostilePathSubclass.fspath_calls == 0


def test_rejected_snapshot_candidate_does_not_create_parent_directories(
    monkeypatch, tmp_path
) -> None:
    book = PaperBook("100")
    destination = tmp_path / "rejected-parent" / "nested" / "snapshot.json"

    def reject_candidate(cls, _raw):
        raise ValueError("candidate rejected")

    monkeypatch.setattr(PaperBook, "_from_raw_snapshot", classmethod(reject_candidate))

    with pytest.raises(ValueError, match="candidate rejected"):
        book.save(destination)

    assert not destination.parent.exists()
    assert not destination.exists()


def test_successful_save_fsyncs_destination_directory(monkeypatch, tmp_path) -> None:
    book = PaperBook("100")
    destination = tmp_path / "snapshot.json"
    calls: list[Path] = []
    original = PaperBook._fsync_snapshot_directory

    def record(directory: Path) -> None:
        calls.append(directory)

    monkeypatch.setattr(PaperBook, "_fsync_snapshot_directory", staticmethod(record))
    book.save(destination)

    assert destination.exists()
    assert calls == [tmp_path]
    monkeypatch.setattr(PaperBook, "_fsync_snapshot_directory", original)



def _saved_snapshot_payload(tmp_path):
    path = tmp_path / "schema-payload.json"
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    book.save(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 7
    for leg in payload["tickets"][0]["legs"]:
        leg.pop("market_semantics_id", None)
    return payload


def test_schema7_rejects_unknown_root_field(tmp_path) -> None:
    payload = _saved_snapshot_payload(tmp_path)
    payload["future_root_semantics"] = {"authority": "smuggled"}

    with pytest.raises(ValueError, match="schema 7 root contains unexpected fields"):
        PaperBook.load_bytes(json.dumps(payload).encode("utf-8"))


def test_schema7_rejects_unknown_ticket_field(tmp_path) -> None:
    payload = _saved_snapshot_payload(tmp_path)
    payload["tickets"][0]["future_ticket_semantics"] = "smuggled"

    with pytest.raises(ValueError, match="schema 7 ticket contains unexpected fields"):
        PaperBook.load_bytes(json.dumps(payload).encode("utf-8"))


def test_schema7_rejects_unknown_leg_field(tmp_path) -> None:
    payload = _saved_snapshot_payload(tmp_path)
    payload["tickets"][0]["legs"][0]["future_leg_semantics"] = "smuggled"

    with pytest.raises(
        ValueError, match="schema 7 ticket leg contains unexpected fields"
    ):
        PaperBook.load_bytes(json.dumps(payload).encode("utf-8"))


def test_schema6_cannot_launder_schema7_exchange_side(tmp_path) -> None:
    payload = _saved_snapshot_payload(tmp_path)
    payload["schema_version"] = 6

    with pytest.raises(
        ValueError, match="schema 6 ticket leg contains unexpected fields"
    ):
        PaperBook.load_bytes(json.dumps(payload).encode("utf-8"))


def test_legacy_snapshot_cannot_smuggle_lifecycle_semantics(tmp_path) -> None:
    payload = _saved_snapshot_payload(tmp_path)
    payload.pop("schema_version")

    with pytest.raises(ValueError, match="legacy root contains unexpected fields"):
        PaperBook.load_bytes(json.dumps(payload).encode("utf-8"))


def test_schema2_cannot_smuggle_provider_semantics(tmp_path) -> None:
    payload = _saved_snapshot_payload(tmp_path)
    payload["schema_version"] = 2
    payload.pop("lifecycle")
    ticket = payload["tickets"][0]
    ticket.pop("provider_accounts")
    ticket.pop("settled_at")
    ticket["legs"][0].pop("sport")
    ticket["legs"][0].pop("exchange_side")

    with pytest.raises(ValueError, match="schema 2 ticket contains unexpected fields"):
        PaperBook.load_bytes(json.dumps(payload).encode("utf-8"))


def test_snapshot_decimal_text_size_limit_applies_to_serialized_ingress() -> None:
    oversized = "1" * 513
    with pytest.raises(ValueError, match="decimal text exceeds the canonical size limit"):
        PaperBook._parse_snapshot_decimal(oversized, "stake")


def test_open_ticket_never_executes_rebound_ticket_id_factory(monkeypatch) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    def hostile_uuid4():
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound ticket id factory executed")

    monkeypatch.setattr(paper_module.uuid, "uuid4", hostile_uuid4)

    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)

    assert ticket.ticket_id
    assert attacker_calls == 0
    assert book.balance == Decimal("90")


def test_open_ticket_never_executes_rebound_placed_at_clock(monkeypatch) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    def hostile_clock():
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound placed_at clock executed")

    monkeypatch.setattr(paper_module, "utc_now_iso", hostile_clock)

    ticket = book.open_ticket([_leg()], "10")

    assert ticket.placed_at
    assert attacker_calls == 0
    assert book.balance == Decimal("90")


@pytest.mark.parametrize(
    ("authority", "message", "use_explicit_placed_at"),
    (
        (paper_module.uuid.uuid4, "ticket id authority changed", True),
        (paper_module.utc_now_iso, "placed_at clock authority changed", False),
    ),
)
def test_open_ticket_rejects_in_place_identity_or_clock_code_mutation_before_execution(
    authority, message: str, use_explicit_placed_at: bool
) -> None:
    book = PaperBook("100")
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match=message):
            if use_explicit_placed_at:
                book.open_ticket([_leg()], "10", placed_at=_TS)
            else:
                book.open_ticket([_leg()], "10")
    finally:
        authority.__code__ = original_code

    assert book.balance == Decimal("100")
    assert book.tickets == {}


def test_open_ticket_never_executes_rebound_ticket_constructor(monkeypatch) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    class HostilePaperTicket:
        def __init__(self, **_kwargs):
            nonlocal attacker_calls
            attacker_calls += 1
            raise AssertionError("rebound ticket constructor executed")

    monkeypatch.setattr(paper_module, "PaperTicket", HostilePaperTicket)

    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)

    assert type(ticket).__name__ == "PaperTicket"
    assert attacker_calls == 0
    assert book.balance == Decimal("90")


def test_open_ticket_rejects_in_place_ticket_constructor_code_mutation_before_execution() -> None:
    book = PaperBook("100")
    constructor = paper_module.PaperTicket.__init__
    original_code = constructor.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        constructor.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="ticket constructor authority changed"):
            book.open_ticket([_leg()], "10", placed_at=_TS)
    finally:
        constructor.__code__ = original_code

    assert book.balance == Decimal("100")
    assert book.tickets == {}


def test_open_ticket_never_executes_rebound_opening_write_authority(monkeypatch) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound opening write authority executed")

    monkeypatch.setattr(paper_module, "_record_ticket_opening_authority", hostile)

    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)

    assert ticket.status.value == "open"
    assert book.committed_stake == Decimal("10")
    assert attacker_calls == 0


def test_open_ticket_never_executes_rebound_causal_open_write_authority(
    monkeypatch,
) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound causal open write authority executed")

    monkeypatch.setattr(
        paper_module, "_advance_paperbook_causal_history_open", hostile
    )

    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)

    assert ticket.status.value == "open"
    assert book.committed_stake == Decimal("10")
    assert attacker_calls == 0


def test_settle_never_executes_rebound_causal_settle_write_authority(
    monkeypatch,
) -> None:
    book = PaperBook("100")
    leg = _leg()
    ticket = book.open_ticket([leg], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound causal settle write authority executed")

    monkeypatch.setattr(
        paper_module, "_advance_paperbook_causal_history_settle", hostile
    )

    settled = book.settle(ticket.ticket_id, {leg.quote_key}, settled_at=_TS)

    assert settled.status.value == "won"
    assert book.committed_stake == Decimal("0")
    assert attacker_calls == 0


@pytest.mark.parametrize(
    ("authority_name", "operation", "message"),
    (
        (
            "_record_ticket_opening_authority",
            "open",
            "opening write authority changed",
        ),
        (
            "_advance_paperbook_causal_history_open",
            "open",
            "causal-history open write authority changed",
        ),
        (
            "_advance_paperbook_causal_history_settle",
            "settle",
            "causal-history settle write authority changed",
        ),
    ),
)
def test_public_mutations_reject_in_place_write_authority_code_mutation(
    authority_name: str,
    operation: str,
    message: str,
) -> None:
    book = PaperBook("100")
    leg = _leg()
    ticket = None
    if operation == "settle":
        ticket = book.open_ticket([leg], "10", placed_at=_TS)

    authority = getattr(paper_module, authority_name)
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match=message):
            if operation == "open":
                book.open_ticket([leg], "10", placed_at=_TS)
            else:
                book.settle(ticket.ticket_id, {leg.quote_key}, settled_at=_TS)
    finally:
        authority.__code__ = original_code


@pytest.mark.parametrize(
    "authority_name",
    (
        "_require_snapshot_candidate_opening_authority",
        "_require_snapshot_candidate_causal_history_authority",
    ),
)
def test_save_never_executes_rebound_snapshot_candidate_authority(
    monkeypatch,
    tmp_path,
    authority_name: str,
) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound snapshot candidate authority executed")

    monkeypatch.setattr(paper_module, authority_name, hostile)

    destination = tmp_path / f"{authority_name}.json"
    book.save(destination)

    assert destination.exists()
    assert attacker_calls == 0


@pytest.mark.parametrize(
    ("authority_name", "message"),
    (
        (
            "_require_snapshot_candidate_opening_authority",
            "snapshot opening candidate authority changed",
        ),
        (
            "_require_snapshot_candidate_causal_history_authority",
            "snapshot causal candidate authority changed",
        ),
    ),
)
def test_save_rejects_in_place_snapshot_candidate_authority_code_mutation(
    tmp_path,
    authority_name: str,
    message: str,
) -> None:
    book = PaperBook("100")
    authority = getattr(paper_module, authority_name)
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))
    destination = tmp_path / f"{authority_name}-mutated.json"

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match=message):
            book.save(destination)
    finally:
        authority.__code__ = original_code

    assert not destination.exists()


@pytest.mark.parametrize(
    "authority_name",
    (
        "_revoke_ticket_opening_authority",
        "_revoke_paperbook_causal_history_authority",
    ),
)
def test_load_bytes_never_executes_rebound_snapshot_revoke_authority(
    monkeypatch,
    authority_name: str,
) -> None:
    source = PaperBook("100")
    payload = {
        "schema_version": 7,
        "initial_bankroll": "100",
        "balance": "100",
        "tickets": [],
        "lifecycle": [],
    }
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound snapshot revoke authority executed")

    monkeypatch.setattr(paper_module, authority_name, hostile)

    decoded = PaperBook.load_bytes(json.dumps(payload).encode("utf-8"))

    with pytest.raises(ValueError, match="lacks product-issued opening authority"):
        _ = decoded.committed_stake
    assert attacker_calls == 0
    assert source.balance == Decimal("100")


@pytest.mark.parametrize(
    ("authority_name", "message"),
    (
        ("_revoke_ticket_opening_authority", "opening revoke authority changed"),
        (
            "_revoke_paperbook_causal_history_authority",
            "causal-history revoke authority changed",
        ),
    ),
)
def test_load_bytes_rejects_in_place_snapshot_revoke_authority_code_mutation(
    authority_name: str,
    message: str,
) -> None:
    payload = json.dumps(
        {
            "schema_version": 7,
            "initial_bankroll": "100",
            "balance": "100",
            "tickets": [],
            "lifecycle": [],
        }
    ).encode("utf-8")
    authority = getattr(paper_module, authority_name)
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match=message):
            PaperBook.load_bytes(payload)
    finally:
        authority.__code__ = original_code


@pytest.mark.parametrize(
    "authority_name",
    (
        "_install_validated_ticket_opening_authority",
        "_install_validated_paperbook_causal_history_authority",
    ),
)
def test_load_never_executes_rebound_snapshot_install_authority(
    monkeypatch,
    tmp_path,
    authority_name: str,
) -> None:
    path = tmp_path / "snapshot.json"
    source = PaperBook("100")
    source.open_ticket([_leg()], "10", placed_at=_TS)
    source.save(path)
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound snapshot install authority executed")

    monkeypatch.setattr(paper_module, authority_name, hostile)

    loaded = PaperBook.load(path)

    assert loaded.committed_stake == Decimal("10")
    assert attacker_calls == 0


@pytest.mark.parametrize(
    ("authority_name", "message"),
    (
        (
            "_install_validated_ticket_opening_authority",
            "opening install authority changed",
        ),
        (
            "_install_validated_paperbook_causal_history_authority",
            "causal-history install authority changed",
        ),
    ),
)
def test_load_rejects_in_place_snapshot_install_authority_code_mutation(
    tmp_path,
    authority_name: str,
    message: str,
) -> None:
    path = tmp_path / "snapshot.json"
    PaperBook("100").save(path)
    authority = getattr(paper_module, authority_name)
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match=message):
            PaperBook.load(path)
    finally:
        authority.__code__ = original_code


def test_load_bytes_never_executes_rebound_json_parser(monkeypatch) -> None:
    payload = json.dumps(
        {
            "schema_version": 7,
            "initial_bankroll": "100",
            "balance": "100",
            "tickets": [],
            "lifecycle": [],
        }
    ).encode("utf-8")
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound JSON parser executed")

    monkeypatch.setattr(paper_module.json, "loads", hostile)

    decoded = PaperBook.load_bytes(payload)

    with pytest.raises(ValueError, match="lacks product-issued opening authority"):
        _ = decoded.committed_stake
    assert attacker_calls == 0


@pytest.mark.parametrize(
    "authority_name",
    (
        "_reject_duplicate_json_keys",
        "_reject_nonfinite_json_constant",
    ),
)
def test_load_bytes_never_executes_rebound_json_rejection_hook(
    monkeypatch,
    authority_name: str,
) -> None:
    payload = json.dumps(
        {
            "schema_version": 7,
            "initial_bankroll": "100",
            "balance": "100",
            "tickets": [],
            "lifecycle": [],
        }
    ).encode("utf-8")
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound JSON rejection hook executed")

    monkeypatch.setattr(paper_module, authority_name, hostile)

    decoded = PaperBook.load_bytes(payload)

    with pytest.raises(ValueError, match="lacks product-issued opening authority"):
        _ = decoded.committed_stake
    assert attacker_calls == 0


@pytest.mark.parametrize(
    ("authority", "message"),
    (
        (paper_module.json.loads, "JSON parser authority changed"),
        (
            paper_module._reject_duplicate_json_keys,
            "duplicate-key authority changed",
        ),
        (
            paper_module._reject_nonfinite_json_constant,
            "non-finite constant authority changed",
        ),
    ),
)
def test_load_bytes_rejects_in_place_json_decode_authority_code_mutation(
    authority,
    message: str,
) -> None:
    payload = b'{"schema_version":7,"initial_bankroll":"100","balance":"100","tickets":[],"lifecycle":[]}'
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match=message):
            PaperBook.load_bytes(payload)
    finally:
        authority.__code__ = original_code


def test_sealed_json_decode_still_rejects_duplicate_keys_and_nonfinite_constants() -> None:
    duplicate = (
        b'{"schema_version":7,"initial_bankroll":"100","initial_bankroll":"200",'
        b'"balance":"100","tickets":[],"lifecycle":[]}'
    )
    nonfinite = (
        b'{"schema_version":7,"initial_bankroll":"100","balance":NaN,'
        b'"tickets":[],"lifecycle":[]}'
    )

    with pytest.raises(ValueError, match="duplicate JSON key: initial_bankroll"):
        PaperBook.load_bytes(duplicate)
    with pytest.raises(ValueError, match="non-finite JSON constant: NaN"):
        PaperBook.load_bytes(nonfinite)



def test_save_never_executes_rebound_json_serializer(monkeypatch, tmp_path) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound JSON serializer executed")

    monkeypatch.setattr(paper_module.json, "dump", hostile)

    destination = tmp_path / "sealed-json-serializer.json"
    book.save(destination)

    assert destination.exists()
    assert attacker_calls == 0
    loaded = PaperBook.load(destination)
    assert loaded.committed_stake == Decimal("10")


def test_save_rejects_in_place_json_serializer_code_mutation(tmp_path) -> None:
    book = PaperBook("100")
    authority = paper_module.json.dump
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))
    destination = tmp_path / "mutated-json-serializer.json"

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="JSON serializer authority changed"):
            book.save(destination)
    finally:
        authority.__code__ = original_code

    assert not destination.exists()

def test_serialized_operation_rejects_in_place_closure_callable_code_mutation() -> None:
    book = PaperBook("100")
    serialized = PaperBook.committed_stake.fget
    assert serialized is not None
    inner = next(
        cell.cell_contents
        for cell in serialized.__closure__ or ()
        if callable(cell.cell_contents)
        and getattr(cell.cell_contents, "__name__", None) == "committed_stake"
    )
    original_code = inner.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        inner.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="operation callable authority changed"):
            _ = book.committed_stake
    finally:
        inner.__code__ = original_code

    assert book.balance == Decimal("100")

def _same_name_closure_callable(function, name: str):
    return next(
        cell.cell_contents
        for cell in function.__closure__ or ()
        if callable(cell.cell_contents)
        and getattr(cell.cell_contents, "__name__", None) == name
    )


def test_constructor_rejects_in_place_wrapped_callable_code_mutation() -> None:
    constructor = PaperBook.__init__
    raw_constructor = _same_name_closure_callable(constructor, "__init__")
    original_code = raw_constructor.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        raw_constructor.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="constructor callable authority changed"):
            PaperBook("100")
    finally:
        raw_constructor.__code__ = original_code


def test_open_ticket_rejects_in_place_nested_callable_code_mutation() -> None:
    book = PaperBook("100")
    serialized = PaperBook.open_ticket
    runtime_guard = _same_name_closure_callable(serialized, "open_ticket")
    transition_guard = _same_name_closure_callable(runtime_guard, "open_ticket")
    raw_open = _same_name_closure_callable(transition_guard, "open_ticket")
    original_code = raw_open.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        raw_open.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="open callable authority changed"):
            book.open_ticket([_leg()], "10", placed_at=_TS)
    finally:
        raw_open.__code__ = original_code

    assert book.balance == Decimal("100")
    assert book.tickets == {}


def test_save_rejects_in_place_nested_publish_callable_code_mutation(tmp_path) -> None:
    book = PaperBook("100")
    serialized = PaperBook.save
    runtime_guard = _same_name_closure_callable(serialized, "save")
    candidate_guard = _same_name_closure_callable(runtime_guard, "save")
    publish_guard = _same_name_closure_callable(candidate_guard, "save")
    original_code = publish_guard.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))
    destination = tmp_path / "nested-publish-code-mutation.json"

    try:
        publish_guard.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="save candidate callable authority changed"):
            book.save(destination)
    finally:
        publish_guard.__code__ = original_code

    assert not destination.exists()


def test_load_bytes_rejects_in_place_raw_decode_callable_code_mutation() -> None:
    descriptor = PaperBook.__dict__["load_bytes"]
    sealed = descriptor.__func__
    raw_decode = _same_name_closure_callable(sealed, "load_bytes")
    original_code = raw_decode.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))
    payload = b'{"schema_version":7,"initial_bankroll":"100","balance":"100","tickets":[],"lifecycle":[]}'

    try:
        raw_decode.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="JSON decode callable authority changed"):
            PaperBook.load_bytes(payload)
    finally:
        raw_decode.__code__ = original_code

def test_public_operation_ignores_rebound_visible_state_validator(monkeypatch) -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    ticket.status = paper_module.TicketStatus.LOST
    attacker_calls = 0

    def hostile_validator(_cls, _book):
        nonlocal attacker_calls
        attacker_calls += 1

    monkeypatch.setattr(
        PaperBook,
        "_validate_loaded_state",
        classmethod(hostile_validator),
    )

    with pytest.raises(
        ValueError,
        match="settled state is missing lifecycle provenance",
    ):
        _ = book.committed_stake

    assert attacker_calls == 0


def test_public_operation_rejects_in_place_visible_state_validator_code_mutation_before_execution() -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    validator = PaperBook.__dict__["_validate_loaded_state"].__func__
    original_code = validator.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        validator.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="visible-state validator authority changed"):
            _ = book.committed_stake
    finally:
        validator.__code__ = original_code

    assert book.balance == Decimal("90")



def test_subclass_constructor_is_rejected_before_overridden_helper_executes() -> None:
    attacker_calls = 0

    class HostilePaperBook(PaperBook):
        @classmethod
        def _canonical_decimal_input(cls, value, label):
            nonlocal attacker_calls
            attacker_calls += 1
            raise AssertionError("subclass decimal helper executed")

    with pytest.raises(ValueError, match="canonical PaperBook type"):
        HostilePaperBook("100")

    assert attacker_calls == 0


def test_subclass_load_bytes_is_rejected_before_overridden_parser_executes() -> None:
    attacker_calls = 0

    class HostilePaperBook(PaperBook):
        @classmethod
        def _parse_snapshot_decimal(cls, value, label):
            nonlocal attacker_calls
            attacker_calls += 1
            raise AssertionError("subclass snapshot parser executed")

    payload = (
        b'{"schema_version":7,"initial_bankroll":"100","balance":"100",'
        b'"tickets":[],"lifecycle":[]}'
    )

    with pytest.raises(ValueError, match="canonical PaperBook type"):
        HostilePaperBook.load_bytes(payload)

    assert attacker_calls == 0


def test_subclass_load_is_rejected_before_overridden_path_helper_executes(
    tmp_path,
) -> None:
    attacker_calls = 0

    class HostilePaperBook(PaperBook):
        @staticmethod
        def _canonical_snapshot_path(path):
            nonlocal attacker_calls
            attacker_calls += 1
            raise AssertionError("subclass path helper executed")

    with pytest.raises(ValueError, match="canonical PaperBook type"):
        HostilePaperBook.load(tmp_path / "never-read.json")

    assert attacker_calls == 0


def test_private_registries_reject_forged_subclass_before_key_hooks_execute() -> None:
    hash_calls = 0

    class HostilePaperBook(PaperBook):
        def __hash__(self):
            nonlocal hash_calls
            hash_calls += 1
            raise AssertionError("subclass hash executed")

    forged = object.__new__(HostilePaperBook)

    with pytest.raises(ValueError, match="canonical PaperBook type"):
        paper_module._register_ticket_opening_authority_book(forged)
    with pytest.raises(ValueError, match="canonical PaperBook type"):
        paper_module._register_paperbook_causal_history_authority_book(forged)
    with pytest.raises(ValueError, match="canonical PaperBook type"):
        paper_module._register_paperbook_operation_lock(forged)

    assert hash_calls == 0


def test_exact_paperbook_type_remains_operational_after_type_authority_seal(
    tmp_path,
) -> None:
    path = tmp_path / "canonical-paper-book.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    book.save(path)
    restored = PaperBook.load(path)

    assert type(restored) is PaperBook
    assert restored.committed_stake == Decimal("10")
    assert restored.tickets[ticket.ticket_id].legs[0].locked_odds == Decimal("2.00")


def test_constructor_ignores_rebound_canonical_type_authority(monkeypatch) -> None:
    attacker_calls = 0

    def hostile(_target):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound canonical type authority executed")

    monkeypatch.setattr(paper_module, "_require_paperbook_type_authority", hostile)

    book = PaperBook("100")

    assert type(book) is PaperBook
    assert book.balance == Decimal("100")
    assert attacker_calls == 0


def test_constructor_rejects_in_place_canonical_type_authority_code_mutation() -> None:
    authority = paper_module._require_paperbook_type_authority
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="canonical type authority changed"):
            PaperBook("100")
    finally:
        authority.__code__ = original_code


def test_load_bytes_rejects_rebound_raw_snapshot_decoder_before_execution(
    monkeypatch,
) -> None:
    payload = (
        b'{"schema_version":7,"initial_bankroll":"100","balance":"100",'
        b'"tickets":[],"lifecycle":[]}'
    )
    attacker_calls = 0

    def hostile(cls, raw):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound raw snapshot decoder executed")

    monkeypatch.setattr(PaperBook, "_from_raw_snapshot", classmethod(hostile))

    with pytest.raises(ValueError, match="raw snapshot decoder dispatch changed"):
        PaperBook.load_bytes(payload)

    assert attacker_calls == 0


def test_load_rejects_rebound_byte_loader_before_execution(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "canonical-byte-loader.json"
    PaperBook("100").save(path)
    attacker_calls = 0

    def hostile(cls, payload):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound byte loader executed")

    monkeypatch.setattr(PaperBook, "load_bytes", classmethod(hostile))

    with pytest.raises(ValueError, match="byte loader dispatch changed"):
        PaperBook.load(path)

    assert attacker_calls == 0


def test_load_rejects_rebound_snapshot_path_helper_before_execution(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "canonical-path-helper.json"
    PaperBook("100").save(path)
    attacker_calls = 0

    def hostile(path_value):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound snapshot path helper executed")

    monkeypatch.setattr(
        PaperBook,
        "_canonical_snapshot_path",
        staticmethod(hostile),
    )

    with pytest.raises(ValueError, match="snapshot path dispatch changed"):
        PaperBook.load(path)

    assert attacker_calls == 0


def test_load_bytes_rejects_in_place_raw_snapshot_decoder_code_mutation() -> None:
    payload = (
        b'{"schema_version":7,"initial_bankroll":"100","balance":"100",'
        b'"tickets":[],"lifecycle":[]}'
    )
    descriptor = PaperBook.__dict__["_from_raw_snapshot"]
    authority = descriptor.__func__
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="raw snapshot decoder authority changed"):
            PaperBook.load_bytes(payload)
    finally:
        authority.__code__ = original_code


def test_load_rejects_in_place_byte_loader_code_mutation(tmp_path) -> None:
    path = tmp_path / "mutated-byte-loader.json"
    PaperBook("100").save(path)
    descriptor = PaperBook.__dict__["load_bytes"]
    authority = descriptor.__func__
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="byte loader authority changed"):
            PaperBook.load(path)
    finally:
        authority.__code__ = original_code


def test_load_rejects_in_place_snapshot_path_helper_code_mutation(tmp_path) -> None:
    path = tmp_path / "mutated-path-helper.json"
    PaperBook("100").save(path)
    descriptor = PaperBook.__dict__["_canonical_snapshot_path"]
    authority = descriptor.__func__
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="snapshot path authority changed"):
            PaperBook.load(path)
    finally:
        authority.__code__ = original_code


def test_constructor_rejects_rebound_canonical_decimal_helper_before_execution(
    monkeypatch,
) -> None:
    attacker_calls = 0

    def hostile(cls, value, label):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound constructor decimal helper executed")

    monkeypatch.setattr(
        PaperBook,
        "_canonical_decimal_input",
        classmethod(hostile),
    )

    with pytest.raises(ValueError, match="constructor decimal dispatch changed"):
        PaperBook("100")

    assert attacker_calls == 0


def test_constructor_rejects_in_place_canonical_decimal_helper_code_mutation() -> None:
    descriptor = PaperBook.__dict__["_canonical_decimal_input"]
    authority = descriptor.__func__
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(ValueError, match="constructor decimal authority changed"):
            PaperBook("100")
    finally:
        authority.__code__ = original_code

def test_save_rejects_rebound_raw_snapshot_decoder_before_execution(
    monkeypatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(cls, raw):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound raw snapshot decoder executed")

    monkeypatch.setattr(PaperBook, "_from_raw_snapshot", classmethod(hostile))

    with pytest.raises(ValueError, match="raw snapshot decoder dispatch changed"):
        book.save(tmp_path / "never-published.json")

    assert attacker_calls == 0


def test_save_rejects_rebound_snapshot_path_helper_before_execution(
    monkeypatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(path_value):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound snapshot path helper executed")

    monkeypatch.setattr(
        PaperBook,
        "_canonical_snapshot_path",
        staticmethod(hostile),
    )

    with pytest.raises(ValueError, match="snapshot path dispatch changed"):
        book.save(tmp_path / "never-published.json")

    assert attacker_calls == 0

def test_save_rejects_rebound_lifecycle_serializer_before_execution(
    monkeypatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(self):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound lifecycle serializer executed")

    monkeypatch.setattr(PaperBook, "_lifecycle_to_json", hostile)

    with pytest.raises(ValueError, match="lifecycle serializer dispatch changed"):
        book.save(tmp_path / "never-published.json")

    assert attacker_calls == 0


def test_save_rejects_rebound_parent_durability_helper_before_execution(
    monkeypatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(cls, directory):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound parent durability helper executed")

    monkeypatch.setattr(
        PaperBook,
        "_ensure_snapshot_parent_durable",
        classmethod(hostile),
    )

    with pytest.raises(ValueError, match="parent durability dispatch changed"):
        book.save(tmp_path / "never-published.json")

    assert attacker_calls == 0


def test_save_rejects_rebound_directory_fsync_helper_before_execution(
    monkeypatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(directory):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound directory fsync helper executed")

    monkeypatch.setattr(
        PaperBook,
        "_fsync_snapshot_directory",
        staticmethod(hostile),
    )

    with pytest.raises(ValueError, match="directory fsync dispatch changed"):
        book.save(tmp_path / "never-published.json")

    assert attacker_calls == 0


def test_save_rejects_rebound_directory_fsync_before_nested_parent_creation(
    monkeypatch,
    tmp_path,
) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(directory):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound nested directory fsync helper executed")

    monkeypatch.setattr(
        PaperBook,
        "_fsync_snapshot_directory",
        staticmethod(hostile),
    )

    with pytest.raises(ValueError, match="directory fsync dispatch changed"):
        book.save(tmp_path / "missing-parent" / "nested" / "never-published.json")

    assert attacker_calls == 0
    assert not (tmp_path / "missing-parent").exists()



_SNAPSHOT_HELPER_AUTHORITY_NAMES = (
    "_require_finite",
    "_require_canonical_text",
    "_validate_timestamp",
    "_validate_ticket_leg",
    "_validate_lifecycle_entry",
    "_validate_lifecycle_reachability",
    "_validate_loaded_state",
    "_parse_lifecycle_key_list",
    "_parse_lifecycle",
    "_parse_snapshot_decimal",
    "_required_snapshot_field",
    "_parse_snapshot_legs",
    "_parse_snapshot_provider_accounts",
    "_parse_snapshot_status",
)


@pytest.mark.parametrize("helper_name", _SNAPSHOT_HELPER_AUTHORITY_NAMES)
def test_load_bytes_rejects_rebound_snapshot_helper_before_execution(
    monkeypatch,
    helper_name: str,
) -> None:
    payload = (
        b'{"schema_version":7,"initial_bankroll":"100","balance":"100",'
        b'"tickets":[],"lifecycle":[]}'
    )
    descriptor = PaperBook.__dict__[helper_name]
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError(f"rebound snapshot helper executed: {helper_name}")

    if type(descriptor) is classmethod:
        replacement = classmethod(hostile)
    elif type(descriptor) is staticmethod:
        replacement = staticmethod(hostile)
    else:
        replacement = hostile

    monkeypatch.setattr(PaperBook, helper_name, replacement)

    with pytest.raises(
        ValueError,
        match=rf"snapshot helper dispatch changed: {helper_name}",
    ):
        PaperBook.load_bytes(payload)

    assert attacker_calls == 0


@pytest.mark.parametrize("helper_name", _SNAPSHOT_HELPER_AUTHORITY_NAMES)
def test_load_bytes_rejects_in_place_snapshot_helper_code_mutation(
    helper_name: str,
) -> None:
    payload = (
        b'{"schema_version":7,"initial_bankroll":"100","balance":"100",'
        b'"tickets":[],"lifecycle":[]}'
    )
    descriptor = PaperBook.__dict__[helper_name]
    if type(descriptor) in {classmethod, staticmethod}:
        authority = descriptor.__func__
    else:
        authority = descriptor
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(
            ValueError,
            match=rf"snapshot helper authority changed: {helper_name}",
        ):
            PaperBook.load_bytes(payload)
    finally:
        authority.__code__ = original_code

def test_open_ticket_rejects_rebound_decimal_helper_before_execution(
    monkeypatch,
) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    def hostile(cls, value, label):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound open decimal helper executed")

    monkeypatch.setattr(PaperBook, "_canonical_decimal_input", classmethod(hostile))

    with pytest.raises(ValueError, match="decimal helper dispatch changed"):
        book.open_ticket([_leg()], "10", placed_at=_TS)

    assert attacker_calls == 0
    assert book.balance == Decimal("100")
    assert book.tickets == {}


def test_open_ticket_rejects_rebound_debit_helper_before_execution(
    monkeypatch,
) -> None:
    book = PaperBook("100")
    attacker_calls = 0

    def hostile(cls, balance, amount):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound debit helper executed")

    monkeypatch.setattr(PaperBook, "_debit_balance", classmethod(hostile))

    with pytest.raises(ValueError, match="debit helper dispatch changed"):
        book.open_ticket([_leg()], "10", placed_at=_TS)

    assert attacker_calls == 0
    assert book.balance == Decimal("100")
    assert book.tickets == {}


def test_settle_rejects_rebound_settlement_helper_before_execution(
    monkeypatch,
) -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    attacker_calls = 0

    def hostile(cls, current_ticket, balance, winners, voids):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound settlement helper executed")

    monkeypatch.setattr(PaperBook, "_settlement_result", classmethod(hostile))

    with pytest.raises(ValueError, match="settlement helper dispatch changed"):
        book.settle(ticket.ticket_id, {ticket.legs[0].quote_key})

    assert attacker_calls == 0
    assert ticket.status is paper_module.TicketStatus.OPEN
    assert ticket.payout == Decimal("0")
    assert book.balance == Decimal("90")



_RUNTIME_HELPER_AUTHORITY_NAMES = (
    "_require_finite",
    "_require_utf8_string",
    "_require_canonical_text",
    "_validate_ticket_provenance",
    "_validate_timestamp",
    "_validate_placed_at",
    "_validate_settled_at",
    "_validate_ticket_leg",
    "_validate_lifecycle_entry",
    "_validate_lifecycle_reachability",
    "_validate_loaded_state",
    "_normalize_resolution_keys",
    "_parse_lifecycle_key_list",
    "_parse_lifecycle",
    "_parse_snapshot_decimal",
    "_required_snapshot_field",
    "_parse_snapshot_legs",
    "_parse_snapshot_provider_accounts",
    "_parse_snapshot_status",
)


@pytest.mark.parametrize("helper_name", _RUNTIME_HELPER_AUTHORITY_NAMES)
def test_public_operation_rejects_rebound_runtime_helper_before_execution(
    monkeypatch,
    helper_name: str,
) -> None:
    book = PaperBook("100")
    descriptor = PaperBook.__dict__[helper_name]
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError(f"rebound runtime helper executed: {helper_name}")

    if type(descriptor) is classmethod:
        replacement = classmethod(hostile)
    elif type(descriptor) is staticmethod:
        replacement = staticmethod(hostile)
    else:
        replacement = hostile

    monkeypatch.setattr(PaperBook, helper_name, replacement)

    with pytest.raises(
        ValueError,
        match=rf"runtime helper dispatch changed: {helper_name}",
    ):
        _ = book.committed_stake

    assert attacker_calls == 0


@pytest.mark.parametrize("helper_name", _RUNTIME_HELPER_AUTHORITY_NAMES)
def test_public_operation_rejects_in_place_runtime_helper_code_mutation(
    helper_name: str,
) -> None:
    book = PaperBook("100")
    descriptor = PaperBook.__dict__[helper_name]
    if type(descriptor) in {classmethod, staticmethod}:
        authority = descriptor.__func__
    else:
        authority = descriptor
    original_code = authority.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        authority.__code__ = hostile.__code__
        with pytest.raises(
            ValueError,
            match=rf"runtime helper authority changed: {helper_name}",
        ):
            _ = book.committed_stake
    finally:
        authority.__code__ = original_code

@pytest.mark.parametrize(
    ("property_name", "error_match"),
    [
        ("parent", "snapshot parent getter callable authority changed"),
        ("name", "snapshot name getter callable authority changed"),
    ],
)
def test_save_rejects_in_place_snapshot_path_property_getter_code_mutation_before_execution(
    tmp_path,
    property_name: str,
    error_match: str,
) -> None:
    book = PaperBook("100")
    book.open_ticket([_leg()], "10", placed_at=_TS)
    path_type = type(Path("."))
    descriptor = getattr(path_type, property_name)
    getter = descriptor.fget
    assert getter is not None
    original_code = getter.__code__
    hostile = _hostile_function_with_freevars(len(original_code.co_freevars))

    try:
        getter.__code__ = hostile.__code__
        with pytest.raises(ValueError, match=error_match):
            book.save(tmp_path / "paper-book.json")
    finally:
        getter.__code__ = original_code

    assert not (tmp_path / "paper-book.json").exists()


def test_lay_open_uses_locked_liability_and_committed_capital() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_lay_leg(odds="3.00")], "10", placed_at=_TS)

    assert ticket.stake == Decimal("10")
    assert book.balance == Decimal("80")
    assert book.committed_stake == Decimal("10")
    assert book.committed_capital == Decimal("20")


def test_lay_selection_winner_is_a_loss_without_additional_debit() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_lay_leg(odds="3.00")], "10", placed_at=_TS)

    settled = book.settle(ticket.ticket_id, {ticket.legs[0].settlement_key}, settled_at=_TS)

    assert settled.status is paper_module.TicketStatus.LOST
    assert settled.payout == Decimal("0")
    assert book.balance == Decimal("80")
    assert book.committed_capital == Decimal("0")


def test_lay_selection_loser_returns_stake_plus_liability() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_lay_leg(odds="3.00")], "10", placed_at=_TS)

    settled = book.settle(ticket.ticket_id, set(), settled_at=_TS)

    assert settled.status is paper_module.TicketStatus.WON
    assert settled.payout == Decimal("30")
    assert book.balance == Decimal("110")
    assert book.committed_capital == Decimal("0")


def test_lay_void_returns_locked_liability_only() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_lay_leg(odds="3.00")], "10", placed_at=_TS)

    settled = book.settle(
        ticket.ticket_id,
        set(),
        {ticket.legs[0].settlement_key},
        settled_at=_TS,
    )

    assert settled.status is paper_module.TicketStatus.VOID
    assert settled.payout == Decimal("20")
    assert book.balance == Decimal("100")
    assert book.committed_capital == Decimal("0")


def test_multi_leg_lay_is_rejected_before_bankroll_mutation() -> None:
    book = PaperBook("100")

    with pytest.raises(
        ValueError,
        match="exactly one canonical single-leg LAY ticket",
    ):
        book.open_ticket(
            [_lay_leg(), _leg()],
            "10",
            placed_at=_TS,
        )

    assert book.balance == Decimal("100")
    assert book.tickets == {}


def test_market_semantics_identity_survives_schema8_save_load(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    ticket = book.open_ticket(
        [_lay_leg(semantics="exchange.match.odds.v2")],
        "10",
        placed_at=_TS,
    )

    book.save(path)
    raw = paper_module.json.loads(path.read_text(encoding="utf-8"))
    raw_leg = raw["tickets"][0]["legs"][0]

    assert raw["schema_version"] == 8
    assert raw_leg["market_semantics_id"] == "exchange.match.odds.v2"

    reopened = PaperBook.load(path)
    reopened_ticket = reopened.tickets[ticket.ticket_id]
    assert reopened_ticket.legs[0].market_semantics_id == "exchange.match.odds.v2"
    assert reopened.committed_capital == Decimal("20")


def test_schema8_market_semantics_field_cannot_be_silently_downgraded(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    book.open_ticket([_lay_leg()], "10", placed_at=_TS)
    book.save(path)

    raw = paper_module.json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 7

    with pytest.raises(
        ValueError,
        match="unsupported before schema 8",
    ):
        PaperBook.load_bytes(
            paper_module.json.dumps(raw, separators=(",", ":")).encode("utf-8")
        )


def test_schema7_without_market_semantics_remains_backward_readable(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg()], "10", placed_at=_TS)
    book.save(path)

    raw = paper_module.json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 7
    for leg in raw["tickets"][0]["legs"]:
        leg.pop("market_semantics_id", None)

    reopened = PaperBook.load_bytes(
        paper_module.json.dumps(raw, separators=(",", ":")).encode("utf-8")
    )
    assert reopened.tickets[ticket.ticket_id].legs[0].market_semantics_id is None


def test_market_semantics_mutation_after_admission_cannot_change_settlement() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket(
        [_lay_leg(semantics="exchange.match.odds.v2")],
        "10",
        placed_at=_TS,
    )
    original_key = ticket.legs[0].quote_key
    object.__setattr__(
        ticket.legs[0],
        "market_semantics_id",
        "exchange.match.odds.v3",
    )

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        book.settle(ticket.ticket_id, set())

    assert ticket.status is paper_module.TicketStatus.OPEN
    assert ticket.payout == Decimal("0")
    assert book.balance == Decimal("80")
    assert ticket.legs[0].quote_key == original_key


def test_lay_capital_root_rejects_underlying_calculator_code_mutation() -> None:
    calculator = paper_module.locked_capital_for_exchange_side
    original_code = calculator.__code__

    def hostile(*_args, **_kwargs):
        raise AssertionError("mutated exchange exposure calculator executed")

    try:
        calculator.__code__ = hostile.__code__
        book = PaperBook("100")
        with pytest.raises(
            ValueError,
            match="exchange exposure authority changed",
        ):
            book.open_ticket([_lay_leg()], "10", placed_at=_TS)
    finally:
        calculator.__code__ = original_code


def test_paperbook_locked_capital_authority_rejects_outer_closure_retarget_before_execution() -> None:
    authority = paper_module._CANONICAL_LOCKED_CAPITAL_FOR_TICKET
    target_cell = next(
        cell
        for cell in authority.__closure__ or ()
        if cell.cell_contents is paper_module.locked_capital_for_exchange_side
    )
    original_value = target_cell.cell_contents
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("retargeted PaperBook locked-capital authority executed")

    try:
        target_cell.cell_contents = hostile
        book = PaperBook("100")
        with pytest.raises(
            ValueError,
            match=r"runtime module dependency closure changed: _CANONICAL_LOCKED_CAPITAL_FOR_TICKET",
        ):
            book.open_ticket([_lay_leg()], "10", placed_at=_TS)
    finally:
        target_cell.cell_contents = original_value

    assert attacker_calls == 0


def test_paperbook_market_semantics_authority_rejects_outer_closure_retarget_before_execution() -> None:
    authority = paper_module._CANONICAL_MARKET_SEMANTICS_IDENTITY
    target_cell = next(
        cell
        for cell in authority.__closure__ or ()
        if cell.cell_contents is paper_module._canonical_semantic_identity
    )
    original_value = target_cell.cell_contents
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("retargeted market semantics authority executed")

    try:
        target_cell.cell_contents = hostile
        book = PaperBook("100")
        with pytest.raises(
            ValueError,
            match=r"runtime module dependency closure changed: _CANONICAL_MARKET_SEMANTICS_IDENTITY",
        ):
            book.open_ticket(
                [_lay_leg(semantics="exchange.match.odds.v2")],
                "10",
                placed_at=_TS,
            )
    finally:
        target_cell.cell_contents = original_value

    assert attacker_calls == 0


def test_lay_capital_root_rejects_underlying_calculator_closure_mutation() -> None:
    calculator = paper_module.locked_capital_for_exchange_side
    target_cell = next(
        cell
        for cell in calculator.__closure__ or ()
        if cell.cell_contents is paper_module._validate_decimal_text_resource_bound
    )
    original_value = target_cell.cell_contents
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("mutated exchange exposure closure executed")

    try:
        target_cell.cell_contents = hostile
        book = PaperBook("100")
        with pytest.raises(
            ValueError,
            match="exchange exposure authority changed",
        ):
            book.open_ticket([_lay_leg()], "10", placed_at=_TS)
    finally:
        target_cell.cell_contents = original_value

    assert attacker_calls == 0


def test_market_semantics_root_rejects_rebound_underlying_validator_dependency_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attacker_calls = 0

    def hostile(*_args, **_kwargs):
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("rebound market semantics dependency executed")

    monkeypatch.setattr(domain_module, "_canonical_string_value", hostile)
    book = PaperBook("100")

    with pytest.raises(
        ValueError,
        match=r"market-semantics dependency changed: _canonical_string_value",
    ):
        book.open_ticket(
            [_lay_leg(semantics="exchange.match.odds.v2")],
            "10",
            placed_at=_TS,
        )

    assert attacker_calls == 0


def test_market_semantics_root_rejects_underlying_validator_code_mutation() -> None:
    validator = paper_module._canonical_semantic_identity
    original_code = validator.__code__

    def hostile(*_args, **_kwargs):
        raise AssertionError("mutated market semantics validator executed")

    try:
        validator.__code__ = hostile.__code__
        book = PaperBook("100")
        with pytest.raises(
            ValueError,
            match="market-semantics identity authority changed",
        ):
            book.open_ticket([_lay_leg()], "10", placed_at=_TS)
    finally:
        validator.__code__ = original_code


def test_ticket_leg_settlement_key_binds_quote_to_market_semantics() -> None:
    back = _leg()
    first = TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal("3.00"),
        sport="soccer",
        exchange_side="lay",
        market_semantics_id="exchange.match.odds.v1",
    )
    second = TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal("3.00"),
        sport="soccer",
        exchange_side="lay",
        market_semantics_id="exchange.match.odds.v2",
    )

    assert first.quote_key == second.quote_key
    assert first.settlement_key != second.settlement_key
    assert back.settlement_key == back.quote_key
    assert first.settlement_identity == (
        first.quote_key,
        "exchange.match.odds.v1",
    )


def test_ticket_leg_market_semantics_id_rejects_reserved_and_noncanonical_values() -> None:
    with pytest.raises(ValueError, match="lowercase canonical semantic identity"):
        TicketLeg(
            "event-1",
            "market-1",
            "selection-1",
            Decimal("3.00"),
            market_semantics_id="Exchange.Match.Odds",
        )

    with pytest.raises(ValueError, match="reserved identity"):
        TicketLeg(
            "event-1",
            "market-1",
            "selection-1",
            Decimal("3.00"),
            market_semantics_id="unknown",
        )


def test_ticket_leg_settlement_key_changes_when_semantics_is_mutated() -> None:
    leg = TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal("3.00"),
        sport="soccer",
        exchange_side="lay",
        market_semantics_id="exchange.match.odds.v1",
    )
    original = leg.settlement_key
    object.__setattr__(leg, "market_semantics_id", "exchange.match.odds.v2")

    assert leg.quote_key == TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal("3.00"),
        sport="soccer",
        exchange_side="lay",
        market_semantics_id="exchange.match.odds.v2",
    ).quote_key
    assert leg.settlement_key != original


def test_schema8_preserves_provider_provenance_and_settlement_time(tmp_path) -> None:
    path = tmp_path / "schema8-provider-roundtrip.json"
    book = PaperBook("100")
    ticket = book.open_ticket(
        [_lay_leg(semantics="exchange.match.odds.v3")],
        "10",
        reason="provider-bound",
        placed_at=_TS,
        provider_source_ids=("betfair-live",),
        provider_accounts=(("betfair", "account-1"),),
        bankroll_id="bankroll-live",
        currency="USD",
    )
    book.settle(
        ticket.ticket_id,
        set(),
        settled_at="2026-10-05T00:05:00+00:00",
    )
    book.save(path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 8
    persisted = raw["tickets"][0]
    assert persisted["provider_source_ids"] == ["betfair-live"]
    assert persisted["provider_accounts"] == [
        {"source_id": "betfair", "account_id": "account-1"}
    ]
    assert persisted["bankroll_id"] == "bankroll-live"
    assert persisted["currency"] == "USD"
    assert persisted["settled_at"] == "2026-10-05T00:05:00+00:00"

    reopened = PaperBook.load(path)
    restored = reopened.tickets[ticket.ticket_id]
    assert restored.provider_source_ids == ("betfair-live",)
    assert restored.provider_accounts == (("betfair", "account-1"),)
    assert restored.bankroll_id == "bankroll-live"
    assert restored.currency == "USD"
    assert restored.settled_at == "2026-10-05T00:05:00+00:00"
    assert restored.legs[0].market_semantics_id == "exchange.match.odds.v3"


def test_lay_settlement_requires_market_semantics_bound_key() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_lay_leg(semantics="exchange.match.odds.v9")], "10", placed_at=_TS)

    with pytest.raises(
        ValueError,
        match="unknown winning settlement key",
    ):
        book.settle(ticket.ticket_id, {ticket.legs[0].quote_key}, settled_at=_TS)

    assert ticket.status is paper_module.TicketStatus.OPEN
    assert book.balance == Decimal("80")

    settled = book.settle(
        ticket.ticket_id,
        {ticket.legs[0].settlement_key},
        settled_at=_TS,
    )
    assert settled.status is paper_module.TicketStatus.LOST
    assert settled.payout == Decimal("0")


def test_settlement_key_rejects_in_place_semantic_validator_code_mutation() -> None:
    validator = paper_module._canonical_semantic_identity
    original_code = validator.__code__
    try:
        def hostile(*_args, **_kwargs):
            raise AssertionError("mutated semantic validator executed")

        validator.__code__ = hostile.__code__
        leg = TicketLeg(
            "event-1",
            "market-1",
            "selection-1",
            Decimal("3.00"),
            exchange_side="lay",
            market_semantics_id="exchange.match.odds.v1",
        )
        with pytest.raises(
            ValueError,
            match="market settlement identity authority changed",
        ):
            _ = leg.settlement_key
    finally:
        validator.__code__ = original_code


def test_settlement_key_rejects_in_place_json_serializer_code_mutation() -> None:
    serializer = paper_module.json.dumps
    original_code = serializer.__code__
    try:
        def hostile(*_args, **_kwargs):
            raise AssertionError("mutated json serializer executed")

        serializer.__code__ = hostile.__code__
        leg = TicketLeg(
            "event-1",
            "market-1",
            "selection-1",
            Decimal("3.00"),
            exchange_side="lay",
            market_semantics_id="exchange.match.odds.v1",
        )
        with pytest.raises(
            ValueError,
            match="market settlement identity authority changed",
        ):
            _ = leg.settlement_key
    finally:
        serializer.__code__ = original_code
