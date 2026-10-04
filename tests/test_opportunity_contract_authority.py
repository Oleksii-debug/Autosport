from __future__ import annotations

from decimal import Decimal

import pytest

import autosport.opportunity as opportunity_module

from autosport.domain import MarketEvent
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
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


_HASH = "a" * 64


def _quote() -> QuoteRef:
    return QuoteRef.from_market_event(
        MarketEvent(
            event_id="event-authority",
            market_id="market-authority",
            selection_id="selection-authority",
            decimal_odds=Decimal("2.00"),
            observed_ts="2026-09-16T16:00:00+00:00",
            source_id="provider-authority",
            sequence=1,
            source_ts="2026-09-16T15:59:59+00:00",
            ingest_ts="2026-09-16T16:00:01+00:00",
        )
    )


def _plan() -> PortfolioPlan:
    opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(_quote(),),
    )
    return PortfolioPlan(
        opportunity_set=OpportunitySet((opportunity,)),
        allocations=(
            PlanAllocation(opportunity.opportunity_id, Decimal("1.00")),
        ),
        portfolio_evidence_refs=(
            EvidenceRef("PortfolioEngine", "scenario-report:authority-test"),
        ),
        risk_evidence_refs=(
            EvidenceRef("PaperRiskPolicy", "risk-decision:authority-test"),
        ),
        ledger_state_refs=(
            EvidenceRef("PaperBook", "ledger-state:authority-test"),
        ),
    )


def test_contract_construction_does_not_mutate_paperbook_or_risk_state() -> None:
    book = PaperBook(Decimal("1000"))
    policy = PaperRiskPolicy()
    state_before = (
        book.initial_bankroll,
        book.balance,
        book.committed_stake,
        tuple(book.tickets.items()),
    )
    risk_before = policy.evaluate(book, Decimal("1"))

    plan = _plan()

    state_after = (
        book.initial_bankroll,
        book.balance,
        book.committed_stake,
        tuple(book.tickets.items()),
    )
    risk_after = policy.evaluate(book, Decimal("1"))
    assert plan.allocations[0].stake == Decimal("1.00")
    assert state_after == state_before
    assert risk_after == risk_before
    assert book.tickets == {}


def test_nonfinite_decimal_contract_inputs_fail_closed() -> None:
    with pytest.raises(
        OpportunityContractError,
        match="allocation stake must be an exact finite Decimal",
    ):
        PlanAllocation(_HASH, Decimal("NaN"))

    with pytest.raises(
        OpportunityContractError,
        match="quote decimal_odds must be an exact finite Decimal",
    ):
        QuoteRef(
            event_id="event",
            market_id="market",
            selection_id="selection",
            source_id="provider",
            sequence=1,
            decimal_odds=Decimal("Infinity"),
            observed_ts="2026-09-16T16:00:00+00:00",
            source_ts="2026-09-16T15:59:59+00:00",
            ingest_ts="2026-09-16T16:00:01+00:00",
            market_event_hash=_HASH,
        )

    with pytest.raises(
        OpportunityContractError,
        match="forecast probability must be an exact finite Decimal",
    ):
        ForecastRef(
            forecast_id="forecast",
            forecast_hash=_HASH,
            quote_key="event|market|selection",
            probability=Decimal("NaN"),
            input_cutoff_ts="2026-09-16T16:00:00+00:00",
            market_snapshot_hash=_HASH,
            quote_market_event_hash=_HASH,
        )


def test_opportunity_set_rejects_conflicting_decisions_for_same_facts() -> None:
    quote = _quote()
    waiting = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.WAIT,
        quotes=(quote,),
    )
    actionable = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(quote,),
    )

    assert waiting.opportunity_id != actionable.opportunity_id
    assert waiting.conflict_key == actionable.conflict_key
    with pytest.raises(
        OpportunityContractError,
        match="conflicting decisions for the same canonical opportunity",
    ):
        OpportunitySet((waiting, actionable))


class _HostileDecimal(Decimal):
    def is_finite(self) -> bool:
        raise AssertionError("Decimal subclass virtual dispatch must not execute")


