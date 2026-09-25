from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autosport.domain import MarketEvent, TicketLeg
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExposureBinding
from autosport.paper_strategy import PaperValueAgent
from autosport.risk import ProposedTicketRiskContext, RiskOfRuinEvidence


_TS = "2026-09-22T12:00:00+00:00"
_S1 = "soccer:h2h:v1"
_S2 = "soccer:h2h:v2"


def _event(semantics: str | None) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2.10"),
        observed_ts=_TS,
        source_id="provider-1",
        sequence=1,
        ingest_ts=_TS,
        sport="soccer",
        exchange_side="back",
        market_semantics_id=semantics,
    )


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


@pytest.mark.parametrize(
    "identity",
    ["", " Soccer:h2h:v1", "SOCCER:H2H:V1", "soccer|h2h", "unknown", "mixed", "unspecified"],
)
def test_ticket_leg_rejects_noncanonical_market_semantics(identity: str) -> None:
    with pytest.raises(ValueError, match="market_semantics_id"):
        _leg(identity)


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


def test_schema7_legacy_leg_remains_readable_without_modern_semantics(tmp_path) -> None:
    path = tmp_path / "paper-book-v7.json"
    book = PaperBook("100")
    book.open_ticket([_leg(_S1)], "10", placed_at=_TS)
    book.save(path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 7
    payload["tickets"][0]["legs"][0].pop("market_semantics_id")
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = PaperBook.load(path)
    assert next(iter(restored.tickets.values())).legs[0].market_semantics_id is None


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


def test_risk_context_requires_exact_market_semantics_match() -> None:
    exact = _leg(_S1)
    quote = _event(_S1)

    context = ProposedTicketRiskContext(legs=(exact,), quotes=(quote,))
    assert context.legs[0].market_semantics_id == _S1

    with pytest.raises(ValueError, match="market semantics"):
        ProposedTicketRiskContext(legs=(_leg(None),), quotes=(quote,))

    with pytest.raises(ValueError, match="market semantics"):
        ProposedTicketRiskContext(legs=(_leg(_S2),), quotes=(quote,))


def test_risk_context_rejects_risk_of_ruin_evidence_subclass() -> None:
    class DerivedRiskOfRuinEvidence(RiskOfRuinEvidence):
        pass

    evidence = DerivedRiskOfRuinEvidence(
        evidence_id="risk-evidence-1",
        research_protocol_sha256="a" * 64,
        reproducibility_bundle_sha256="b" * 64,
        producer_identity="test-producer",
        causal_cutoff="2026-09-22T11:59:58+00:00",
        evaluated_at="2026-09-22T11:59:59+00:00",
        bankroll_id="paper-bankroll",
        currency="EUR",
        base_portfolio_sha256="c" * 64,
        candidate_sha256="d" * 64,
        evaluated_stake=Decimal("10"),
        upper_bound=Decimal("0.01"),
    )

    with pytest.raises(
        ValueError,
        match="risk_of_ruin_evidence must be canonical RiskOfRuinEvidence",
    ):
        ProposedTicketRiskContext(
            legs=(_leg(None),),
            quotes=(_event(None),),
            risk_of_ruin_evidence=evidence,
        )


def test_paper_value_material_action_identity_binds_market_semantics() -> None:
    context = SimpleNamespace(replay_run_id="replay-1")

    first = PaperValueAgent._material_action_id(context, _event(_S1))
    second = PaperValueAgent._material_action_id(context, _event(_S2))

    assert first != second


def test_paper_value_material_action_identity_preserves_legacy_none_payload() -> None:
    context = SimpleNamespace(replay_run_id="replay-1")
    legacy_payload = {
        "schema": "autosport.paper-value.open-ticket.v1",
        "replay_run_id": "replay-1",
        "agent": "paper-value-strategy",
        "action": "OPEN_PAPER_VALUE_TICKET",
        "quote_key": _event(None).quote_key,
    }
    expected = hashlib.sha256(
        json.dumps(
            legacy_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    assert PaperValueAgent._material_action_id(context, _event(None)) == expected


def test_paper_value_restart_matcher_rejects_semantics_substitution() -> None:
    book = PaperBook("100")
    ticket = book.open_ticket([_leg(_S1)], "10", placed_at=_TS)

    assert PaperValueAgent._ticket_matches_event(ticket, _event(_S1))
    assert not PaperValueAgent._ticket_matches_event(ticket, _event(_S2))


def test_paper_exposure_binding_carries_only_canonical_semantics() -> None:
    binding = PaperExposureBinding(
        action_id="action-1",
        sport="soccer",
        bankroll_id="paper-bankroll",
        currency="EUR",
        market_semantics_id=_S1,
    )
    assert binding.market_semantics_id == _S1

    with pytest.raises(ValueError, match="market_semantics_id"):
        PaperExposureBinding(
            action_id="action-1",
            sport="soccer",
            bankroll_id="paper-bankroll",
            currency="EUR",
            market_semantics_id="Soccer:H2H:V1",
        )
