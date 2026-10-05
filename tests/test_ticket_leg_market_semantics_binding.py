import json
from decimal import Decimal

import pytest

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


_S1 = "soccer:h2h:v1"
_S2 = "soccer:h2h:v2"
_TS = "2026-09-22T12:00:00+00:00"


class _SemanticStringSubclass(str):
    pass


def _leg(semantics: str | None) -> TicketLeg:
    return TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal("2.10"),
        sport="soccer",
        exchange_side="back",
        market_semantics_id=semantics,
    )


def test_same_quote_different_market_semantics_do_not_alias_settlement_identity() -> None:
    first = _leg(_S1)
    second = _leg(_S2)

    assert first.quote_key == second.quote_key
    assert first.settlement_identity != second.settlement_identity


def test_legacy_quote_identity_is_unchanged_by_absent_semantics() -> None:
    legacy = TicketLeg(
        "event-1",
        "market-1",
        "selection-1",
        Decimal("2.10"),
        sport="soccer",
        exchange_side="back",
    )
    explicit_none = _leg(None)

    assert legacy.quote_key == explicit_none.quote_key
    assert legacy.settlement_identity == (legacy.quote_key, None)


@pytest.mark.parametrize(
    "identity",
    [
        "",
        " Soccer:h2h:v1",
        "SOCCER:H2H:V1",
        "soccer|h2h",
        "unknown",
        "mixed",
        "unspecified",
    ],
)
def test_ticket_leg_rejects_noncanonical_market_semantics(identity: str) -> None:
    with pytest.raises(ValueError, match="market_semantics_id"):
        _leg(identity)



def test_ticket_leg_rejects_semantic_string_subclass() -> None:
    with pytest.raises(ValueError, match="market_semantics_id"):
        _leg(_SemanticStringSubclass(_S1))


def test_paperbook_schema8_round_trip_preserves_market_semantics(tmp_path) -> None:
    path = tmp_path / "paper-book.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg(_S1)], "10", placed_at=_TS)
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 8
    assert payload["tickets"][0]["legs"][0]["market_semantics_id"] == _S1

    restored = PaperBook.load(path)
    restored_leg = restored.tickets[ticket.ticket_id].legs[0]
    assert restored_leg.market_semantics_id == _S1
    assert restored_leg.settlement_identity == ticket.legs[0].settlement_identity


def test_schema7_legacy_leg_remains_readable_without_market_semantics(tmp_path) -> None:
    path = tmp_path / "paper-book-v7.json"
    book = PaperBook("100")
    book.open_ticket([_leg(_S1)], "10", placed_at=_TS)
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 7
    payload["tickets"][0]["legs"][0].pop("market_semantics_id")
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = PaperBook.load(path)
    restored_leg = next(iter(restored.tickets.values())).legs[0]
    assert restored_leg.market_semantics_id is None


def test_schema8_requires_explicit_market_semantics_field_even_when_none(tmp_path) -> None:
    path = tmp_path / "paper-book-v8.json"
    book = PaperBook("100")
    book.open_ticket([_leg(None)], "10", placed_at=_TS)
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tickets"][0]["legs"][0].pop("market_semantics_id")
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="market_semantics_id"):
        PaperBook.load(path)


def test_paperbook_opening_authority_rejects_semantics_mutation_before_save(
    tmp_path,
) -> None:
    path = tmp_path / "paper-book-mutated.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg(_S1)], "10", placed_at=_TS)
    object.__setattr__(ticket.legs[0], "market_semantics_id", "SOCCER:H2H:V1")

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        book.save(path)

    assert not path.exists()


def test_valid_semantics_revision_cannot_move_opening_authority(tmp_path) -> None:
    path = tmp_path / "paper-book-semantics-revision.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg(_S1)], "10", placed_at=_TS)
    book.save(path)
    durable_before = path.read_bytes()

    object.__setattr__(ticket.legs[0], "market_semantics_id", _S2)

    with pytest.raises(
        ValueError,
        match="opening economic identity changed after admission",
    ):
        book.save(path)

    assert path.read_bytes() == durable_before


def test_schema7_rejects_injected_market_semantics_field(tmp_path) -> None:
    path = tmp_path / "paper-book-v7-hybrid.json"
    book = PaperBook("100")
    book.open_ticket([_leg(_S1)], "10", placed_at=_TS)
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 7

    with pytest.raises(ValueError, match="unsupported before schema 8"):
        PaperBook.load_bytes(json.dumps(payload).encode("utf-8"))


def test_trusted_schema7_round_trip_upgrades_to_explicit_schema8_none(tmp_path) -> None:
    path = tmp_path / "paper-book-v7-upgrade.json"
    book = PaperBook("100")
    book.open_ticket([_leg(None)], "10", placed_at=_TS)
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 7
    payload["tickets"][0]["legs"][0].pop("market_semantics_id")
    path.write_text(json.dumps(payload), encoding="utf-8")

    # Path authority must correspond to the exact rewritten bytes. Structural loading
    # demonstrates the migration contract without claiming trusted-path provenance.
    legacy = PaperBook.load_bytes(path.read_bytes())
    legacy_leg = next(iter(legacy.tickets.values())).legs[0]
    assert legacy_leg.market_semantics_id is None

    # A product-issued fresh book using the migrated legacy identity publishes v8
    # with explicit null semantics rather than silently retaining a hybrid schema.
    upgraded = PaperBook("100")
    upgraded.open_ticket(
        [
            TicketLeg(
                legacy_leg.event_id,
                legacy_leg.market_id,
                legacy_leg.selection_id,
                legacy_leg.locked_odds,
                sport=legacy_leg.sport,
                exchange_side=legacy_leg.exchange_side,
                market_semantics_id=legacy_leg.market_semantics_id,
            )
        ],
        "10",
        placed_at=_TS,
    )
    upgraded.save(path)
    upgraded_payload = json.loads(path.read_text(encoding="utf-8"))
    assert upgraded_payload["schema_version"] == 8
    assert upgraded_payload["tickets"][0]["legs"][0]["market_semantics_id"] is None
