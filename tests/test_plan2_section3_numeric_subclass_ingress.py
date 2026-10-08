"""Plan 2 Section 3: exact Decimal financial/quote ingress is not duck typing."""
from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.opportunity import (
    OpportunityContractError,
    Opportunity,
    PlanAllocation,
    QuoteRef,
)


class HostileDecimal(Decimal):
    def is_finite(self):
        raise AssertionError("virtual is_finite must not run")

    def __str__(self):
        raise AssertionError("virtual decimal string must not run")


def _quote(odds: Decimal) -> QuoteRef:
    return QuoteRef(
        event_id="event",
        market_id="market",
        selection_id="selection",
        source_id="recorded",
        sequence=1,
        decimal_odds=odds,
        observed_ts="2026-10-08T12:00:00+00:00",
        source_ts="2026-10-08T11:59:59+00:00",
        ingest_ts="2026-10-08T12:00:01+00:00",
        market_event_hash="a" * 64,
        market_snapshot_hash="b" * 64,
        sport="football",
    )


def test_hostile_decimal_subclass_cannot_supply_financial_stake() -> None:
    with pytest.raises(OpportunityContractError, match="exact finite Decimal"):
        PlanAllocation("c" * 64, HostileDecimal("1.25"))


def test_hostile_decimal_subclass_cannot_supply_market_odds() -> None:
    with pytest.raises(OpportunityContractError, match="exact finite Decimal"):
        _quote(HostileDecimal("2.05"))


def test_exact_decimal_values_keep_canonical_roundtrip() -> None:
    allocation = PlanAllocation("c" * 64, Decimal("0.0000000001"))
    assert PlanAllocation.from_dict(allocation.to_dict()) == allocation
    quote = _quote(Decimal("2.05"))
    assert QuoteRef.from_dict(quote.to_dict()) == quote


def test_hostile_decimal_subclass_cannot_override_intent_signal_ingress() -> None:
    """Fail before invoking untrusted numeric methods or nested typed witnesses."""
    from autosport.portfolio_plan import OpportunityEvidence, OpportunityIntent
    from autosport.risk import ProposedTicketRiskContext

    # Intentionally uninitialized *exact* typed placeholders: all are checked
    # for nominal type before signal validation and must not be dereferenced.
    with pytest.raises(ValueError, match="signal_strength must be a finite exact Decimal"):
        OpportunityIntent(
            intent_id="hostile-signal",
            opportunity=object.__new__(Opportunity),
            evidence=object.__new__(OpportunityEvidence),
            risk_context=object.__new__(ProposedTicketRiskContext),
            signal_strength=HostileDecimal("0.20"),
            strategy_id="recorded-signal",
            config_sha256="a" * 64,
        )
