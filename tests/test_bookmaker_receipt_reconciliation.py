from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.bookmaker_routing import (
    ExternalEffect,
    RoutingContractError,
    RoutingState,
    VenueObservation,
    VenueQuote,
)
from autosport.bookmaker_receipt_reconciliation import (
    bind_leg_receipt,
    reconcile_equal_split_residual,
)
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.opportunity import QuoteRef


_REQUEST_ID = "route-request-receipts-1"
_PLAN_ID = "parent-plan-receipts-1"


def _quote(source: str) -> QuoteRef:
    return QuoteRef(
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        source_id=source,
        sequence=1,
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-17T01:40:00+00:00",
        source_ts=None,
        ingest_ts="2026-09-17T01:40:00+00:00",
        market_event_hash="a" * 64,
    )


def _venue(source: str, account: str, ceiling: str = "100.00") -> VenueQuote:
    return VenueQuote(source, account, _quote(source), Decimal(ceiling))


def _initial(venues: tuple[VenueQuote, ...]):
    return plan_equal_split_residual(
        Decimal("100.00"),
        venues,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )


def _reconcile(
    venues: tuple[VenueQuote, ...],
    receipts,
):
    return reconcile_equal_split_residual(
        Decimal("100.00"),
        venues,
        receipts,
        routing_request_id=_REQUEST_ID,
        parent_plan_id=_PLAN_ID,
        stake_quantum=Decimal("0.01"),
    )


def test_two_child_receipts_complete_one_parent_without_duplicate_ledger() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    assert [leg.proposed_stake for leg in initial.legs] == [
        Decimal("50.00"),
        Decimal("50.00"),
    ]

    receipts = (
        bind_leg_receipt(
            initial.legs[0],
            effect=ExternalEffect.ACCEPTED,
            external_receipt_id="external-a-1",
            confirmed_accepted=Decimal("50.00"),
        ),
        bind_leg_receipt(
            initial.legs[1],
            effect=ExternalEffect.ACCEPTED,
            external_receipt_id="external-b-1",
            confirmed_accepted=Decimal("50.00"),
        ),
    )

    reconciled = _reconcile((a, b), receipts)
    assert reconciled.state is RoutingState.COMPLETE
    assert reconciled.confirmed_total == Decimal("100.00")
    assert reconciled.residual_before == Decimal("0")
    assert reconciled.proposed_total == Decimal("0")
    assert reconciled.legs == ()


def test_exact_external_receipt_replay_is_idempotent() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    receipt = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="external-a-1",
        confirmed_accepted=Decimal("50.00"),
    )

    reconciled = _reconcile((a, b), (receipt, receipt))
    assert reconciled.confirmed_total == Decimal("50.00")
    assert reconciled.residual_before == Decimal("50.00")
    assert reconciled.state is RoutingState.ROUTE


def test_conflicting_reuse_of_provider_receipt_identity_fails_closed() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    receipt = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="external-a-1",
        confirmed_accepted=Decimal("40.00"),
    )
    conflict = replace(receipt, confirmed_accepted=Decimal("50.00"))

    with pytest.raises(RoutingContractError, match="conflicting external receipt"):
        _reconcile((a, b), (receipt, conflict))


def test_receipt_parent_and_canonical_child_identity_are_verified() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    receipt = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="external-a-1",
        confirmed_accepted=Decimal("40.00"),
    )

    with pytest.raises(RoutingContractError, match="parent_plan_id"):
        _reconcile(
            (a, b),
            (replace(receipt, parent_plan_id="different-parent"),),
        )

    with pytest.raises(RoutingContractError, match="canonical child identity"):
        _reconcile(
            (a, b),
            (replace(receipt, proposal_leg_id="b" * 64),),
        )


def test_second_receipt_for_same_child_fails_closed_within_stake() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    first = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="external-a-1",
        confirmed_accepted=Decimal("20.00"),
    )
    second = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="external-a-2",
        confirmed_accepted=Decimal("20.00"),
    )

    with pytest.raises(RoutingContractError, match="multiple external receipts"):
        _reconcile((a, b), (first, second))


def test_child_bound_acceptance_requires_real_external_receipt_identity() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    leg = initial.legs[0]

    with pytest.raises(RoutingContractError, match="requires external_receipt_id"):
        VenueObservation(
            venue_id=leg.venue.venue_id,
            account_id=leg.venue.account_id,
            effect=ExternalEffect.ACCEPTED,
            routing_request_id=leg.routing_request_id,
            quote=leg.venue.quote,
            confirmed_accepted=Decimal("10.00"),
            parent_plan_id=leg.parent_plan_id,
            proposal_leg_id=leg.leg_id,
            proposed_stake=leg.proposed_stake,
        )


def test_unknown_child_effect_blocks_without_inventing_external_receipt() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    unknown = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.UNKNOWN,
    )
    assert unknown.external_receipt_id is None

    reconciled = _reconcile((a, b), (unknown,))
    assert reconciled.state is RoutingState.BLOCKED_UNKNOWN
    assert reconciled.proposed_total == Decimal("0")
    assert reconciled.legs == ()


def test_partial_acceptance_then_refusal_routes_residual_to_other_venue() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    accepted = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="external-a-accepted",
        confirmed_accepted=Decimal("40.00"),
    )
    refused = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.MARKET_REFUSED,
        observation_id="refusal-a-1",
    )
    assert refused.external_receipt_id is None

    reconciled = _reconcile((a, b), (accepted, refused))
    assert reconciled.confirmed_total == Decimal("40.00")
    assert reconciled.residual_before == Decimal("60.00")
    assert reconciled.state is RoutingState.ROUTE
    assert reconciled.proposed_total == Decimal("60.00")
    assert [leg.venue for leg in reconciled.legs] == [b]
    assert [leg.proposed_stake for leg in reconciled.legs] == [Decimal("60.00")]


def test_receipt_bound_partial_refusal_then_residual_acceptance_completes_parent() -> None:
    a, b = _venue("book-a", "acct-a"), _venue("book-b", "acct-b")
    initial = _initial((a, b))
    accepted_a = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="external-a-partial",
        confirmed_accepted=Decimal("40.00"),
    )
    refused_a = bind_leg_receipt(
        initial.legs[0],
        effect=ExternalEffect.MARKET_REFUSED,
        observation_id="refusal-a-after-partial",
    )

    after_a = _reconcile((a, b), (accepted_a, refused_a))
    assert after_a.state is RoutingState.ROUTE
    assert after_a.parent_plan_id == _PLAN_ID
    assert after_a.confirmed_total == Decimal("40.00")
    assert after_a.residual_before == Decimal("60.00")
    assert len(after_a.legs) == 1
    assert after_a.legs[0].venue == b
    assert after_a.legs[0].proposed_stake == Decimal("60.00")

    accepted_b = bind_leg_receipt(
        after_a.legs[0],
        effect=ExternalEffect.ACCEPTED,
        external_receipt_id="external-b-residual",
        confirmed_accepted=Decimal("60.00"),
    )
    final = _reconcile((a, b), (accepted_a, refused_a, accepted_b))
    assert final.state is RoutingState.COMPLETE
    assert final.parent_plan_id == _PLAN_ID
    assert final.confirmed_total == Decimal("100.00")
    assert final.residual_before == Decimal("0")
    assert final.proposed_total == Decimal("0")
    assert final.legs == ()
