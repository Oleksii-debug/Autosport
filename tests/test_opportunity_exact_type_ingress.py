from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

import autosport.opportunity as opportunity_module

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
    PredictiveEligibilityEvidence,
    QuoteRef,
    StrategyClass,
)


_HASH = "a" * 64


def _quote() -> QuoteRef:
    return QuoteRef.from_market_event(
        MarketEvent(
            event_id="event-exact-type",
            market_id="market-exact-type",
            selection_id="selection-exact-type",
            decimal_odds=Decimal("2.00"),
            observed_ts="2026-09-16T16:00:00+00:00",
            source_id="provider-exact-type",
            sequence=1,
            source_ts="2026-09-16T15:59:59+00:00",
            ingest_ts="2026-09-16T16:00:01+00:00",
        )
    )


class _ForecastRecordSubclass(ForecastRecord):
    __slots__ = ()


class _PredictiveEligibilitySubclass(PredictiveEligibilityEvidence):
    __slots__ = ()


class _ForecastRefSubclass(ForecastRef):
    __slots__ = ()


class _QuoteRefSubclass(QuoteRef):
    __slots__ = ()


class _OpportunitySubclass(Opportunity):
    __slots__ = ()


class _OpportunitySetSubclass(OpportunitySet):
    __slots__ = ()


class _PlanAllocationSubclass(PlanAllocation):
    __slots__ = ()


def test_forecast_source_and_quote_require_exact_types() -> None:
    quote = _quote()
    hostile_forecast = _ForecastRecordSubclass(
        quote_key=quote.quote_key,
        probability=Decimal("0.5"),
        model_id="model",
        model_version="1",
        strategy_version="1",
        model_training_cutoff_ts="2026-09-16T15:00:00+00:00",
        input_cutoff_ts="2026-09-16T16:00:00+00:00",
        generated_at="2026-09-16T16:00:01+00:00",
        market_snapshot_hash=_HASH,
    )
    with pytest.raises(
        OpportunityContractError,
        match="forecast source must be a ForecastRecord",
    ):
        ForecastRef.from_forecast(hostile_forecast, quote)

    exact_forecast = ForecastRecord(
        quote_key=quote.quote_key,
        probability=Decimal("0.5"),
        model_id="model",
        model_version="1",
        strategy_version="1",
        model_training_cutoff_ts="2026-09-16T15:00:00+00:00",
        input_cutoff_ts="2026-09-16T16:00:00+00:00",
        generated_at="2026-09-16T16:00:01+00:00",
        market_snapshot_hash=_HASH,
    )
    hostile_quote = _QuoteRefSubclass(
        event_id="event-hostile-quote",
        market_id="market-hostile-quote",
        selection_id="selection-hostile-quote",
        source_id="provider-hostile-quote",
        sequence=1,
        decimal_odds=Decimal("2"),
        observed_ts="2026-09-16T16:00:00+00:00",
        source_ts="2026-09-16T15:59:59+00:00",
        ingest_ts="2026-09-16T16:00:01+00:00",
        market_event_hash=_HASH,
    )
    with pytest.raises(
        OpportunityContractError,
        match="forecast quote must be a QuoteRef",
    ):
        ForecastRef.from_forecast(exact_forecast, hostile_quote)


def test_predictive_eligibility_requires_exact_type() -> None:
    eligibility = _PredictiveEligibilitySubclass(
        evaluation_id="evaluation-hostile-type",
        evaluation_sha256=_HASH,
        protocol_sha256=_HASH,
        admission_policy_sha256=_HASH,
        model_id="model",
        model_version="1",
        strategy_version="1",
        uncertainty_kind="absolute_probability_radius_v1",
        sample_size=100,
        minimum_sample_size=50,
        maximum_uncertainty=Decimal("0.1"),
        as_of="2026-09-16T16:00:00+00:00",
        valid_until="2026-09-17T16:00:00+00:00",
    )
    with pytest.raises(
        OpportunityContractError,
        match="forecast predictive_eligibility must be typed evidence",
    ):
        ForecastRef(
            forecast_id="forecast-hostile-evidence-type",
            forecast_hash=_HASH,
            quote_key=_quote().quote_key,
            probability=Decimal("0.5"),
            input_cutoff_ts="2026-09-16T16:00:00+00:00",
            market_snapshot_hash=_HASH,
            quote_market_event_hash=_HASH,
            model_id="model",
            model_version="1",
            strategy_version="1",
            uncertainty=Decimal("0.1"),
            predictive_eligibility=eligibility,
        )