def test_decimal_subclasses_fail_closed_before_virtual_dispatch() -> None:
    with pytest.raises(
        OpportunityContractError,
        match="allocation stake must be an exact finite Decimal",
    ):
        PlanAllocation(_HASH, _HostileDecimal("1"))

    with pytest.raises(
        OpportunityContractError,
        match="quote decimal_odds must be an exact finite Decimal",
    ):
        QuoteRef(
            event_id="event-hostile-decimal",
            market_id="market-hostile-decimal",
            selection_id="selection-hostile-decimal",
            source_id="provider-hostile-decimal",
            sequence=1,
            decimal_odds=_HostileDecimal("2"),
            observed_ts="2026-09-16T16:00:00+00:00",
            source_ts="2026-09-16T15:59:59+00:00",
            ingest_ts="2026-09-16T16:00:01+00:00",
            market_event_hash=_HASH,
        )

    with pytest.raises(
        OpportunityContractError,
        match="forecast probability must be an exact finite Decimal",
    ):
        ForecastRef(
            forecast_id="forecast-hostile-probability",
            forecast_hash=_HASH,
            quote_key="event|market|selection",
            probability=_HostileDecimal("0.5"),
            input_cutoff_ts="2026-09-16T16:00:00+00:00",
            market_snapshot_hash=_HASH,
            quote_market_event_hash=_HASH,
        )

    with pytest.raises(
        OpportunityContractError,
        match="forecast uncertainty must be an exact finite Decimal",
    ):
        ForecastRef(
            forecast_id="forecast-hostile-uncertainty",
            forecast_hash=_HASH,
            quote_key="event|market|selection",
            probability=Decimal("0.5"),
            input_cutoff_ts="2026-09-16T16:00:00+00:00",
            market_snapshot_hash=_HASH,
            quote_market_event_hash=_HASH,
            model_id="model",
            model_version="1",
            strategy_version="1",
            uncertainty=_HostileDecimal("0.1"),
        )

    with pytest.raises(
        OpportunityContractError,
        match="predictive maximum_uncertainty must be an exact finite Decimal",
    ):
        PredictiveEligibilityEvidence(
            evaluation_id="evaluation-hostile-decimal",
            evaluation_sha256=_HASH,
            protocol_sha256=_HASH,
            admission_policy_sha256=_HASH,
            model_id="model",
            model_version="1",
            strategy_version="1",
            uncertainty_kind="absolute_probability_radius_v1",
            sample_size=100,
            minimum_sample_size=50,
            maximum_uncertainty=_HostileDecimal("0.1"),
            as_of="2026-09-16T16:00:00+00:00",
            valid_until="2026-09-17T16:00:00+00:00",
        )


class _HostileEvidenceRef(EvidenceRef):
    __slots__ = ()

    def __hash__(self) -> int:
        raise AssertionError("EvidenceRef subclass hash dispatch must not execute")

    def __lt__(self, other: object) -> bool:
        raise AssertionError("EvidenceRef subclass ordering dispatch must not execute")


def test_evidence_ref_subclasses_fail_closed_before_virtual_dispatch() -> None:
    hostile = _HostileEvidenceRef(
        "attacker-evidence-authority",
        "attacker-evidence-reference",
    )

    with pytest.raises(
        OpportunityContractError,
        match="opportunity evidence_refs must contain only EvidenceRef values",
    ):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(_quote(),),
            evidence_refs=(hostile,),
        )

class _HostileMarketEvent(MarketEvent):
    __slots__ = ()

    def to_dict(self) -> dict[str, object]:
        raise AssertionError("MarketEvent subclass serialization must not execute")


class _HostileQuoteRef(QuoteRef):
    __slots__ = ()

    @property
    def identity_key(self) -> tuple[str, str, str, str, str, int]:
        raise AssertionError("QuoteRef subclass identity dispatch must not execute")

    @property
    def quote_key(self) -> str:
        raise AssertionError("QuoteRef subclass quote dispatch must not execute")


def test_canonical_typed_subclasses_fail_closed_before_property_dispatch() -> None:
    hostile_event = _HostileMarketEvent(
        event_id="event-hostile-type",
        market_id="market-hostile-type",
        selection_id="selection-hostile-type",
        decimal_odds=Decimal("2"),
        observed_ts="2026-09-16T16:00:00+00:00",
        source_id="provider-hostile-type",
        sequence=1,
        source_ts="2026-09-16T15:59:59+00:00",
        ingest_ts="2026-09-16T16:00:01+00:00",
    )
    with pytest.raises(
        OpportunityContractError,
        match="quote source must be a MarketEvent",
    ):
        QuoteRef.from_market_event(hostile_event)

    hostile_quote = _HostileQuoteRef(
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
        match="opportunity quotes must be QuoteRef values",
    ):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(hostile_quote,),
        )

