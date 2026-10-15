from __future__ import annotations

from decimal import Decimal

from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.forecasting import ForecastRecord
from autosport.research_pipeline import (
    DeterministicResearchCritic,
    ResearchDecisionPolicy,
    ResearchEvidence,
)


QUOTE_KEY = "event-1|market-1|selection-1"
SNAPSHOT_HASH = "a" * 64


def _candidate() -> ParlayCandidate:
    return ParlayCandidate(
        (
            CandidateLeg(
                QUOTE_KEY,
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


def _forecast(*evidence_hashes: str) -> ForecastRecord:
    return ForecastRecord(
        quote_key=QUOTE_KEY,
        probability=Decimal("0.60"),
        model_id="model-1",
        model_version="1",
        strategy_version="1",
        model_training_cutoff_ts="2026-09-22T09:00:00+00:00",
        input_cutoff_ts="2026-09-22T10:02:00+00:00",
        generated_at="2026-09-22T10:02:00+00:00",
        uncertainty=Decimal("0.10"),
        evidence_hashes=evidence_hashes,
        market_snapshot_hash=SNAPSHOT_HASH,
        provenance={},
        forecast_id="forecast-1",
    )


def _evidence(
    *,
    evidence_id: str,
    content_sha256: str,
    observed_at: str,
    quality_flags: tuple[str, ...] = (),
) -> ResearchEvidence:
    return ResearchEvidence(
        evidence_id=evidence_id,
        quote_key=QUOTE_KEY,
        source_id="provider",
        observed_at=observed_at,
        available_at=observed_at,
        decimal_odds=Decimal("2.00"),
        content_sha256=content_sha256,
        quality_flags=quality_flags,
        market_snapshot_hash=SNAPSHOT_HASH,
    )


def _review(
    forecast: ForecastRecord,
    evidence: tuple[ResearchEvidence, ...],
):
    return DeterministicResearchCritic(
        ResearchDecisionPolicy(minimum_evidence_per_leg=2)
    ).review(
        _candidate(),
        {QUOTE_KEY: forecast},
        evidence,
        decision_ts="2026-09-22T10:03:00+00:00",
    )


def test_blocked_forecast_linked_evidence_cannot_hide_behind_clean_latest_row() -> None:
    blocked_hash = "1" * 64
    clean_hash = "2" * 64
    evidence = (
        _evidence(
            evidence_id="evidence-blocked",
            content_sha256=blocked_hash,
            observed_at="2026-09-22T10:00:00+00:00",
            quality_flags=("GAP_DETECTED",),
        ),
        _evidence(
            evidence_id="evidence-clean-latest",
            content_sha256=clean_hash,
            observed_at="2026-09-22T10:01:00+00:00",
        ),
    )

    verdict = _review(_forecast(blocked_hash, clean_hash), evidence)

    assert verdict.approved is False
    assert any(
        "blocked data-quality flags" in reason and "GAP_DETECTED" in reason
        for reason in verdict.reasons
    )


def test_unlinked_historical_blocked_evidence_does_not_poison_clean_forecast() -> None:
    blocked_hash = "1" * 64
    clean_early_hash = "2" * 64
    clean_latest_hash = "3" * 64
    evidence = (
        _evidence(
            evidence_id="evidence-blocked-unlinked",
            content_sha256=blocked_hash,
            observed_at="2026-09-22T09:59:00+00:00",
            quality_flags=("GAP_DETECTED",),
        ),
        _evidence(
            evidence_id="evidence-clean-early",
            content_sha256=clean_early_hash,
            observed_at="2026-09-22T10:00:00+00:00",
        ),
        _evidence(
            evidence_id="evidence-clean-latest",
            content_sha256=clean_latest_hash,
            observed_at="2026-09-22T10:01:00+00:00",
        ),
    )

    verdict = _review(_forecast(clean_early_hash, clean_latest_hash), evidence)

    assert verdict.approved is True
    assert verdict.reasons == ()


def test_blocked_linked_evidence_rejects_even_when_clean_rows_meet_minimum() -> None:
    blocked_hash = "1" * 64
    clean_early_hash = "2" * 64
    clean_latest_hash = "3" * 64
    evidence = (
        _evidence(
            evidence_id="evidence-blocked-linked",
            content_sha256=blocked_hash,
            observed_at="2026-09-22T09:59:00+00:00",
            quality_flags=("STALE_SOURCE",),
        ),
        _evidence(
            evidence_id="evidence-clean-early",
            content_sha256=clean_early_hash,
            observed_at="2026-09-22T10:00:00+00:00",
        ),
        _evidence(
            evidence_id="evidence-clean-latest",
            content_sha256=clean_latest_hash,
            observed_at="2026-09-22T10:01:00+00:00",
        ),
    )

    verdict = _review(
        _forecast(blocked_hash, clean_early_hash, clean_latest_hash),
        evidence,
    )

    assert verdict.approved is False
    assert any(
        "blocked data-quality flags" in reason and "STALE_SOURCE" in reason
        for reason in verdict.reasons
    )