def test_nested_opportunity_members_require_exact_types() -> None:
    quote = _quote()
    forecast = _ForecastRefSubclass(
        forecast_id="forecast-hostile-ref-type",
        forecast_hash=_HASH,
        quote_key=quote.quote_key,
        probability=Decimal("0.5"),
        input_cutoff_ts="2026-09-16T16:00:00+00:00",
        market_snapshot_hash=_HASH,
        quote_market_event_hash=_HASH,
    )
    with pytest.raises(
        OpportunityContractError,
        match="opportunity forecasts must be ForecastRef values",
    ):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(quote,),
            forecasts=(forecast,),
        )

    hostile_opportunity = _OpportunitySubclass(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.WAIT,
        quotes=(quote,),
    )
    with pytest.raises(
        OpportunityContractError,
        match="opportunity set must contain only Opportunity values",
    ):
        OpportunitySet((hostile_opportunity,))


def test_portfolio_plan_members_require_exact_types() -> None:
    opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(_quote(),),
    )
    hostile_set = _OpportunitySetSubclass((opportunity,))
    with pytest.raises(
        OpportunityContractError,
        match="opportunity_set must be an OpportunitySet",
    ):
        PortfolioPlan(opportunity_set=hostile_set)

    exact_set = OpportunitySet((opportunity,))
    hostile_allocation = _PlanAllocationSubclass(
        opportunity.opportunity_id,
        Decimal("1"),
    )
    with pytest.raises(
        OpportunityContractError,
        match="allocations must contain only PlanAllocation values",
    ):
        PortfolioPlan(
            opportunity_set=exact_set,
            allocations=(hostile_allocation,),
        )

class _HostileTuple(tuple):
    def __iter__(self):
        raise AssertionError("hostile tuple iteration executed")


def test_canonical_tuple_fields_reject_subclasses_before_iteration() -> None:
    quote = _quote()

    with pytest.raises(OpportunityContractError, match="opportunity quotes must be a tuple"):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=_HostileTuple((quote,)),  # type: ignore[arg-type]
        )

    with pytest.raises(OpportunityContractError, match="opportunity forecasts must be a tuple"):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(quote,),
            forecasts=_HostileTuple(()),  # type: ignore[arg-type]
        )

    with pytest.raises(OpportunityContractError, match="opportunity evidence_refs must be a tuple"):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(quote,),
            evidence_refs=_HostileTuple(()),  # type: ignore[arg-type]
        )

    opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.WAIT,
        quotes=(quote,),
    )
    with pytest.raises(OpportunityContractError, match="opportunity set members must be a tuple"):
        OpportunitySet(_HostileTuple((opportunity,)))  # type: ignore[arg-type]

    opportunity_set = OpportunitySet((opportunity,))
    with pytest.raises(OpportunityContractError, match="allocations must be a tuple"):
        PortfolioPlan(
            opportunity_set=opportunity_set,
            allocations=_HostileTuple(()),  # type: ignore[arg-type]
        )

    with pytest.raises(OpportunityContractError, match="portfolio_evidence_refs must be a tuple"):
        PortfolioPlan(
            opportunity_set=opportunity_set,
            portfolio_evidence_refs=_HostileTuple(()),  # type: ignore[arg-type]
        )




class _RebindHostileDecimal(Decimal):
    def is_finite(self) -> bool:
        raise AssertionError("rebound Decimal virtual dispatch executed")


class _RebindHostileMarketEvent(MarketEvent):
    __slots__ = ()

    def to_dict(self) -> dict[str, object]:
        raise AssertionError("rebound MarketEvent serialization executed")


class _RebindHostileQuoteRef(QuoteRef):
    __slots__ = ()

    @property
    def identity_key(self) -> tuple[str, str, str, str, str, int]:
        raise AssertionError("rebound QuoteRef identity dispatch executed")


