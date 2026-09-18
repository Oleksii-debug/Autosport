from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from autosport.domain import PaperTicket, TicketLeg
from autosport.market_outcome_universe import (
    AuthorityKind,
    MarketAuthorityEvidence,
    MarketLifecycle,
    MarketOutcomeSelection,
    MarketOutcomeUniverse,
    MarketTerminalState,
    OutcomeAvailability,
    OutcomeUniverseError,
    SettlementResolution,
    TerminalResolution,
)
from autosport.scenario_search import ScenarioSearchEngine


_DECISION_TS = "2026-09-18T10:05:00Z"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _authority(
    *,
    source_id: str = "provider-a",
    source_ts: str = "2026-09-18T10:01:00Z",
    observed_ts: str = "2026-09-18T10:02:00Z",
    roster_complete: bool = True,
    settlement_complete: bool = True,
) -> MarketAuthorityEvidence:
    return MarketAuthorityEvidence(
        provider_source_id=source_id,
        source_revision="revision-17",
        source_sequence=17,
        source_ts=source_ts,
        observed_ts=observed_ts,
        payload_sha256=_digest(f"{source_id}|revision-17"),
        authority_kind=AuthorityKind.PROVIDER_MARKET_DEFINITION_WITH_SETTLEMENT_RULES,
        roster_complete=roster_complete,
        settlement_semantics_complete=settlement_complete,
    )


def _winner_state(
    state_id: str,
    *,
    winner: str,
    active: tuple[str, ...],
    removed: tuple[str, ...] = (),
) -> MarketTerminalState:
    resolutions = []
    for selection_id in active:
        resolutions.append(
            TerminalResolution(
                selection_id=selection_id,
                resolution=(
                    SettlementResolution.WIN
                    if selection_id == winner
                    else SettlementResolution.LOSS
                ),
            )
        )
    for selection_id in removed:
        resolutions.append(
            TerminalResolution(
                selection_id=selection_id,
                resolution=SettlementResolution.VOID,
            )
        )
    return MarketTerminalState(
        state_id=state_id,
        resolutions=tuple(resolutions),
    )


def _universe(
    *,
    active: tuple[str, ...] = ("home", "away"),
    removed: tuple[str, ...] = (),
    source_id: str = "provider-a",
    market_status: MarketLifecycle = MarketLifecycle.OPEN,
    authority: MarketAuthorityEvidence | None = None,
) -> MarketOutcomeUniverse:
    authority = authority or _authority(source_id=source_id)
    selections = tuple(
        MarketOutcomeSelection(
            selection_id=selection_id,
            availability=OutcomeAvailability.ACTIVE,
            lifecycle_ts="2026-09-18T10:02:00Z",
        )
        for selection_id in active
    ) + tuple(
        MarketOutcomeSelection(
            selection_id=selection_id,
            availability=OutcomeAvailability.REMOVED,
            lifecycle_ts="2026-09-18T10:02:00Z",
        )
        for selection_id in removed
    )
    states = ()
    if authority.settlement_semantics_complete:
        states = tuple(
            _winner_state(
                f"{winner}-wins",
                winner=winner,
                active=active,
                removed=removed,
            )
            for winner in active
        )
    return MarketOutcomeUniverse(
        sport="tennis",
        event_id="event-1",
        market_id="market-1",
        market_status=market_status,
        authority=authority,
        selections=selections,
        terminal_states=states,
    )


def _ticket(selection_id: str = "home") -> PaperTicket:
    return PaperTicket(
        ticket_id=f"ticket-{selection_id}",
        stake=Decimal("10"),
        legs=(
            TicketLeg(
                sport="tennis",
                event_id="event-1",
                market_id="market-1",
                selection_id=selection_id,
                locked_odds=Decimal("2"),
            ),
        ),
        placed_at="2026-09-18T10:03:00Z",
    )


def test_authoritative_universe_roundtrip_binds_exact_contents() -> None:
    universe = _universe(active=("home", "draw", "away"))

    raw = universe.to_dict()
    restored = MarketOutcomeUniverse.from_dict(raw)

    assert restored == universe
    assert restored.universe_id == universe.universe_id
    assert restored.to_dict() == raw

    tampered = dict(raw)
    tampered["market_id"] = "market-2"
    with pytest.raises(OutcomeUniverseError, match="universe_id"):
        MarketOutcomeUniverse.from_dict(tampered)


def test_authoritative_scenario_search_derives_complete_group() -> None:
    universe = _universe()
    report = ScenarioSearchEngine().analyse_authoritative(
        [_ticket("home")],
        [universe],
        decision_ts=_DECISION_TS,
    )

    assert report.mode == "exact-enumeration"
    assert report.total_states == 2
    assert report.worst_proven is True
    assert report.best_proven is True
    assert report.observed_worst == Decimal("-10")
    assert report.observed_best == Decimal("10")


def test_omitted_real_outcome_cannot_validate_as_authoritative() -> None:
    universe = _universe(active=("home", "draw", "away"))
    authoritative = universe.scenario_quote_keys(decision_ts=_DECISION_TS)

    with pytest.raises(OutcomeUniverseError, match="missing="):
        universe.validate_scenario_quote_keys(
            set(authoritative[:2]),
            decision_ts=_DECISION_TS,
        )


