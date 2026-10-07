from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.domain import MarketEvent
from autosport.forecasting import ForecastRecord
from autosport.research_pipeline import ResearchEvidence
from autosport.research_strategy import (
    ResearchReplayInstruction,
    ResearchStrategyPlan,
    market_event_evidence_hash,
    research_market_snapshot_hash,
)
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome

T0 = "2026-10-06T23:00:00+00:00"
T1 = "2026-10-07T00:00:00+00:00"
QUOTE = "event-1|market-1|selection-1"

class _TrapStr(str):
    def _dispatch(self, *_args, **_kwargs):
        raise AssertionError("hostile identity dispatched before exact admission")
    __hash__ = _dispatch
    __eq__ = _dispatch
    __iter__ = _dispatch
    __len__ = _dispatch
    strip = _dispatch
    encode = _dispatch

def _instruction() -> ResearchReplayInstruction:
    leg = CandidateLeg(QUOTE, "event-1", Decimal("2"), Decimal("0.5"), "market-1", "selection-1")
    candidate = ParlayCandidate((leg,), Decimal("2"), Decimal("0.5"), Decimal("0"))
    forecast = ForecastRecord(
        quote_key=QUOTE, probability=Decimal("0.5"), model_id="model-1",
        model_version="v1", strategy_version="strategy-v1",
        model_training_cutoff_ts=T0, input_cutoff_ts=T1, generated_at=T1,
        forecast_id="forecast-1",
    )
    evidence = ResearchEvidence(
        evidence_id="evidence-1", quote_key=QUOTE, source_id="provider-1",
        observed_at=T1, available_at=T1, decimal_odds=Decimal("2"),
        content_sha256="a" * 64, quality_flags=(),
    )
    group = ScenarioGroup("market-1", (
        ScenarioOutcome(QUOTE, Decimal("0.5")),
        ScenarioOutcome("event-1|market-1|selection-2", Decimal("0.5")),
    ))
    return ResearchReplayInstruction(
        "decision-1", QUOTE, T1, Decimal("1"), candidate,
        (group,), (forecast,), (evidence,),
    )

def test_instruction_rejects_candidate_subclass_before_legs_dispatch() -> None:
    base = _instruction().candidate
    class HostileCandidate(ParlayCandidate):
        armed = False
        def __getattribute__(self, name: str):
            if name == "legs" and type(self).armed:
                raise AssertionError("candidate legs dispatched before exact admission")
            return super().__getattribute__(name)
    hostile = HostileCandidate(
        base.legs, base.combined_odds, base.independent_probability,
        base.expected_profit_per_unit,
    )
    HostileCandidate.armed = True
    with pytest.raises(TypeError, match="exact ParlayCandidate"):
        replace(_instruction(), candidate=hostile)

def test_instruction_revalidates_mutated_candidate_leg_before_hashing() -> None:
    instruction = _instruction()
    object.__setattr__(instruction.candidate.legs[0], "quote_key", _TrapStr(QUOTE))
    with pytest.raises(ValueError, match="candidate leg quote_key"):
        ResearchReplayInstruction.__post_init__(instruction)

def test_instruction_rejects_forecast_subclass_before_quote_dispatch() -> None:
    base = _instruction().forecasts[0]
    class HostileForecast(ForecastRecord):
        armed = False
        def __getattribute__(self, name: str):
            if name == "quote_key" and type(self).armed:
                raise AssertionError("forecast quote dispatched before exact admission")
            return super().__getattribute__(name)
    hostile = HostileForecast(
        quote_key=base.quote_key, probability=base.probability,
        model_id=base.model_id, model_version=base.model_version,
        strategy_version=base.strategy_version,
        model_training_cutoff_ts=base.model_training_cutoff_ts,
        input_cutoff_ts=base.input_cutoff_ts, generated_at=base.generated_at,
        uncertainty=base.uncertainty, evidence_hashes=base.evidence_hashes,
        market_snapshot_hash=base.market_snapshot_hash, provenance={},
        forecast_id=base.forecast_id,
    )
    HostileForecast.armed = True
    with pytest.raises(TypeError, match="exact ForecastRecord"):
        replace(_instruction(), forecasts=(hostile,))

def test_forecasts_by_quote_revalidates_mutated_forecast_before_hashing() -> None:
    instruction = _instruction()
    object.__setattr__(instruction.forecasts[0], "quote_key", _TrapStr(QUOTE))
    with pytest.raises(ValueError, match="quote_key"):
        _ = instruction.forecasts_by_quote

def test_plan_requires_exact_tuple_and_revalidates_instruction() -> None:
    instruction = _instruction()
    with pytest.raises(ValueError, match="non-empty exact tuple"):
        ResearchStrategyPlan([instruction], "b" * 64)  # type: ignore[arg-type]
    object.__setattr__(instruction, "trigger_quote_key", _TrapStr(QUOTE))
    with pytest.raises(ValueError, match="trigger_quote_key"):
        ResearchStrategyPlan((instruction,), "b" * 64)


def _market_event() -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal("2"),
        observed_ts=T1,
        source_id="provider-1",
        sequence=1,
        ingest_ts=T1,
    )


def test_market_evidence_hash_rejects_event_subclass_before_member_dispatch() -> None:
    base = _market_event()

    class HostileEvent(MarketEvent):
        armed = False

        def __getattribute__(self, name: str):
            if name == "event_id" and type(self).armed:
                raise AssertionError("MarketEvent identity dispatched before exact admission")
            return super().__getattribute__(name)

    hostile = HostileEvent(
        event_id=base.event_id,
        market_id=base.market_id,
        selection_id=base.selection_id,
        decimal_odds=base.decimal_odds,
        observed_ts=base.observed_ts,
        source_id=base.source_id,
        sequence=base.sequence,
        market_type=base.market_type,
        status=base.status,
        source_ts=base.source_ts,
        ingest_ts=base.ingest_ts,
        score_state=base.score_state,
        metadata=base.metadata,
    )
    HostileEvent.armed = True
    with pytest.raises(TypeError, match="exact MarketEvent"):
        market_event_evidence_hash(hostile)


def test_market_evidence_hash_revalidates_post_construction_identity() -> None:
    event = _market_event()
    object.__setattr__(event, "selection_id", _TrapStr("selection-1"))
    with pytest.raises(ValueError, match="selection_id"):
        market_event_evidence_hash(event)


def test_snapshot_hash_rejects_mapping_subclass_before_get_dispatch() -> None:
    event = _market_event()

    class HostileQuotes(dict):
        def get(self, *_args, **_kwargs):
            raise AssertionError("mapping get dispatched before exact admission")

    with pytest.raises(TypeError, match="exact dict"):
        research_market_snapshot_hash(
            HostileQuotes({event.quote_key: event}),
            [event.quote_key],
        )


def test_snapshot_hash_validates_quote_key_before_hash_dispatch() -> None:
    event = _market_event()
    with pytest.raises(ValueError, match="snapshot quote_key"):
        research_market_snapshot_hash(
            {event.quote_key: event},
            [_TrapStr(event.quote_key)],
        )


def test_snapshot_hash_rejects_mapping_key_identity_mismatch() -> None:
    event = _market_event()
    with pytest.raises(ValueError, match="mapping key does not match"):
        research_market_snapshot_hash(
            {"event-1|market-1|different-selection": event},
            ["event-1|market-1|different-selection"],
        )
