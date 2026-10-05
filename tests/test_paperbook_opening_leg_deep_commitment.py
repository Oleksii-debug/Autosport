from decimal import Decimal

import pytest

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
