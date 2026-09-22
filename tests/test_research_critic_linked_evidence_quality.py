from __future__ import annotations

from decimal import Decimal

from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.forecasting import ForecastRecord
from autosport.research_pipeline import (
    DeterministicResearchCritic,
    ResearchDecisionPolicy,
    ResearchEvidence,
)


def test_blocked_forecast_linked_evidence_cannot_hide_behind_clean_latest_row() -> None:
    quote_key = "event-1|market-1|selection-1"
    snapshot_hash = "a" * 64
    blocked_hash = "1" * 64
    clean_hash = "2" * 64

    candidate = ParlayCandidate(
        (
            CandidateLeg(
                quote_key,
                "event-1",
                Decimal("2.00"),
                Decimal("0.60"),
                "market-1",
                "selection-1",
            ),
        ),
        Decimal("2.00"),
        Decimal("0.60"),
        Decimal("0.20"),
    )
    forecast = ForecastRecord(
        quote_key=quote_key,
        probability=Decimal("0.60"),
        model_id="model-1",
        model_version="1",
        strategy_version="1",
        model_training_cutoff_ts="2026-09-22T09:00:00+00:00",
        input_cutoff_ts="2026-09-22T10:02:00+00:00",
        generated_at="2026-09-22T10:02:00+00:00",
        uncertainty=Decimal("0.10"),
        evidence_hashes=(blocked_hash, clean_hash),
        market_snapshot_hash=snapshot_hash,
        provenance={},
        forecast_id="forecast-1",
    )
    evidence = (
        ResearchEvidence(
            evidence_id="evidence-blocked",
            quote_key=quote_key,
            source_id="provider",
            observed_at="2026-09-22T10:00:00+00:00",
            available_at="2026-09-22T10:00:00+00:00",
            decimal_odds=Decimal("2.00"),
            content_sha256=blocked_hash,
            quality_flags=("GAP_DETECTED",),
            market_snapshot_hash=snapshot_hash,
        ),
        ResearchEvidence(
            evidence_id="evidence-clean-latest",
            quote_key=quote_key,
            source_id="provider",
            observed_at="2026-09-22T10:01:00+00:00",
            available_at="2026-09-22T10:01:00+00:00",
            decimal_odds=Decimal("2.00"),
            content_sha256=clean_hash,
            quality_flags=(),
            market_snapshot_hash=snapshot_hash,
        ),
    )

    verdict = DeterministicResearchCritic(
        ResearchDecisionPolicy(minimum_evidence_per_leg=2)
    ).review(
        candidate,
        {quote_key: forecast},
        evidence,
        decision_ts="2026-09-22T10:03:00+00:00",
    )

    assert verdict.approved is False
    assert any(
        "blocked data-quality flags" in reason and "GAP_DETECTED" in reason
        for reason in verdict.reasons
    )
