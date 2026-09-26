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


def test_paperbook_round_trip_preserves_exchange_side_and_side_identity(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    back = _leg("back")
    lay = _leg("lay")
    ticket = book.open_ticket([back, lay], "10", placed_at=_TS)

    assert {item.quote_key for item in ticket.legs} == {back.quote_key, lay.quote_key}
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 7
    assert [item["exchange_side"] for item in payload["tickets"][0]["legs"]] == [
        "back",
        "lay",
    ]

    restored = PaperBook.load(path)
    restored_legs = next(iter(restored.tickets.values())).legs
    assert [item.exchange_side for item in restored_legs] == ["back", "lay"]
    assert {item.quote_key for item in restored_legs} == {back.quote_key, lay.quote_key}


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
