from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from decimal import Decimal

from autosport.source_quality_confidence import (
    ConfidenceAction,
    SourceClass,
    SourceQualityObservation,
    SourceQualityPolicy,
    assess_source_quality,
)


class _FoldOffsetTimezone(tzinfo):
    """Deterministic fold-sensitive tzinfo without external timezone data."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(hours=-4)
        return timedelta(hours=-4 if dt.fold == 0 else -5)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "FOLD_TEST"


def test_freshness_age_uses_absolute_instants_across_repeated_local_time_fold() -> None:
    tz = _FoldOffsetTimezone()
    observed_at = datetime(2026, 11, 1, 1, 30, tzinfo=tz, fold=0)
    now = datetime(2026, 11, 1, 1, 30, tzinfo=tz, fold=1)

    # Same wall-clock fields, but fold=1 is one real UTC hour after fold=0.
    assert observed_at.utcoffset() == timedelta(hours=-4)
    assert now.utcoffset() == timedelta(hours=-5)

    observation = SourceQualityObservation(
        provider_id="provider-a",
        source_id="browser-a",
        evidence_id="evidence-a",
        source_class=SourceClass.BROWSER_ADAPTER,
        observed_at=observed_at,
        base_confidence=Decimal("0.90"),
        schema_version=1,
        transport_verified=True,
        provenance_bound=True,
        provenance_sha256="a" * 64,
    )
    policy = SourceQualityPolicy(
        max_age=timedelta(minutes=30),
        accept_confidence=Decimal("0.80"),
        downweight_confidence=Decimal("0.50"),
        schema_version=1,
    )

    result = assess_source_quality(observation, now=now, policy=policy)

    assert result.action is ConfidenceAction.ABSTAIN
    assert "STALE_OBSERVATION" in result.reasons