def test_validator_helper_rebinding_cannot_bypass_exact_decimal_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        opportunity_module,
        "_finite_decimal",
        lambda value, *_args, **_kwargs: value,
    )

    with pytest.raises(
        OpportunityContractError,
        match="allocation stake must be an exact finite Decimal",
    ):
        PlanAllocation(_HASH, _HostileDecimal("1"))

    with pytest.raises(
        OpportunityContractError,
        match="quote decimal_odds must be an exact finite Decimal",
    ):
        QuoteRef(
            event_id="event-helper-rebind",
            market_id="market-helper-rebind",
            selection_id="selection-helper-rebind",
            source_id="provider-helper-rebind",
            sequence=1,
            decimal_odds=_HostileDecimal("2"),
            observed_ts="2026-09-16T16:00:00+00:00",
            source_ts="2026-09-16T15:59:59+00:00",
            ingest_ts="2026-09-16T16:00:01+00:00",
            market_event_hash=_HASH,
        )

    with pytest.raises(
        OpportunityContractError,
        match="forecast probability must be an exact finite Decimal",
    ):
        ForecastRef(
            forecast_id="forecast-helper-rebind",
            forecast_hash=_HASH,
            quote_key="event|market|selection",
            probability=_HostileDecimal("0.5"),
            input_cutoff_ts="2026-09-16T16:00:00+00:00",
            market_snapshot_hash=_HASH,
            quote_market_event_hash=_HASH,
        )


def test_evidence_helper_rebinding_cannot_admit_subclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = _HostileEvidenceRef(
        "attacker-helper-authority",
        "attacker-helper-reference",
    )
    monkeypatch.setattr(
        opportunity_module,
        "_sorted_unique_evidence",
        lambda values, *_args, **_kwargs: values,
    )

    with pytest.raises(
        OpportunityContractError,
        match="opportunity evidence_refs must contain only EvidenceRef values",
    ):
        Opportunity(
            strategy_class=StrategyClass.ARBITRAGE,
            decision=OpportunityDecision.WAIT,
            quotes=(_quote(),),
            evidence_refs=(hostile,),
        )


def test_identity_helper_rebinding_cannot_replace_economic_ids(
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
    expected = (
        quote.quote_key,
        quote.market_event_hash,
        opportunity.conflict_key,
        opportunity.opportunity_id,
        opportunity_set.opportunity_set_id,
        plan.plan_id,
    )

    monkeypatch.setattr(
        opportunity_module,
        "_quote_key",
        lambda *_args, **_kwargs: "attacker|quote|key",
    )
    monkeypatch.setattr(
        opportunity_module,
        "_canonical_json_hash",
        lambda *_args, **_kwargs: "0" * 64,
    )

    rebuilt_quote = _quote()
    rebuilt_opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(rebuilt_quote,),
    )
    rebuilt_set = OpportunitySet((rebuilt_opportunity,))
    rebuilt_plan = PortfolioPlan(opportunity_set=rebuilt_set)

    assert (
        rebuilt_quote.quote_key,
        rebuilt_quote.market_event_hash,
        rebuilt_opportunity.conflict_key,
        rebuilt_opportunity.opportunity_id,
        rebuilt_set.opportunity_set_id,
        rebuilt_plan.plan_id,
    ) == expected

class _HostileDecisionTime:
    tzinfo = object()

    def __lt__(self, _other: object) -> bool:
        raise AssertionError("caller decision-time comparison must not execute")

    def __gt__(self, _other: object) -> bool:
        raise AssertionError("caller decision-time comparison must not execute")


