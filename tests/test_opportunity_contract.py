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
_SHA_C = "c" * 64


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


def _quote(
    event: MarketEvent | None = None,
    *,
    snapshot_hash: str | None = _SHA_B,
) -> QuoteRef:
    return QuoteRef.from_market_event(
        _event() if event is None else event,
        market_snapshot_hash=snapshot_hash,
    )


def _forecast(
    quote_key: str,
    *,
    forecast_id: str = "forecast-1",
    probability: str = "0.55",
    snapshot_hash: str | None = _SHA_B,
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
        market_snapshot_hash=snapshot_hash,
        provenance={"split": "live-causal"},
        forecast_id=forecast_id,
    )


def _forecast_ref(
    quote: QuoteRef,
    *,
    forecast_id: str = "forecast-1",
    probability: str = "0.55",
) -> ForecastRef:
    return ForecastRef.from_forecast(
        _forecast(
            quote.quote_key,
            forecast_id=forecast_id,
            probability=probability,
            snapshot_hash=quote.market_snapshot_hash,
        ),
        quote,
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
    quote = _quote()
    forecast = _forecast_ref(quote)
    return Opportunity(
        strategy_class=StrategyClass.PREDICTIVE_EDGE,
        decision=decision,
        quotes=(quote,),
        claims_probability_edge=True,
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
    restored_quote = restored.opportunity_set.opportunities[0].quotes[0]
    assert restored_quote.source_ts == "2026-09-16T15:59:59+00:00"
    assert restored_quote.ingest_ts == "2026-09-16T16:00:01+00:00"
    assert restored_quote.market_snapshot_hash == _SHA_B


def test_nonpredictive_strategy_does_not_require_or_synthesize_forecast() -> None:
    quote = _quote(snapshot_hash=None)
    opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.WAIT,
        quotes=(quote,),
    )

    assert opportunity.claims_probability_edge is False
    assert opportunity.forecasts == ()
    assert opportunity.to_dict()["forecasts"] == []
    assert Opportunity.from_dict(opportunity.to_dict()) == opportunity


def test_predictive_opportunity_requires_forecast_for_every_quote() -> None:
    first = _quote()
    second = _quote(
        _event(
            event_id="event-2",
            market_id="market-2",
            selection_id="selection-2",
            sequence=8,
        )
    )
    forecast = _forecast_ref(first)

    with pytest.raises(
        OpportunityContractError,
        match="requires forecast evidence for every quote",
    ):
        Opportunity(
            strategy_class=StrategyClass.PREDICTIVE_EDGE,
            decision=OpportunityDecision.ACTIONABLE,
            quotes=(first, second),
            claims_probability_edge=True,
            forecasts=(forecast,),
        )


def test_hybrid_probability_edge_requires_forecasts_but_structural_hybrid_does_not() -> None:
    quote = _quote()

    with pytest.raises(
        OpportunityContractError,
        match="requires forecast evidence for every quote",
    ):
        Opportunity(
            strategy_class=StrategyClass.HYBRID,
            decision=OpportunityDecision.WAIT,
            quotes=(quote,),
            claims_probability_edge=True,
        )

    structural = Opportunity(
        strategy_class=StrategyClass.HYBRID,
        decision=OpportunityDecision.WAIT,
        quotes=(quote,),
        claims_probability_edge=False,
    )
    assert structural.forecasts == ()

    forecast = _forecast_ref(quote)
    predictive_hybrid = Opportunity(
        strategy_class=StrategyClass.HYBRID,
        decision=OpportunityDecision.WAIT,
        quotes=(quote,),
        claims_probability_edge=True,
        forecasts=(forecast,),
    )
    assert predictive_hybrid.forecasts == (forecast,)


def test_predictive_edge_cannot_disable_probability_edge_semantics() -> None:
    with pytest.raises(
        OpportunityContractError,
        match="PREDICTIVE_EDGE must claim a probability edge",
    ):
        Opportunity(
            strategy_class=StrategyClass.PREDICTIVE_EDGE,
            decision=OpportunityDecision.WAIT,
            quotes=(_quote(),),
            claims_probability_edge=False,
        )


def test_structural_strategy_cannot_claim_probability_edge() -> None:
    with pytest.raises(
        OpportunityContractError,
        match="supported only for PREDICTIVE_EDGE or HYBRID",
    ):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(_quote(),),
            claims_probability_edge=True,
        )


def test_forecast_for_outside_quote_fails_closed() -> None:
    quote = _quote()
    outside_quote = _quote(
        _event(
            event_id="event-2",
            market_id="market-2",
            selection_id="selection-2",
        )
    )
    outside = _forecast_ref(outside_quote, forecast_id="forecast-outside")

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


def test_forecast_reference_requires_canonical_market_snapshot() -> None:
    quote_without_snapshot = _quote(snapshot_hash=None)
    record = _forecast(quote_without_snapshot.quote_key, snapshot_hash=None)

    with pytest.raises(
        OpportunityContractError,
        match="requires canonical market_snapshot_hash evidence",
    ):
        ForecastRef.from_forecast(record, quote_without_snapshot)


def test_forecast_reference_rejects_market_snapshot_mismatch() -> None:
    quote = _quote(snapshot_hash=_SHA_B)
    record = _forecast(quote.quote_key, snapshot_hash=_SHA_C)

    with pytest.raises(
        OpportunityContractError,
        match="market snapshot does not match bound QuoteRef",
    ):
        ForecastRef.from_forecast(record, quote)


def test_predictive_forecast_is_bound_to_exact_quote_event_snapshot() -> None:
    older = _quote(
        _event(sequence=7, odds="2.10"),
        snapshot_hash=_SHA_B,
    )
    later = _quote(
        _event(sequence=8, odds="1.95"),
        snapshot_hash=_SHA_B,
    )
    forecast = _forecast_ref(older)

    assert older.quote_key == later.quote_key
    assert older.market_event_hash != later.market_event_hash
    with pytest.raises(
        OpportunityContractError,
        match="does not bind the exact opportunity quote snapshot",
    ):
        Opportunity(
            strategy_class=StrategyClass.PREDICTIVE_EDGE,
            decision=OpportunityDecision.ACTIONABLE,
            quotes=(later,),
            claims_probability_edge=True,
            forecasts=(forecast,),
        )


def test_quote_reference_preserves_source_sequence_and_full_event_hash() -> None:
    first = _quote(_event(), snapshot_hash=None)
    later = _quote(
        _event(sequence=8, metadata={"provider_market": "changed"}),
        snapshot_hash=None,
    )

    assert first.quote_key == later.quote_key
    assert first.identity_key != later.identity_key
    assert first.market_event_hash != later.market_event_hash

    with pytest.raises(
        OpportunityContractError,
        match="quote serialization is ambiguous",
    ):
        Opportunity(
            strategy_class=StrategyClass.LIVE_PRICE_MOVEMENT,
            decision=OpportunityDecision.WAIT,
            quotes=(first, later),
        )


def test_opportunity_set_order_is_deterministic_and_duplicates_fail_closed() -> None:
    first = _predictive_opportunity()
    second_quote = _quote(
        _event(
            event_id="event-2",
            market_id="market-2",
            selection_id="selection-2",
        ),
        snapshot_hash=None,
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


def test_serialized_forecast_quote_binding_tampering_fails_closed() -> None:
    opportunity = _predictive_opportunity()
    payload = opportunity.to_dict()
    payload["forecasts"][0]["quote_market_event_hash"] = _SHA_C

    with pytest.raises(
        OpportunityContractError,
        match="does not bind the exact opportunity quote snapshot",
    ):
        Opportunity.from_dict(payload)
