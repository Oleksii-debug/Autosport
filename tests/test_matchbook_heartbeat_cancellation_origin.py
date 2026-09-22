from __future__ import annotations

from autosport.matchbook_heartbeat_safety import (
    MatchbookCancellationReason,
    MatchbookHeartbeatCancellationObservation,
)


SHA = "a" * 64
AT = "2026-09-22T06:00:00Z"


def test_detached_cancellation_record_does_not_mint_provider_origin() -> None:
    observation = MatchbookHeartbeatCancellationObservation(
        offer_id=123,
        cancellation_reason=MatchbookCancellationReason.HEARTBEAT_EXPIRY,
        observed_at=AT,
        provider_observation_sha256=SHA,
    )

    # The reason label may be represented durably, but this DTO is publicly
    # constructible and its SHA is caller-supplied. It cannot by itself prove
    # that authenticated Matchbook readback actually emitted this cancellation.
    assert observation.heartbeat_expiry_observed is True
    assert observation.provider_origin_proven is False
    assert observation.offers_cancelled_proven is False

    reloaded = MatchbookHeartbeatCancellationObservation.from_dict(
        observation.to_dict()
    )
    assert reloaded.provider_origin_proven is False
    assert reloaded.offers_cancelled_proven is False
