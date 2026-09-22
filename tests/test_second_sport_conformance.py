from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.domain import MarketEvent, MarketType, TicketLeg, TicketStatus
from autosport.evaluation import evaluate
from autosport.market_mirror import MarketMirror, MirrorUpdate
from autosport.market_outcomes import (
    MarketOutcomeIdentity,
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    OutcomeRosterBasis,
    SettlementResult,
    SettlementSemantics,
    _VERIFIED_AUTHORITY_TOKEN,
    assess_market_outcome_authority,
)
from autosport.paper import PaperBook
from autosport.scenario_search import ScenarioSearchEngine
from autosport.settlement import SettlementEngine


FIXTURE_SOURCE_ID = "test_fixture_only:football_three_way"
DECISION_AS_OF = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _event(*, sport: str, selection_id: str, sequence: int = 1) -> MarketEvent:
    return MarketEvent(
        event_id="shared-event",
        market_id="shared-match-odds",
        selection_id=selection_id,
        decimal_odds=Decimal("2.50"),
        observed_ts="2026-09-22T05:59:00+00:00",
        source_id="fixture-source",
        sequence=sequence,
        market_type=MarketType.WINNER,
        sport=sport,
        market_semantics_id=f"{sport}.winner.three_way"
        if sport == "football"
        else f"{sport}.winner",
        provider_source_class="test.fixture",
    )


def _football_identity() -> MarketOutcomeIdentity:
    return MarketOutcomeIdentity(
        sport="football",
        event_id="shared-event",
        market_id="shared-match-odds",
        source_id=FIXTURE_SOURCE_ID,
        market_type=MarketType.WINNER,
    )


def _football_authority() -> MarketSettlementOutcomeAuthority:
    # This is deliberately a TEST_FIXTURE_ONLY authority. Production callers cannot
    # obtain the private verification token; the test below also proves the public raw
    # caller assessment remains refused rather than becoming provider truth.
    return MarketSettlementOutcomeAuthority(
        identity=_football_identity(),
        selection_ids=("away", "draw", "home"),
        roster_basis=OutcomeRosterBasis.GOVERNED_DATASET_MARKET_DEFINITION,
        settlement_semantics=SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID,
        source_revision="test-fixture-football-three-way-v1",
        causal_cutoff="2026-09-22T05:58:00Z",
        observed_at="2026-09-22T05:59:00Z",
        roster_provenance_sha256=SHA_A,
        settlement_rules_sha256=SHA_B,
        verification_protocol_sha256=SHA_C,
        _verification_token=_VERIFIED_AUTHORITY_TOKEN,
    )


def _leg(selection_id: str, odds: str) -> TicketLeg:
    return TicketLeg(
        "shared-event",
        "shared-match-odds",
        selection_id,
        Decimal(odds),
        sport="football",
    )


def test_same_provider_local_ids_are_disjoint_across_sports_and_mirror_views() -> None:
    football = _event(sport="football", selection_id="home")
    table_tennis = _event(sport="table_tennis", selection_id="home")

    assert football.quote_key != table_tennis.quote_key
    assert football.dedupe_key != table_tennis.dedupe_key
    assert MarketEvent.from_dict(football.to_dict()) == football
    assert MarketEvent.from_dict(table_tennis.to_dict()) == table_tennis

    mirror = MarketMirror()
    assert mirror.apply(football).status is MirrorUpdate.APPLIED
    assert mirror.apply(table_tennis).status is MirrorUpdate.APPLIED
    assert len(mirror.view().events) == 2
    assert mirror.view(sports="football").events == (football,)
    assert mirror.view(sports="table_tennis").events == (table_tennis,)


def test_three_way_second_sport_terminal_space_is_exact_and_keeps_draw() -> None:
    authority = _football_authority()

    assert authority.identity.sport == "football"
    assert authority.selection_ids == ("away", "draw", "home")
    assert authority.terminal_space_exact is True
    assert authority.terminal_state_count == 4
    assert tuple(state.state_id for state in authority.terminal_states) == (
        "winner:away",
        "winner:draw",
        "winner:home",
        "all_void",
    )

    draw_state = next(
        state for state in authority.terminal_states if state.state_id == "winner:draw"
    )
    assert dict(draw_state.settlements) == {
        "away": SettlementResult.LOSS,
        "draw": SettlementResult.WIN,
        "home": SettlementResult.LOSS,
    }