def test_predictive_decision_time_rejects_hostile_objects_before_comparison() -> None:
    eligibility = PredictiveEligibilityEvidence(
        evaluation_id="evaluation-hostile-decision-time",
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
        forecast_id="forecast-hostile-decision-time",
        forecast_hash=_HASH,
        quote_key="event|market|selection",
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

    assert forecast.predictive_eligibility_reason(
        _HostileDecisionTime(),
        expected_model_id="model",
    ) == "predictive decision time must be timezone-aware"

def test_public_opportunity_dependency_parameters_are_sealed() -> None:
    from inspect import signature

    public_surfaces = {
        "QuoteRef.__post_init__": QuoteRef.__post_init__,
        "QuoteRef.quote_key": QuoteRef.quote_key.fget,
        "QuoteRef.from_market_event": QuoteRef.from_market_event,
        "PredictiveEligibilityEvidence.__post_init__": (
            PredictiveEligibilityEvidence.__post_init__
        ),
        "ForecastRef.__post_init__": ForecastRef.__post_init__,
        "ForecastRef.from_forecast": ForecastRef.from_forecast,
        "ForecastRef.predictive_eligibility_reason": (
            ForecastRef.predictive_eligibility_reason
        ),
        "Opportunity.__post_init__": Opportunity.__post_init__,
        "Opportunity.predictive_uncertainty_haircut": (
            Opportunity.predictive_uncertainty_haircut.fget
        ),
        "Opportunity.conflict_key": Opportunity.conflict_key.fget,
        "Opportunity.opportunity_id": Opportunity.opportunity_id.fget,
        "OpportunitySet.__post_init__": OpportunitySet.__post_init__,
        "OpportunitySet.opportunity_set_id": OpportunitySet.opportunity_set_id.fget,
        "PlanAllocation.__post_init__": PlanAllocation.__post_init__,
        "PortfolioPlan.__post_init__": PortfolioPlan.__post_init__,
        "PortfolioPlan.plan_id": PortfolioPlan.plan_id.fget,
    }
    expected_parameters = {
        "QuoteRef.__post_init__": ("self",),
        "QuoteRef.quote_key": ("self",),
        "QuoteRef.from_market_event": ("event", "market_snapshot_hash"),
        "PredictiveEligibilityEvidence.__post_init__": ("self",),
        "ForecastRef.__post_init__": ("self",),
        "ForecastRef.from_forecast": (
            "forecast",
            "quote",
            "predictive_eligibility",
        ),
        "ForecastRef.predictive_eligibility_reason": (
            "self",
            "decision_time",
            "expected_model_id",
        ),
        "Opportunity.__post_init__": ("self",),
        "Opportunity.predictive_uncertainty_haircut": ("self",),
        "Opportunity.conflict_key": ("self",),
        "Opportunity.opportunity_id": ("self",),
        "OpportunitySet.__post_init__": ("self",),
        "OpportunitySet.opportunity_set_id": ("self",),
        "PlanAllocation.__post_init__": ("self",),
        "PortfolioPlan.__post_init__": ("self",),
        "PortfolioPlan.plan_id": ("self",),
    }

    assert set(public_surfaces) == set(expected_parameters)
    for name, surface in public_surfaces.items():
        assert surface is not None
        assert tuple(signature(surface).parameters) == expected_parameters[name]


def test_public_opportunity_dependency_injection_is_rejected() -> None:
    quote = _quote()
    opportunity = Opportunity(
        strategy_class=StrategyClass.ARBITRAGE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(quote,),
    )
    allocation = PlanAllocation(opportunity.opportunity_id, Decimal("1"))

    with pytest.raises(TypeError):
        QuoteRef.from_market_event(
            MarketEvent(
                event_id="event-public-hook",
                market_id="market-public-hook",
                selection_id="selection-public-hook",
                decimal_odds=Decimal("2"),
                observed_ts="2026-09-16T16:00:00+00:00",
                source_id="provider-public-hook",
                sequence=1,
                ingest_ts="2026-09-16T16:00:01+00:00",
            ),
            _canonical_json_hash_fn=lambda _payload: "0" * 64,
        )

    with pytest.raises(TypeError):
        QuoteRef.__post_init__(
            quote,
            _finite_decimal_fn=lambda value, *_args, **_kwargs: value,
        )

    with pytest.raises(TypeError):
        QuoteRef.quote_key.fget(
            quote,
            _quote_key_fn=lambda *_args: "attacker|quote|key",
        )

    with pytest.raises(TypeError):
        Opportunity.opportunity_id.fget(
            opportunity,
            _canonical_json_hash_fn=lambda _payload: "0" * 64,
        )

    with pytest.raises(TypeError):
        PlanAllocation.__post_init__(
            allocation,
            _finite_decimal_fn=lambda value, *_args, **_kwargs: value,
        )

