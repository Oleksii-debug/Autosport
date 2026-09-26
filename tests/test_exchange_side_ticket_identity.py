from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autosport.domain import MarketEvent, TicketLeg
from autosport.paper import PaperBook


_TS = "2026-09-21T21:40:00+00:00"


def _leg(side: str | None, *, sport: str | None = "soccer") -> TicketLeg:
    return TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal("2.5"),
        sport=sport,
        exchange_side=side,
    )


def test_ticket_leg_back_and_lay_are_identity_disjoint_while_legacy_is_unchanged() -> None:
    back = _leg("back", sport=None)
    lay = _leg("lay", sport=None)
    legacy = _leg(None, sport=None)

    assert back.quote_key.startswith("exchange-side-v1-")
    assert lay.quote_key.startswith("exchange-side-v1-")
    assert back.quote_key != lay.quote_key
    assert legacy.quote_key == "event-1|market-1|selection-1"


def test_market_event_and_ticket_leg_share_exact_side_bearing_quote_identity() -> None:
    event = MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.5"),
        observed_ts=_TS,
        source_id="matchbook",
        sequence=1,
        ingest_ts=_TS,
        sport="soccer",
        exchange_side="lay",
    )
    leg = _leg("lay")

    assert event.quote_key == leg.quote_key


def test_paperbook_round_trip_preserves_supported_back_side_identity(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    back = _leg("back")
    ticket = book.open_ticket([back], "10", placed_at=_TS)

    assert ticket.legs[0].quote_key == back.quote_key
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 7
    assert payload["tickets"][0]["legs"][0]["exchange_side"] == "back"

    restored_leg = next(iter(PaperBook.load(path).tickets.values())).legs[0]
    assert restored_leg.exchange_side == "back"
    assert restored_leg.quote_key == back.quote_key


def test_paperbook_rejects_lay_before_open_economic_mutation() -> None:
    book = PaperBook("100")
    balance_before = book.balance

    with pytest.raises(ValueError, match="LAY economic materialization"):
        book.open_ticket([_leg("lay")], "10", placed_at=_TS)

    assert book.balance == balance_before
    assert book.tickets == {}
    assert book._lifecycle == []


def test_paperbook_rejects_lay_mutation_before_settlement() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg("back")], "10", placed_at=_TS)
    lay = _leg("lay")
    ticket.legs = (lay,)
    balance_before = book.balance

    with pytest.raises(ValueError, match="LAY economic materialization"):
        book.settle(ticket.ticket_id, {lay.quote_key}, settled_at=_TS)

    assert book.balance == balance_before
    assert ticket.status.value == "open"
    assert ticket.payout == Decimal("0")
    assert book._lifecycle == [("open", ticket.ticket_id, (), ())]


def test_paperbook_rejects_coherent_stake_rewrite_before_settlement() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg("back")], "10", placed_at=_TS)

    # Keep lifecycle replay arithmetically coherent with the forged stake so the
    # private opening authority, not a balance mismatch, is the decisive fence.
    ticket.stake = Decimal("20")
    book.balance = Decimal("80")
    balance_before = book.balance
    lifecycle_before = tuple(book._lifecycle)

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        book.settle(
            ticket.ticket_id,
            {ticket.legs[0].quote_key},
            settled_at=_TS,
        )

    assert book.balance == balance_before
    assert tuple(book._lifecycle) == lifecycle_before
    assert ticket.status.value == "open"
    assert ticket.payout == Decimal("0")


def test_paperbook_rejects_same_quote_inflated_odds_before_settlement() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg("back")], "10", placed_at=_TS)
    original_quote_key = ticket.legs[0].quote_key
    inflated = TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal("100"),
        sport="soccer",
        exchange_side="back",
    )
    assert inflated.quote_key == original_quote_key
    ticket.legs = (inflated,)

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


def test_paperbook_rejects_opening_rewrite_before_save_and_after_round_trip(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg("back")], "10", placed_at=_TS)
    book.save(path)
    durable_before = path.read_bytes()

    ticket.stake = Decimal("20")
    book.balance = Decimal("80")
    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        book.save(path)
    assert path.read_bytes() == durable_before

    restored = PaperBook.load(path)
    restored_ticket = next(iter(restored.tickets.values()))
    inflated = TicketLeg(
        restored_ticket.legs[0].event_id,
        restored_ticket.legs[0].market_id,
        restored_ticket.legs[0].selection_id,
        Decimal("100"),
        sport=restored_ticket.legs[0].sport,
        exchange_side=restored_ticket.legs[0].exchange_side,
    )
    assert inflated.quote_key == restored_ticket.legs[0].quote_key
    restored_ticket.legs = (inflated,)

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        restored.save(path)
    assert path.read_bytes() == durable_before


def test_paperbook_save_and_load_fail_closed_on_lay_materialization(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg("back")], "10", placed_at=_TS)
    book.save(path)
    durable_before = path.read_bytes()

    ticket.legs = (_leg("lay"),)
    with pytest.raises(ValueError, match="LAY economic materialization"):
        book.save(path)
    assert path.read_bytes() == durable_before

    payload = json.loads(durable_before.decode("utf-8"))
    payload["tickets"][0]["legs"][0]["exchange_side"] = "lay"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="LAY economic materialization"):
        PaperBook.load(path)


def test_schema6_snapshot_keeps_sport_and_upgrades_legacy_no_side_to_none(tmp_path) -> None:
    path = tmp_path / "paper-book-v6.json"
    book = PaperBook("100")
    book.open_ticket([_leg(None)], "10", placed_at=_TS)
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 6
    for item in payload["tickets"][0]["legs"]:
        item.pop("exchange_side")
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = PaperBook.load(path)
    restored_leg = next(iter(restored.tickets.values())).legs[0]
    assert restored_leg.sport == "soccer"
    assert restored_leg.exchange_side is None


def test_schema7_requires_explicit_exchange_side_field_even_when_none(tmp_path) -> None:
    path = tmp_path / "paper-book-v7.json"
    book = PaperBook("100")
    book.open_ticket([_leg(None)], "10", placed_at=_TS)
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tickets"][0]["legs"][0].pop("exchange_side")
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="exchange_side"):
        PaperBook.load(path)


@pytest.mark.parametrize("side", ["BACK", "Lay", "back ", "", "buy"])
def test_ticket_leg_rejects_noncanonical_exchange_side(side: str) -> None:
    with pytest.raises(ValueError, match="exchange_side"):
        _leg(side)