def test_public_raw_caller_cannot_mint_second_sport_exhaustive_authority() -> None:
    assessment = assess_market_outcome_authority(
        identity=_football_identity(),
        selection_ids=("away", "draw", "home"),
        roster_basis=OutcomeRosterBasis.GOVERNED_DATASET_MARKET_DEFINITION,
        settlement_semantics=SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID,
        source_revision="caller-asserted-revision",
        causal_cutoff="2026-09-22T05:58:00Z",
        observed_at="2026-09-22T05:59:00Z",
        roster_provenance_sha256=SHA_A,
        settlement_rules_sha256=SHA_B,
        verification_protocol_sha256=SHA_C,
    )

    assert assessment.status is OutcomeAuthorityStatus.REFUSED
    assert assessment.authority is None
    assert assessment.refusal_reason == "caller_supplied_roster_has_no_verified_revision_evidence"


def test_authoritative_scenario_search_does_not_collapse_three_way_market() -> None:
    authority = _football_authority()
    book = PaperBook("100")
    draw_ticket = book.open_ticket(
        [_leg("draw", "3.50")],
        "10",
        placed_at="2026-09-22T05:59:30Z",
        provider_source_ids=(FIXTURE_SOURCE_ID,),
    )
    home_ticket = book.open_ticket(
        [_leg("home", "2.10")],
        "10",
        placed_at="2026-09-22T05:59:31Z",
        provider_source_ids=(FIXTURE_SOURCE_ID,),
    )

    report = ScenarioSearchEngine().analyse_authoritative(
        [draw_ticket, home_ticket],
        [authority],
        decision_as_of=DECISION_AS_OF,
    )

    assert report.mode == "authoritative-exact-enumeration"
    assert report.total_states == 4
    assert report.nodes_explored == 4
    assert report.outcome_space_exhaustive is True
    assert report.outcome_space_exact is True
    assert report.worst_proven is True
    assert report.best_proven is True
    assert report.outcome_authority_sha256s == (authority.authority_sha256,)


def test_cross_sport_ticket_cannot_reuse_second_sport_outcome_authority() -> None:
    authority = _football_authority()
    book = PaperBook("100")
    table_tennis_leg = TicketLeg(
        "shared-event",
        "shared-match-odds",
        "draw",
        Decimal("3.50"),
        sport="table_tennis",
    )
    ticket = book.open_ticket(
        [table_tennis_leg],
        "10",
        placed_at="2026-09-22T05:59:30Z",
        provider_source_ids=(FIXTURE_SOURCE_ID,),
    )

    with pytest.raises(
        ValueError,
        match="ticket leg missing from authoritative outcome universe",
    ):
        ScenarioSearchEngine().analyse_authoritative(
            [ticket],
            [authority],
            decision_as_of=DECISION_AS_OF,
        )


def test_draw_settlement_and_restart_preserve_second_sport_identity(tmp_path) -> None:
    authority = _football_authority()
    draw_state = next(
        state for state in authority.terminal_states if state.state_id == "winner:draw"
    )
    draw_ticket_leg = _leg("draw", "3.50")
    home_ticket_leg = _leg("home", "2.10")

    book = PaperBook("100")
    draw_ticket = book.open_ticket(
        [draw_ticket_leg],
        "10",
        placed_at="2026-09-22T05:59:30Z",
        provider_source_ids=(FIXTURE_SOURCE_ID,),
    )
    home_ticket = book.open_ticket(
        [home_ticket_leg],
        "10",
        placed_at="2026-09-22T05:59:31Z",
        provider_source_ids=(FIXTURE_SOURCE_ID,),
    )

    settlements = authority.settlement_by_quote(draw_state)
    engine = SettlementEngine()
    engine.record(settlements)
    settled = engine.settle_ready(book)

    assert set(settled) == {draw_ticket.ticket_id, home_ticket.ticket_id}
    assert draw_ticket.status is TicketStatus.WON
    assert home_ticket.status is TicketStatus.LOST
    assert draw_ticket.payout == Decimal("35.00")
    assert book.balance == Decimal("115.00")

    snapshot = tmp_path / "second-sport-book.json"
    book.save(snapshot)
    restored = PaperBook.load(snapshot)

    restored_draw = restored.tickets[draw_ticket.ticket_id]
    restored_home = restored.tickets[home_ticket.ticket_id]
    assert restored_draw.legs[0].sport == "football"
    assert restored_home.legs[0].sport == "football"
    assert restored_draw.legs[0].quote_key == draw_ticket_leg.quote_key
    assert restored_home.legs[0].quote_key == home_ticket_leg.quote_key

    summary = evaluate(restored)
    assert summary.won == 1
    assert summary.lost == 1
    assert summary.void == 0
    assert summary.final_balance == Decimal("115.00")


def test_fixture_source_is_explicitly_non_external_and_non_execution() -> None:
    authority = _football_authority()

    assert authority.identity.source_id.startswith("test_fixture_only:")
    assert "provider" not in authority.identity.source_id
    assert "live" not in authority.identity.source_id
    assert authority.identity.sport == "football"
