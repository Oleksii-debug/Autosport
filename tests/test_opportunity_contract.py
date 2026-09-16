from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

from autosport.domain import MarketEvent
from autosport.forecasting import ForecastRecord
from autosport.opportunity import (
    EvidenceRef,
    ForecastRef,
    Opportunity,
    OpportunityContractError,
    OpportunityDecision,
    OpportunitySet,
    PlanAllocation,
    PortfolioPlan,
    QuoteRef,
    StrategyClass,
)


_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _event(
    *,
    event_id: str = "event-1",
    market_id: str = "market-1",
    selection_id: str = "selection-1",
    source_id: str = "provider-a",
    sequence: int = 7,
    odds: str = "2.10",
    metadata: dict[str, object] | None = None,
) -> MarketEvent:
    return MarketEvent(
        event_id=event_id,
        market_id=market_id,
        selection_id=selection_id,
        decimal_odds=Decimal(odds),
        observed_ts="2026-09-16T16:00:00+00:00",
        source_id=source_id,
        sequence=sequence,
        source_ts="2026-09-16T15:59:59+00:00",
        ingest_ts="2026-09-16T16:00:01+00:00",
        metadata=metadata or {"provider_market": "soccer-match-odds"},
    )


def _forecast(
    quote_key: str,
    *,
    forecast_id: str = "forecast-1",
    probability: str = "0.55",
) -> ForecastRecord:
    return ForecastRecord(
        quote_key=quote_key,
        probability=Decimal(probability),
        model_id="model-a",
        model_version="1",
        strategy_version="strategy-1",
        model_training_cutoff_ts="2026-09-01T00:00:00+00:00",
        input_cutoff_ts="2026-09-16T15:59:58+00:00",
        generated_at="2026-09-16T15:59:59+00:00",
        evidence_hashes=(_SHA_A,),
        market_snapshot_hash=_SHA_B,
        provenance={"split": "live-causal"},
        forecast_id=forecast_id,
    )


def _evidence() -> tuple[EvidenceRef, EvidenceRef, EvidenceRef]:
    return (
        EvidenceRef("PortfolioEngine", "scenario-report:run-1"),
        EvidenceRef("PaperRiskPolicy", "risk-decision:run-1"),
        EvidenceRef("PaperBook", "ledger-state:run-1"),
    )


def _predictive_opportunity(
    *,
    decision: OpportunityDecision = OpportunityDecision.ACTIONABLE,
) -> Opportunity:
    quote = QuoteRef.from_market_event(_event())
    forecast = ForecastRef.from_forecast(_forecast(quote.quote_key))
    return Opportunity(
        strategy_class=StrategyClass.PREDICTIVE,
        decision=decision,
        quotes=(quote,),
        forecasts=(forecast,),
    )


def test_predictive_plan_round_trip_is_stable_and_serialization_safe() -> None:
    opportunity = _predictive_opportunity()
    opportunity_set = OpportunitySet((opportunity,))
    portfolio_ref, risk_ref, ledger_ref = _evidence()
    plan = PortfolioPlan(
        opportunity_set=opportunity_set,
        allocations=(
            PlanAllocation(opportunity.opportunity_id, Decimal("12.50")),
        ),
        portfolio_evidence_refs=(portfolio_ref,),
        risk_evidence_refs=(risk_ref,),
        ledger_state_refs=(ledger_ref,),
    )

    payload = plan.to_dict()
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    restored = PortfolioPlan.from_dict(json.loads(serialized))

    assert restored == plan
    assert restored.plan_id == plan.plan_id
    assert restored.opportunity_set.opportunity_set_id == opportunity_set.opportunity_set_id
    assert restored.opportunity_set.opportunities[0].opportunity_id == opportunity.opportunity_id
    assert payload["allocations"][0]["stake"] == "12.50"


def test_nonpredictive_strategy_does_not_require_or_synthesize_forecast() -> None:
    quote = QuoteRef.from_market_event(_event())
    opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.WAIT,
        quotes=(quote,),
    )

    assert opportunity.forecasts == ()
    assert opportunity.to_dict()["forecasts"] == []
    assert Opportunity.from_dict(opportunity.to_dict()) == opportunity


def test_predictive_opportunity_requires_forecast_for_every_quote() -> None:
    first = QuoteRef.from_market_event(_event())
    second = QuoteRef.from_market_event(
        _event(
            event_id="event-2",
            market_id="market-2",
            selection_id="selection-2",
            sequence=8,
        )
    )
    forecast = ForecastRef.from_forecast(_forecast(first.quote_key))

    with pytest.raises(
        OpportunityContractError,
        match="requires forecast evidence for every quote",
    ):
        Opportunity(
            strategy_class=StrategyClass.PREDICTIVE,
            decision=OpportunityDecision.ACTIONABLE,
            quotes=(first, second),
            forecasts=(forecast,),
        )


def test_forecast_for_outside_quote_fails_closed() -> None:
    quote = QuoteRef.from_market_event(_event())
    outside_event = _event(
        event_id="event-2",
        market_id="market-2",
        selection_id="selection-2",
    )
    outside = ForecastRef.from_forecast(
        _forecast(outside_event.quote_key, forecast_id="forecast-outside")
    )

    with pytest.raises(
        OpportunityContractError,
        match="does not belong to an opportunity quote",
    ):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(quote,),
            forecasts=(outside,),
        )