def test_removed_selection_remains_in_roster_but_is_void_and_not_scenario_winner() -> None:
    universe = _universe(active=("home", "away"), removed=("withdrawn",))

    quote_keys = universe.scenario_quote_keys(decision_ts=_DECISION_TS)
    removed_key = universe.quote_key("withdrawn")
    assert removed_key not in quote_keys

    settlement = universe.settlement_outcomes(
        "home-wins",
        decision_ts=_DECISION_TS,
    )
    assert settlement[removed_key] == "void"


def test_removed_selection_nonvoid_terminal_semantics_are_rejected() -> None:
    authority = _authority()
    selections = (
        MarketOutcomeSelection(
            "home",
            OutcomeAvailability.ACTIVE,
            "2026-09-18T10:02:00Z",
        ),
        MarketOutcomeSelection(
            "away",
            OutcomeAvailability.ACTIVE,
            "2026-09-18T10:02:00Z",
        ),
        MarketOutcomeSelection(
            "withdrawn",
            OutcomeAvailability.REMOVED,
            "2026-09-18T10:02:00Z",
        ),
    )
    state = MarketTerminalState(
        "home-wins",
        (
            TerminalResolution("home", SettlementResolution.WIN),
            TerminalResolution("away", SettlementResolution.LOSS),
            TerminalResolution("withdrawn", SettlementResolution.LOSS),
        ),
    )

    with pytest.raises(OutcomeUniverseError, match="removed selections"):
        MarketOutcomeUniverse(
            sport="tennis",
            event_id="event-1",
            market_id="market-1",
            market_status=MarketLifecycle.OPEN,
            authority=authority,
            selections=selections,
            terminal_states=(state,),
        )


def test_void_or_cancellation_semantics_fail_closed_for_scenario_projection() -> None:
    authority = _authority()
    selections = (
        MarketOutcomeSelection(
            "home",
            OutcomeAvailability.ACTIVE,
            "2026-09-18T10:02:00Z",
        ),
        MarketOutcomeSelection(
            "away",
            OutcomeAvailability.ACTIVE,
            "2026-09-18T10:02:00Z",
        ),
    )
    cancellation = MarketTerminalState(
        "cancelled",
        (
            TerminalResolution("home", SettlementResolution.VOID),
            TerminalResolution("away", SettlementResolution.VOID),
        ),
    )
    universe = MarketOutcomeUniverse(
        sport="tennis",
        event_id="event-1",
        market_id="market-1",
        market_status=MarketLifecycle.OPEN,
        authority=authority,
        selections=selections,
        terminal_states=(cancellation,),
    )

    with pytest.raises(OutcomeUniverseError, match="one exhaustive ScenarioGroup winner"):
        universe.scenario_quote_keys(decision_ts=_DECISION_TS)


def test_future_provider_or_lifecycle_evidence_cannot_drive_decision() -> None:
    future = _universe(
        authority=_authority(
            source_ts="2026-09-18T10:06:00Z",
            observed_ts="2026-09-18T10:07:00Z",
        )
    )

    with pytest.raises(OutcomeUniverseError, match="future"):
        future.scenario_quote_keys(decision_ts=_DECISION_TS)


def test_incomplete_authority_refuses_exhaustive_projection() -> None:
    incomplete = _universe(
        authority=_authority(
            roster_complete=False,
            settlement_complete=False,
        )
    )

    with pytest.raises(OutcomeUniverseError, match="must both be complete"):
        incomplete.scenario_quote_keys(decision_ts=_DECISION_TS)


def test_cross_provider_same_market_authority_is_rejected() -> None:
    first = _universe(source_id="provider-a")
    second = _universe(source_id="provider-b")

    with pytest.raises(ValueError, match="conflicting authoritative providers"):
        ScenarioSearchEngine().analyse_authoritative(
            [_ticket()],
            [first, second],
            decision_ts=_DECISION_TS,
        )


def test_unrelated_authoritative_market_is_not_silently_added_to_state_space() -> None:
    first = _universe()
    unrelated = MarketOutcomeUniverse(
        sport="tennis",
        event_id="event-2",
        market_id="market-2",
        market_status=MarketLifecycle.OPEN,
        authority=_authority(source_id="provider-b"),
        selections=(
            MarketOutcomeSelection(
                "p1",
                OutcomeAvailability.ACTIVE,
                "2026-09-18T10:02:00Z",
            ),
            MarketOutcomeSelection(
                "p2",
                OutcomeAvailability.ACTIVE,
                "2026-09-18T10:02:00Z",
            ),
        ),
        terminal_states=(
            MarketTerminalState(
                "p1-wins",
                (
                    TerminalResolution("p1", SettlementResolution.WIN),
                    TerminalResolution("p2", SettlementResolution.LOSS),
                ),
            ),
            MarketTerminalState(
                "p2-wins",
                (
                    TerminalResolution("p1", SettlementResolution.LOSS),
                    TerminalResolution("p2", SettlementResolution.WIN),
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="unrelated"):
        ScenarioSearchEngine().analyse_authoritative(
            [_ticket()],
            [first, unrelated],
            decision_ts=_DECISION_TS,
        )
