from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
import subprocess
import sys

import pytest

from autosport.forecasting import ForecastRecord
from autosport.team_residual_model import TeamMarketInput, TeamResidualConfig, TeamResidualModel


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(day):
    return (T0 + timedelta(days=day)).isoformat()


def item(index, **changes):
    return TeamMarketInput(**{"match_id": str(index), "home_id": "a", "away_id": "b",
                             "available_at": at(index), "source_sha256": "a" * 64,
                             "market_probabilities": (Decimal("0.4"), Decimal("0.2"), Decimal("0.4")),
                             **changes})


def model():
    return TeamResidualModel(TeamResidualConfig(minimum_team_observations=2), created_at=at(-1))


def test_past_team_results_change_predictions_and_reduce_repeated_label_loss():
    learner = model()
    before = learner.predict(item(0))
    for index in range(10):
        learner.learn(item(index), result="1", available_at=at(index + 0.5))
    prediction = learner.predict(item(11))
    assert prediction[0] > before[0]
    assert prediction[2] < before[2]
    assert sum(prediction) == 1
    assert -prediction[0].ln() < -before[0].ln()
    assert all(abs(w) <= Decimal("0.5") for w in learner.weights.values())


def test_new_team_falls_back_and_cannot_inherit_other_team_support():
    learner = model()
    for index in range(3):
        learner.learn(item(index), result="1", available_at=at(index + 0.5))
    unknown = item(4, home_id="unseen")
    assert not learner.supported(unknown)
    assert learner.predict(unknown) == unknown.market_probabilities


def test_causal_boundaries_and_duplicates_reject_without_mutation():
    learner = model()
    learner.learn(item(0), result="1", available_at=at(2))
    before = learner.payload()
    for operation in (
        lambda: learner.learn(item(0), result="2", available_at=at(3)),
        lambda: learner.learn(item(3), result="2", available_at=at(3)),
        lambda: learner.learn(item(0.2), result="X", available_at=at(1)),
        lambda: learner.predict(item(1)),
        lambda: learner.predict(item(2)),
        lambda: learner.predict(replace(item(0), available_at=at(4))),
    ):
        with pytest.raises(ValueError):
            operation()
        assert learner.payload() == before


def test_checkpoint_resumes_same_updates_and_canonical_forecasts(tmp_path):
    learner = model()
    for index in range(3):
        learner.learn(item(index), result="1", available_at=at(index + 0.5))
    payload = json.loads(json.dumps(learner.payload()))
    resumed = TeamResidualModel.restore(payload)
    for current in (learner, resumed):
        current.learn(item(4), result="2", available_at=at(4.5))
    assert learner.payload() == resumed.payload()
    forecasts = resumed.forecasts(item(6))
    assert all(type(f) is ForecastRecord and f.uncertainty == 1 for f in forecasts)
    assert all(f.provenance["promotion_authorized"] is False for f in forecasts)
    assert [f.canonical_hash for f in forecasts] == [f.canonical_hash for f in learner.forecasts(item(6))]
    path = tmp_path / "model.json"
    path.write_text(json.dumps(resumed.payload()))
    child = subprocess.run([sys.executable, "-c",
        "import sys,json; from autosport.team_residual_model import TeamResidualModel as M; "
        "print(json.dumps(M.restore(json.load(open(sys.argv[1]))).payload(),sort_keys=True))", str(path)],
        check=True, capture_output=True, text=True)
    assert json.loads(child.stdout) == resumed.payload()


def test_training_is_independent_of_callers_decimal_precision():
    a, b = model(), model()
    for index in range(10):
        a.learn(item(index), result="1", available_at=at(index + 0.5))
        with localcontext() as ctx:
            ctx.prec = 5
            b.learn(item(index), result="1", available_at=at(index + 0.5))
    assert a.payload() == b.payload()
    assert a.predict(item(11)) == b.predict(item(11))


@pytest.mark.parametrize("field,value", [("promotion_authorized", True), ("schema_version", True),
                                       ("real_money_execution", True), ("seen", ["0", "0"])])
def test_invalid_checkpoint_rejected(field, value):
    learner = model()
    learner.learn(item(0), result="X", available_at=at(1))
    payload = learner.payload()
    payload[field] = value
    with pytest.raises(ValueError):
        TeamResidualModel.restore(payload)


@pytest.mark.parametrize("values", [(Decimal("NaN"), Decimal("0.2"), Decimal("0.8")),
                                    (Decimal("0.5"), Decimal("0.5"), Decimal("0.5"))])
def test_invalid_market_probabilities(values):
    with pytest.raises(ValueError):
        item(0, market_probabilities=values)


def test_research_forecasts_cannot_bypass_existing_uncertainty_critic():
    from autosport.candidate_search import CandidateLeg, ParlayCandidate
    from autosport.research_pipeline import DeterministicResearchCritic, ResearchEvidence

    learner = model()
    for index in range(4):
        learner.learn(item(index), result="1", available_at=at(index + 0.5))
    forecast = learner.forecasts(item(6))[0]
    leg = CandidateLeg(forecast.quote_key, "6", Decimal("3"), forecast.probability)
    candidate = ParlayCandidate((leg,), Decimal("3"), forecast.probability,
                                forecast.probability * 3 - 1)
    evidence = ResearchEvidence(
        evidence_id="test-input", quote_key=forecast.quote_key, source_id="fixture",
        observed_at=at(6), available_at=at(6), decimal_odds=Decimal("3"),
        content_sha256=forecast.evidence_hashes[1], market_snapshot_hash=forecast.market_snapshot_hash,
    )
    verdict = DeterministicResearchCritic().review(
        candidate, {forecast.quote_key: forecast}, (evidence,), decision_ts=at(6.1))
    assert not verdict.approved
    assert verdict.reasons == (f"{forecast.quote_key}: forecast uncertainty exceeds policy",)