def test_quote_reference_preserves_source_sequence_and_full_event_hash() -> None:
    first = QuoteRef.from_market_event(_event())
    later = QuoteRef.from_market_event(
        _event(sequence=8, metadata={"provider_market": "changed"})
    )

    assert first.quote_key == later.quote_key
    assert first.identity_key != later.identity_key
    assert first.market_event_hash != later.market_event_hash

    with pytest.raises(
        OpportunityContractError,
        match="quote serialization is ambiguous",
    ):
        Opportunity(
            strategy_class=StrategyClass.LIVE_MOVEMENT,
            decision=OpportunityDecision.WAIT,
            quotes=(first, later),
        )


def test_opportunity_set_order_is_deterministic_and_duplicates_fail_closed() -> None:
    first = _predictive_opportunity()
    second_quote = QuoteRef.from_market_event(
        _event(
            event_id="event-2",
            market_id="market-2",
            selection_id="selection-2",
        )
    )
    second = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ZERO,
        quotes=(second_quote,),
    )

    left = OpportunitySet((first, second))
    right = OpportunitySet((second, first))

    assert left == right
    assert left.opportunity_set_id == right.opportunity_set_id
    assert left.to_dict() == right.to_dict()

    with pytest.raises(
        OpportunityContractError,
        match="duplicate members",
    ):
        OpportunitySet((first, first))


def test_positive_allocation_requires_external_authority_evidence() -> None:
    opportunity = _predictive_opportunity()
    opportunity_set = OpportunitySet((opportunity,))
    allocation = PlanAllocation(opportunity.opportunity_id, Decimal("1"))
    portfolio_ref, risk_ref, ledger_ref = _evidence()

    with pytest.raises(
        OpportunityContractError,
        match="requires portfolio, risk, and ledger-state evidence",
    ):
        PortfolioPlan(
            opportunity_set=opportunity_set,
            allocations=(allocation,),
            portfolio_evidence_refs=(portfolio_ref,),
            risk_evidence_refs=(risk_ref,),
        )

    plan = PortfolioPlan(
        opportunity_set=opportunity_set,
        allocations=(allocation,),
        portfolio_evidence_refs=(portfolio_ref,),
        risk_evidence_refs=(risk_ref,),
        ledger_state_refs=(ledger_ref,),
    )
    assert plan.allocations[0].stake == Decimal("1")


@pytest.mark.parametrize(
    "decision",
    (OpportunityDecision.WAIT, OpportunityDecision.ZERO),
)
def test_wait_and_zero_opportunities_cannot_receive_positive_stake(
    decision: OpportunityDecision,
) -> None:
    opportunity = _predictive_opportunity(decision=decision)
    portfolio_ref, risk_ref, ledger_ref = _evidence()

    with pytest.raises(
        OpportunityContractError,
        match="cannot receive positive stake",
    ):
        PortfolioPlan(
            opportunity_set=OpportunitySet((opportunity,)),
            allocations=(
                PlanAllocation(opportunity.opportunity_id, Decimal("0.01")),
            ),
            portfolio_evidence_refs=(portfolio_ref,),
            risk_evidence_refs=(risk_ref,),
            ledger_state_refs=(ledger_ref,),
        )

    zero_plan = PortfolioPlan(
        opportunity_set=OpportunitySet((opportunity,)),
        allocations=(
            PlanAllocation(opportunity.opportunity_id, Decimal("0")),
        ),
    )
    assert zero_plan.allocations[0].stake == Decimal("0")


def test_plan_rejects_unknown_member_and_duplicate_allocation() -> None:
    opportunity = _predictive_opportunity()
    opportunity_set = OpportunitySet((opportunity,))
    portfolio_ref, risk_ref, ledger_ref = _evidence()
    evidence = {
        "portfolio_evidence_refs": (portfolio_ref,),
        "risk_evidence_refs": (risk_ref,),
        "ledger_state_refs": (ledger_ref,),
    }

    with pytest.raises(
        OpportunityContractError,
        match="outside its canonical set",
    ):
        PortfolioPlan(
            opportunity_set=opportunity_set,
            allocations=(PlanAllocation("f" * 64, Decimal("1")),),
            **evidence,
        )

    allocation = PlanAllocation(opportunity.opportunity_id, Decimal("1"))
    with pytest.raises(
        OpportunityContractError,
        match="duplicate allocations",
    ):
        PortfolioPlan(
            opportunity_set=opportunity_set,
            allocations=(allocation, allocation),
            **evidence,
        )


def test_serialized_tampering_and_noncanonical_decimal_fail_closed() -> None:
    opportunity = _predictive_opportunity()
    opportunity_set = OpportunitySet((opportunity,))
    portfolio_ref, risk_ref, ledger_ref = _evidence()
    plan = PortfolioPlan(
        opportunity_set=opportunity_set,
        allocations=(
            PlanAllocation(opportunity.opportunity_id, Decimal("1")),
        ),
        portfolio_evidence_refs=(portfolio_ref,),
        risk_evidence_refs=(risk_ref,),
        ledger_state_refs=(ledger_ref,),
    )

    tampered = copy.deepcopy(plan.to_dict())
    tampered["opportunity_set"]["opportunities"][0]["decision"] = "wait"
    with pytest.raises(
        OpportunityContractError,
        match="opportunity_id does not match canonical contents",
    ):
        PortfolioPlan.from_dict(tampered)

    malformed_decimal = copy.deepcopy(plan.to_dict())
    malformed_decimal["allocations"][0]["stake"] = "01"
    with pytest.raises(
        OpportunityContractError,
        match="canonical finite Decimal string",
    ):
        PortfolioPlan.from_dict(malformed_decimal)
