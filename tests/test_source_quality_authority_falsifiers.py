from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.source_quality_confidence import (
    ConfidenceAction,
    SourceClass,
    SourceQualityAssessment,
    SourceQualityObservation,
    SourceQualityPolicy,
    assess_source_quality,
)


_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _policy() -> SourceQualityPolicy:
    return SourceQualityPolicy(
        max_age=timedelta(seconds=30),
        accept_confidence=Decimal("0.80"),
        downweight_confidence=Decimal("0.50"),
        schema_version=1,
    )


def _browser_observation(*, corroborator_ids: tuple[str, ...] = ()) -> SourceQualityObservation:
    return SourceQualityObservation(
        provider_id="provider-a",
        source_id="browser-source-a",
        evidence_id="browser-evidence-a",
        source_class=SourceClass.BROWSER_ADAPTER,
        observed_at=_NOW - timedelta(seconds=1),
        base_confidence=Decimal("0.90"),
        schema_version=1,
        transport_verified=True,
        provenance_bound=True,
        provenance_sha256="a" * 64,
        corroborator_ids=corroborator_ids,
    )


def test_caller_cannot_directly_construct_positive_accept_authority() -> None:
    """A public DTO must not let arbitrary callers manufacture positive authority.

    Removing ACCEPT until a durable authority exists is valid. Keeping ACCEPT is
    also valid only if direct public construction of a positive assessment fails.
    """

    accept = getattr(ConfidenceAction, "ACCEPT", None)
    if accept is None:
        return

    with pytest.raises((TypeError, ValueError)):
        SourceQualityAssessment(
            action=accept,
            effective_confidence=Decimal("1"),
            reasons=(),
            corroborated=True,
        )


def test_unvalidated_corroborator_ids_cannot_mint_positive_corroboration_fact() -> None:
    """Caller-declared identities are not independent corroboration authority."""

    result = assess_source_quality(
        _browser_observation(corroborator_ids=("made-up-peer",)),
        now=_NOW,
        policy=_policy(),
    )

    # A safe repair may remove/rename the positive corroboration field entirely.
    # If the authority-bearing field remains, caller declarations alone must not
    # make it true; independent validation must be part of the assessment path.
    assert getattr(result, "corroborated", False) is False