def test_decimal_module_global_rebind_cannot_replace_canonical_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(opportunity_module, "Decimal", _RebindHostileDecimal)

    with pytest.raises(
        OpportunityContractError,
        match="allocation stake must be an exact finite Decimal",
    ):
        PlanAllocation(_HASH, _RebindHostileDecimal("1"))

    restored = PlanAllocation.from_dict(
        {"opportunity_id": _HASH, "stake": "1"}
    )
    assert type(restored.stake) is Decimal
    assert restored.stake == Decimal("1")


def test_market_event_global_rebind_cannot_replace_quote_source_trust_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = _RebindHostileMarketEvent(
        event_id="event-rebound-market",
        market_id="market-rebound-market",
        selection_id="selection-rebound-market",
        decimal_odds=Decimal("2"),
        observed_ts="2026-09-16T16:00:00+00:00",
        source_id="provider-rebound-market",
        sequence=1,
        source_ts="2026-09-16T15:59:59+00:00",
        ingest_ts="2026-09-16T16:00:01+00:00",
    )
    monkeypatch.setattr(opportunity_module, "MarketEvent", _RebindHostileMarketEvent)

    with pytest.raises(
        OpportunityContractError,
        match="quote source must be a MarketEvent",
    ):
        QuoteRef.from_market_event(hostile)


def test_quote_ref_global_rebind_cannot_replace_opportunity_member_trust_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = _RebindHostileQuoteRef(
        event_id="event-rebound-quote",
        market_id="market-rebound-quote",
        selection_id="selection-rebound-quote",
        source_id="provider-rebound-quote",
        sequence=1,
        decimal_odds=Decimal("2"),
        observed_ts="2026-09-16T16:00:00+00:00",
        source_ts="2026-09-16T15:59:59+00:00",
        ingest_ts="2026-09-16T16:00:01+00:00",
        market_event_hash=_HASH,
    )
    monkeypatch.setattr(opportunity_module, "QuoteRef", _RebindHostileQuoteRef)

    with pytest.raises(
        OpportunityContractError,
        match="opportunity quotes must be QuoteRef values",
    ):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(hostile,),
        )


def test_serialized_nested_reconstruction_ignores_rebound_type_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.WAIT,
        quotes=(_quote(),),
        evidence_refs=(EvidenceRef("test-authority", "test-reference"),),
    )
    payload = original.to_dict()

    class _PoisonQuoteRef:
        @classmethod
        def from_dict(cls, raw: object) -> object:
            raise AssertionError("rebound QuoteRef.from_dict executed")

    class _PoisonEvidenceRef:
        @classmethod
        def from_dict(cls, raw: object) -> object:
            raise AssertionError("rebound EvidenceRef.from_dict executed")

    def _poison_strategy(value: object) -> object:
        raise AssertionError("rebound StrategyClass constructor executed")

    def _poison_decision(value: object) -> object:
        raise AssertionError("rebound OpportunityDecision constructor executed")

    monkeypatch.setattr(opportunity_module, "StrategyClass", _poison_strategy)
    monkeypatch.setattr(opportunity_module, "OpportunityDecision", _poison_decision)
    monkeypatch.setattr(opportunity_module, "QuoteRef", _PoisonQuoteRef)
    monkeypatch.setattr(opportunity_module, "EvidenceRef", _PoisonEvidenceRef)

    restored = Opportunity.from_dict(payload)
    assert restored == original
    assert type(restored.strategy_class) is StrategyClass
    assert type(restored.decision) is OpportunityDecision
    assert all(type(item) is QuoteRef for item in restored.quotes)
    assert all(type(item) is EvidenceRef for item in restored.evidence_refs)

def test_quote_key_uses_captured_canonical_identity_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote = _quote()
    expected_quote_key = quote.quote_key

    def _poison_quote_identity(*args: object, **kwargs: object) -> str:
        raise AssertionError("rebound quote identity dispatch executed")

    monkeypatch.setattr(
        opportunity_module,
        "_quote_identity",
        _poison_quote_identity,
    )

    assert quote.quote_key == expected_quote_key


