from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.forecasting import ForecastRecord
from autosport.research_pipeline import ResearchEvidence
from autosport.research_strategy import ResearchReplayInstruction, ResearchStrategyPlan
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
