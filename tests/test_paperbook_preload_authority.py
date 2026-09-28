from __future__ import annotations

import json
from decimal import Decimal

import pytest

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_TS = "2026-09-26T20:15:00+00:00"


def _back_leg(odds: str = "2.5") -> TicketLeg:
    return TicketLeg(
        "event-preload",
        "market-preload",
        "selection-preload",
        Decimal(odds),
        sport="soccer",
        exchange_side="back",
    )


def test_preload_coherent_stake_rewrite_cannot_mint_positive_opening_authority(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    source = PaperBook("100")
    source.open_ticket([_back_leg()], "10", placed_at=_TS)
    source.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tickets"][0]["stake"] = "20"
    payload["balance"] = "80"
    path.write_text(json.dumps(payload), encoding="utf-8")

    # A path load may reject the forged snapshot immediately, or it may return a
    # structural-only object. It must never derive positive opening authority from
    # the same caller-editable bytes it is supposed to authenticate.
    with pytest.raises(ValueError):
        loaded = PaperBook.load(path)
        assert loaded.committed_stake == Decimal("20")


def test_preload_same_quote_odds_rewrite_cannot_reach_settlement_authority(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    source = PaperBook("100")
    ticket = source.open_ticket([_back_leg()], "10", placed_at=_TS)
    source.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tickets"][0]["legs"][0]["locked_odds"] = "100"
    path.write_text(json.dumps(payload), encoding="utf-8")

    # locked_odds is not part of quote_key identity. A coherent pre-load rewrite
    # must therefore be rejected by an independent durable witness, not blessed by
    # rebuilding private authority from the already-modified snapshot.
    with pytest.raises(ValueError):
        loaded = PaperBook.load(path)
        loaded_ticket = loaded.tickets[ticket.ticket_id]
        loaded.settle(
            loaded_ticket.ticket_id,
            {loaded_ticket.legs[0].quote_key},
            settled_at=_TS,
        )
