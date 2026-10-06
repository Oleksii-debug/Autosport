import json
from decimal import Decimal

import pytest

import autosport.domain as domain_module

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


def test_settlement_key_ignores_public_encoding_rebinding(monkeypatch) -> None:
    leg = _leg(_S1)
    expected = leg.settlement_key

    def forbidden(*args, **kwargs):
        raise AssertionError("rebound settlement encoder executed")

    monkeypatch.setattr(domain_module, "_market_settlement_key", forbidden)
    monkeypatch.setattr(domain_module, "_CANONICAL_MARKET_SETTLEMENT_KEY", forbidden)
    monkeypatch.setattr(domain_module.json, "dumps", forbidden)
    monkeypatch.setattr(domain_module.base64, "urlsafe_b64encode", forbidden)
    monkeypatch.setattr(domain_module.hashlib, "sha256", forbidden)
    monkeypatch.setattr(
        domain_module,
        "_CANONICAL_SEMANTIC_IDENTITY",
        forbidden,
    )
    monkeypatch.setattr(
        domain_module,
        "_CANONICAL_REQUIRE_UTF8_ENCODABLE",
        forbidden,
    )
    monkeypatch.setattr(
        domain_module,
        "_CANONICAL_SETTLEMENT_JSON_DUMPS",
        forbidden,
    )
    monkeypatch.setattr(
        domain_module,
        "_CANONICAL_SETTLEMENT_B64ENCODE",
        forbidden,
    )
    monkeypatch.setattr(
        domain_module,
        "_CANONICAL_SETTLEMENT_SHA256",
        forbidden,
    )
    monkeypatch.setattr(
        domain_module,
        "_CANONICAL_SETTLEMENT_SEMANTIC_IDENTITY",
        forbidden,
    )
    monkeypatch.setattr(
        domain_module,
        "_CANONICAL_SETTLEMENT_UTF8",
        forbidden,
    )

    assert leg.settlement_key == expected


def test_market_semantics_produces_distinct_settlement_keys() -> None:
    first = _leg(_S1)
    second = _leg(_S2)

    assert first.quote_key == second.quote_key
    assert first.settlement_key != second.settlement_key
    assert first.settlement_key.startswith("market-semantics-v1-")
    assert len(first.settlement_key) == len("market-semantics-v1-") + 64


def test_legacy_absent_semantics_preserves_quote_key_as_settlement_key() -> None:
    legacy = _leg(None)

    assert legacy.settlement_key == legacy.quote_key


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



def test_ticket_leg_semantics_ignore_public_validator_rebinding(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        domain_module,
        "_canonical_semantic_identity",
        lambda *args, **kwargs: "forged",
    )
    monkeypatch.setattr(
        domain_module,
        "_canonical_string_value",
        lambda *args, **kwargs: "forged",
    )
    monkeypatch.setattr(
        domain_module,
        "_require_utf8_encodable",
        lambda *args, **kwargs: "forged",
    )
    monkeypatch.setattr(
        domain_module,
        "_SEMANTIC_ID_CHARS",
        frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ:"),
    )
    monkeypatch.setattr(
        domain_module,
        "_RESERVED_SEMANTIC_IDENTITIES",
        frozenset(),
    )

    with pytest.raises(ValueError, match="market_semantics_id"):
        _leg("SOCCER:H2H:V1")

    assert _leg(_S1).market_semantics_id == _S1


def test_ticket_leg_rejects_semantic_string_subclass() -> None:
    with pytest.raises(ValueError, match="market_semantics_id"):
        _leg(_SemanticStringSubclass(_S1))


def test_paperbook_settlement_rejects_quote_only_key_for_semantic_leg() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg(_S1)], "10", placed_at=_TS)
    balance_before = book.balance

    with pytest.raises(ValueError, match="unknown winning settlement key"):
        book.settle(
            ticket.ticket_id,
            {ticket.legs[0].quote_key},
            settled_at="2026-09-22T13:00:00+00:00",
        )

    assert ticket.status.value == "open"
    assert book.balance == balance_before


def test_paperbook_settlement_accepts_exact_market_semantics_key() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg(_S1)], "10", placed_at=_TS)

    settled = book.settle(
        ticket.ticket_id,
        {ticket.legs[0].settlement_key},
        settled_at="2026-09-22T13:00:00+00:00",
    )

    assert settled.status.value == "won"
    assert settled.payout == Decimal("21.00")


def test_paperbook_settlement_rejects_other_semantics_revision_key() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg(_S1)], "10", placed_at=_TS)
    wrong_semantics_key = _leg(_S2).settlement_key

    with pytest.raises(ValueError, match="unknown winning settlement key"):
        book.settle(
            ticket.ticket_id,
            {wrong_semantics_key},
            settled_at="2026-09-22T13:00:00+00:00",
        )

    assert ticket.status.value == "open"


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


def test_schema8_multi_leg_round_trip_preserves_independent_semantics(tmp_path) -> None:
    path = tmp_path / "paper-book-multi-semantics.json"
    book = PaperBook("100")
    first = _leg(_S1)
    second = TicketLeg(
        "event-2",
        "market-2",
        "selection-2",
        Decimal("1.80"),
        sport="soccer",
        exchange_side="back",
        market_semantics_id=_S2,
    )
    ticket = book.open_ticket([first, second], "10", placed_at=_TS)
    before = tuple(leg.settlement_identity for leg in ticket.legs)

    book.save(path)
    restored = PaperBook.load(path)
    after = tuple(
        leg.settlement_identity
        for leg in restored.tickets[ticket.ticket_id].legs
    )

    assert after == before
    assert after[0][1] == _S1
    assert after[1][1] == _S2
    assert after[0] != after[1]


def test_schema8_settled_semantics_round_trip_replays_exact_identity(tmp_path) -> None:
    path = tmp_path / "paper-book-settled-semantics.json"
    book = PaperBook("100")
    ticket = book.open_ticket([_leg(_S1)], "10", placed_at=_TS)
    settlement_key = ticket.legs[0].settlement_key
    book.settle(
        ticket.ticket_id,
        {settlement_key},
        settled_at="2026-09-22T13:00:00+00:00",
    )
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    settle_entry = payload["lifecycle"][1]
    assert settle_entry["winning_quote_keys"] == [settlement_key]

    restored = PaperBook.load(path)
    restored_ticket = restored.tickets[ticket.ticket_id]
    assert restored_ticket.status.value == "won"
    assert restored_ticket.payout == Decimal("21.00")
    assert restored_ticket.legs[0].settlement_key == settlement_key


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
        match="market_semantics_id must be canonical",
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
