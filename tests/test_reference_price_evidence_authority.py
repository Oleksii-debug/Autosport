from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json

import pytest

from autosport.domain import MarketEvent, MarketType
from autosport.reference_price_evidence import (
    ReferenceObservation,
    ReferencePriceEvidenceError,
    ReferencePriceProtocol,
    ReferenceTargetInclusionPolicy,
    build_reference_price_evidence,
)


_DECISION = "2026-09-21T08:30:10+00:00"


def _event(
    source: str,
    odds: str = "2.0",
    *,
    source_ts: str | None = "2026-09-21T08:29:59+00:00",
    observed_ts: str = "2026-09-21T08:30:00+00:00",
    ingest_ts: str = "2026-09-21T08:30:01+00:00",
) -> MarketEvent:
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        decimal_odds=Decimal(odds),
        observed_ts=observed_ts,
        source_id=source,
        sequence=1,
        market_type=MarketType.WINNER,
        status="open",
        source_ts=source_ts,
        ingest_ts=ingest_ts,
        metadata={
            "price_semantics": "best_available_to_back",
            "execution_quote_verified": False,
            "bookmaker_key": source,
        },
        sport="table_tennis",
        market_semantics_id="winner.match.v1",
    )


def _build(events: tuple[MarketEvent, ...]):
    source_ids = tuple(sorted({event.source_id for event in events}))
    price_source_ids = tuple(
        sorted({event.metadata["bookmaker_key"] for event in events})
    )
    protocol = ReferencePriceProtocol(
        eligible_source_ids=source_ids,
        eligible_price_source_ids=price_source_ids,
        target_source_id="target-provider",
        target_inclusion_policy=ReferenceTargetInclusionPolicy.EXCLUDE,
        price_semantics="best_available_to_back",
        max_age_seconds=30,
        max_skew_seconds=5,
        minimum_sources=2,
    )
    return build_reference_price_evidence(
        events,
        decision_ts=_DECISION,
        protocol=protocol,
    )


def _canonical_event_json(event: MarketEvent) -> str:
    return json.dumps(
        event.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def test_recent_receipt_cannot_substitute_for_missing_provider_source_time() -> None:
    with pytest.raises(
        ReferencePriceEvidenceError,
        match="authoritative provider source_ts",
    ):
        _build((_event("provider-a", source_ts=None), _event("provider-b", source_ts=None)))


def test_derived_statistics_and_evidence_id_are_not_caller_replaceable() -> None:
    evidence = _build((_event("provider-a", "2.0"), _event("provider-b", "2.2")))

    with pytest.raises((TypeError, ValueError)):
        replace(evidence, median_decimal_odds=Decimal("999"))
    with pytest.raises((TypeError, ValueError)):
        replace(evidence, lower_median_decimal_odds=Decimal("999"))
    with pytest.raises((TypeError, ValueError)):
        replace(evidence, upper_median_decimal_odds=Decimal("999"))
    with pytest.raises((TypeError, ValueError)):
        replace(evidence, evidence_id="f" * 64)


def test_direct_identity_substitution_revalidates_exact_component_bytes() -> None:
    evidence = _build((_event("provider-a"), _event("provider-b")))

    with pytest.raises(
        ReferencePriceEvidenceError,
        match="one exact market selection",
    ):
        replace(evidence, selection_id="selection-evil")


def test_observation_requires_canonical_event_bytes() -> None:
    canonical = _canonical_event_json(_event("provider-a"))
    observation = ReferenceObservation(canonical)

    assert len(observation.event_sha256) == 64
    with pytest.raises(ReferencePriceEvidenceError, match="canonical JSON"):
        ReferenceObservation(canonical.replace("{", "{ ", 1))


def test_observation_hash_and_price_cannot_be_forged_independently() -> None:
    observation = ReferenceObservation(_canonical_event_json(_event("provider-a")))

    with pytest.raises((TypeError, ValueError)):
        replace(observation, event_sha256="f" * 64)
    with pytest.raises((TypeError, ValueError)):
        replace(observation, decimal_odds=Decimal("999"))


def test_valid_component_change_recomputes_statistics_and_identity() -> None:
    baseline = _build((_event("provider-a", "2.0"), _event("provider-b", "2.2")))
    revised = _build((_event("provider-a", "2.0"), _event("provider-b", "2.4")))

    assert baseline.evidence_id != revised.evidence_id
    assert baseline.median_decimal_odds is None
    assert baseline.lower_median_decimal_odds == Decimal("2.0")
    assert baseline.upper_median_decimal_odds == Decimal("2.2")
    assert revised.median_decimal_odds is None
    assert revised.lower_median_decimal_odds == Decimal("2.0")
    assert revised.upper_median_decimal_odds == Decimal("2.4")


def test_provider_source_time_remains_the_freshness_authority() -> None:
    old_upstream = _event(
        "provider-b",
        "2.1",
        source_ts="2026-09-21T08:20:00+00:00",
    )

    with pytest.raises(ReferencePriceEvidenceError, match="source_ts is stale"):
        _build((_event("provider-a"), old_upstream))


def test_direct_observation_without_provider_source_time_is_rejected() -> None:
    missing = _event("provider-a", source_ts=None)

    with pytest.raises(
        ReferencePriceEvidenceError,
        match="authoritative provider source_ts",
    ):
        ReferenceObservation(_canonical_event_json(missing))


def test_observation_rejects_canonical_json_with_unknown_event_fields() -> None:
    payload = _event("provider-a").to_dict()
    payload["caller_only_shadow"] = "not-part-of-MarketEvent"
    canonical_with_extra = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    with pytest.raises(
        ReferencePriceEvidenceError,
        match="exact canonical MarketEvent bytes",
    ):
        ReferenceObservation(canonical_with_extra)