def test_economic_identity_hashing_uses_captured_dispatch_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote = _quote()
    opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(quote,),
    )
    opportunity_set = OpportunitySet((opportunity,))
    plan = PortfolioPlan(opportunity_set=opportunity_set)
    expected_ids = (
        quote.market_event_hash,
        opportunity.conflict_key,
        opportunity.opportunity_id,
        opportunity_set.opportunity_set_id,
        plan.plan_id,
    )

    class _PoisonJson:
        @staticmethod
        def dumps(*args: object, **kwargs: object) -> str:
            raise AssertionError("rebound json.dumps executed")

    class _PoisonHashlib:
        @staticmethod
        def sha256(*args: object, **kwargs: object) -> object:
            raise AssertionError("rebound hashlib.sha256 executed")

    monkeypatch.setattr(opportunity_module, "json", _PoisonJson())
    monkeypatch.setattr(opportunity_module, "hashlib", _PoisonHashlib())

    rebuilt_quote = _quote()
    rebuilt_opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(rebuilt_quote,),
    )
    rebuilt_set = OpportunitySet((rebuilt_opportunity,))
    rebuilt_plan = PortfolioPlan(opportunity_set=rebuilt_set)

    assert (
        rebuilt_quote.market_event_hash,
        rebuilt_opportunity.conflict_key,
        rebuilt_opportunity.opportunity_id,
        rebuilt_set.opportunity_set_id,
        rebuilt_plan.plan_id,
    ) == expected_ids

def test_predictive_chronology_uses_captured_timestamp_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _poison_timestamp_parser(value: object) -> object:
        raise AssertionError("rebound timestamp parser executed")

    monkeypatch.setattr(
        opportunity_module,
        "parse_iso_timestamp",
        _poison_timestamp_parser,
    )

    eligibility = PredictiveEligibilityEvidence(
        evaluation_id="evaluation-chronology-root",
        evaluation_sha256=_HASH,
        protocol_sha256=_HASH,
        admission_policy_sha256=_HASH,
        model_id="model",
        model_version="1",
        strategy_version="1",
        uncertainty_kind="absolute_probability_radius_v1",
        sample_size=100,
        minimum_sample_size=50,
        maximum_uncertainty=Decimal("0.1"),
        as_of="2026-09-16T15:00:00+00:00",
        valid_until="2026-09-16T18:00:00+00:00",
    )
    forecast = ForecastRef(
        forecast_id="forecast-chronology-root",
        forecast_hash=_HASH,
        quote_key=_quote().quote_key,
        probability=Decimal("0.5"),
        input_cutoff_ts="2026-09-16T16:00:00+00:00",
        market_snapshot_hash=_HASH,
        quote_market_event_hash=_HASH,
        model_id="model",
        model_version="1",
        strategy_version="1",
        uncertainty=Decimal("0.05"),
        predictive_eligibility=eligibility,
    )

    reason = forecast.predictive_eligibility_reason(
        datetime(2026, 9, 16, 16, 30, tzinfo=timezone.utc),
        expected_model_id="model",
    )
    assert reason is None


def test_future_forecast_cutoff_still_rejects_after_parser_global_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eligibility = PredictiveEligibilityEvidence(
        evaluation_id="evaluation-future-cutoff",
        evaluation_sha256=_HASH,
        protocol_sha256=_HASH,
        admission_policy_sha256=_HASH,
        model_id="model",
        model_version="1",
        strategy_version="1",
        uncertainty_kind="absolute_probability_radius_v1",
        sample_size=100,
        minimum_sample_size=50,
        maximum_uncertainty=Decimal("0.1"),
        as_of="2026-09-16T15:00:00+00:00",
        valid_until="2026-09-16T18:00:00+00:00",
    )
    forecast = ForecastRef(
        forecast_id="forecast-future-cutoff",
        forecast_hash=_HASH,
        quote_key=_quote().quote_key,
        probability=Decimal("0.5"),
        input_cutoff_ts="2026-09-16T17:00:00+00:00",
        market_snapshot_hash=_HASH,
        quote_market_event_hash=_HASH,
        model_id="model",
        model_version="1",
        strategy_version="1",
        uncertainty=Decimal("0.05"),
        predictive_eligibility=eligibility,
    )

    def _launder_future_cutoff(value: object) -> object:
        raise AssertionError("rebound timestamp parser executed")

    monkeypatch.setattr(
        opportunity_module,
        "parse_iso_timestamp",
        _launder_future_cutoff,
    )

    reason = forecast.predictive_eligibility_reason(
        datetime(2026, 9, 16, 16, 30, tzinfo=timezone.utc),
        expected_model_id="model",
    )
    assert reason == "predictive forecast input cutoff is from the future"

