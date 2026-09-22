from __future__ import annotations

from datetime import datetime, timedelta, timezone

from autosport.provider_time_freshness import (
    ProviderTimeEvidence,
    ProviderTimeStatus,
    assess_provider_time_freshness,
)


def _assess(sequence_id: object):
    evidence = ProviderTimeEvidence(
        source_updated_at="2026-09-22T12:00:00+00:00",
        received_wall_at="2026-09-22T12:00:00.100000+00:00",
        acquisition_started_monotonic_ns=1_000_000,
        received_monotonic_ns=1_100_000,
        sequence_id=sequence_id,
    )
    return evidence, assess_provider_time_freshness(
        evidence,
        decision_at=datetime(2026, 9, 22, 12, 0, 1, tzinfo=timezone.utc),
        max_quote_age=timedelta(seconds=2),
        max_source_clock_skew=timedelta(milliseconds=50),
    )


def test_generic_timing_evidence_preserves_opaque_provider_clock() -> None:
    evidence, result = _assess("xAeG/opaque-initialClk:next")

    assert evidence.sequence_id == "xAeG/opaque-initialClk:next"
    assert result.sequence_id == "xAeG/opaque-initialClk:next"
    assert result.status is ProviderTimeStatus.FRESH


def test_timing_only_freshness_does_not_mint_live_market_eligibility() -> None:
    _, result = _assess("heartbeat-clk-without-market-data")

    assert result.status is ProviderTimeStatus.FRESH
    assert result.timing_fresh is True
    assert result.eligible is False


def test_numeric_sequence_and_equal_text_cursor_are_distinct_evidence() -> None:
    numeric, _ = _assess(17)
    opaque, _ = _assess("17")

    assert numeric.evidence_id != opaque.evidence_id
