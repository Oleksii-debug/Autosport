from __future__ import annotations

from dataclasses import replace

import pytest

from autosport.matchbook_heartbeat_safety import (
    MatchbookCancellationReason,
    MatchbookHeartbeatCancellationObservation,
    MatchbookHeartbeatLeaseState,
    MatchbookHeartbeatRegistration,
    MatchbookHeartbeatSafetyError,
    register_after_provider_success,
)

SHA_A = "a" * 64
AT_1 = "2026-09-22T03:52:00Z"
AT_2 = "2026-09-22T03:53:00Z"


def _lease(*, requested: int = 20, effective: int = 30, generation: int = 7):
    return register_after_provider_success(
        session_generation=generation,
        requested_timeout_seconds=requested,
        effective_timeout_seconds=effective,
        registered_at=AT_1,
        now_monotonic=100.0,
    )


def test_provider_effective_timeout_not_requested_timeout_controls_expiry() -> None:
    lease = _lease(requested=20, effective=45)
    assert lease.expires_monotonic == 145.0
    assert (
        lease.state(now_monotonic=120.0, current_session_generation=7)
        is MatchbookHeartbeatLeaseState.ACTIVE
    )
    assert (
        lease.state(now_monotonic=144.999, current_session_generation=7)
        is MatchbookHeartbeatLeaseState.ACTIVE
    )
    assert (
        lease.state(now_monotonic=145.0, current_session_generation=7)
        is MatchbookHeartbeatLeaseState.EXPIRED
    )


def test_shorter_provider_timeout_fails_closed_even_when_request_was_longer() -> None:
    lease = _lease(requested=60, effective=10)
    assert (
        lease.state(now_monotonic=109.0, current_session_generation=7)
        is MatchbookHeartbeatLeaseState.ACTIVE
    )
    assert (
        lease.state(now_monotonic=110.0, current_session_generation=7)
        is MatchbookHeartbeatLeaseState.EXPIRED
    )
    assert (
        lease.remaining_seconds(
            now_monotonic=111.0,
            current_session_generation=7,
        )
        == 0.0
    )


def test_session_rotation_invalidates_old_lease() -> None:
    lease = _lease()
    assert (
        lease.state(now_monotonic=101.0, current_session_generation=8)
        is MatchbookHeartbeatLeaseState.SESSION_ROTATED
    )
    assert (
        lease.remaining_seconds(
            now_monotonic=101.0,
            current_session_generation=8,
        )
        == 0.0
    )


def test_monotonic_clock_rollback_fails_closed() -> None:
    lease = _lease()
    with pytest.raises(MatchbookHeartbeatSafetyError, match="moved backwards"):
        lease.state(now_monotonic=99.0, current_session_generation=7)


def test_refresh_uses_new_provider_timeout_and_chains_predecessor() -> None:
    first = _lease(requested=20, effective=40)
    second = first.refreshed_after_provider_success(
        requested_timeout_seconds=20,
        effective_timeout_seconds=12,
        registered_at=AT_2,
        now_monotonic=110.0,
        current_session_generation=7,
    )
    assert (
        second.registration.predecessor_registration_id
        == first.registration.registration_id
    )
    assert second.registration.requested_timeout_seconds == 20
    assert second.registration.effective_timeout_seconds == 12
    assert second.expires_monotonic == 122.0
    assert (
        second.state(now_monotonic=121.0, current_session_generation=7)
        is MatchbookHeartbeatLeaseState.ACTIVE
    )
    assert (
        second.state(now_monotonic=122.0, current_session_generation=7)
        is MatchbookHeartbeatLeaseState.EXPIRED
    )


def test_refresh_cannot_cross_session_generation() -> None:
    lease = _lease()
    with pytest.raises(
        MatchbookHeartbeatSafetyError,
        match="different session generation",
    ):
        lease.refreshed_after_provider_success(
            requested_timeout_seconds=20,
            effective_timeout_seconds=20,
            registered_at=AT_2,
            now_monotonic=110.0,
            current_session_generation=8,
        )


def test_durable_registration_round_trip_excludes_secret_and_monotonic_state() -> None:
    registration = _lease().registration
    payload = registration.to_dict()
    assert MatchbookHeartbeatRegistration.from_dict(payload) == registration
    serialized_keys = " ".join(payload).lower()
    assert "token" not in serialized_keys
    assert "monotonic" not in serialized_keys
    assert "account" not in serialized_keys
    assert payload["registration_id"] == registration.registration_id


def test_restart_never_reconstructs_active_heartbeat_authority() -> None:
    registration = MatchbookHeartbeatRegistration.from_dict(
        _lease().registration.to_dict()
    )
    assert (
        registration.state_after_restart(current_session_generation=7)
        is MatchbookHeartbeatLeaseState.RESTART_REQUIRES_REREGISTRATION
    )
    assert (
        registration.state_after_restart(current_session_generation=8)
        is MatchbookHeartbeatLeaseState.SESSION_ROTATED
    )


def test_registration_digest_detects_effective_timeout_tamper() -> None:
    raw = _lease().registration.to_dict()
    raw["effective_timeout_seconds"] = 300
    with pytest.raises(ValueError, match="digest mismatch"):
        MatchbookHeartbeatRegistration.from_dict(raw)


def test_registration_parser_rejects_unknown_fields() -> None:
    raw = _lease().registration.to_dict()
    raw["session-token"] = "must-never-be-stored"
    with pytest.raises(ValueError, match="fields mismatch"):
        MatchbookHeartbeatRegistration.from_dict(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("session_generation", True),
        ("session_generation", 0),
        ("requested_timeout_seconds", False),
        ("requested_timeout_seconds", 0),
        ("effective_timeout_seconds", 0),
        ("effective_timeout_seconds", 1.5),
    ),
)
def test_registration_rejects_ambiguous_or_nonpositive_integer_inputs(
    field: str,
    value: object,
) -> None:
    kwargs = dict(
        session_generation=7,
        requested_timeout_seconds=20,
        effective_timeout_seconds=30,
        registered_at=AT_1,
    )
    kwargs[field] = value
    with pytest.raises(ValueError, match="positive integer"):
        MatchbookHeartbeatRegistration(**kwargs)


def test_registration_rejects_noncanonical_utc_timestamp() -> None:
    with pytest.raises(ValueError, match="canonical"):
        MatchbookHeartbeatRegistration(
            session_generation=7,
            requested_timeout_seconds=20,
            effective_timeout_seconds=30,
            registered_at="2026-09-22T05:52:00+02:00",
        )


def test_successful_unsubscribe_does_not_claim_offer_cancellation() -> None:
    lease = _lease()
    stopped, evidence = lease.mark_unsubscribed_after_provider_success(
        unsubscribed_at=AT_2,
        now_monotonic=110.0,
        current_session_generation=7,
    )
    assert (
        stopped.state(now_monotonic=111.0, current_session_generation=7)
        is MatchbookHeartbeatLeaseState.UNSUBSCRIBED
    )
    assert evidence.offers_cancelled_proven is False
    assert evidence.to_dict()["offers_cancelled_proven"] is False
    assert evidence.registration_id == lease.registration.registration_id


def test_unsubscribe_cannot_be_replayed_or_cross_session() -> None:
    lease = _lease()
    stopped, _ = lease.mark_unsubscribed_after_provider_success(
        unsubscribed_at=AT_2,
        now_monotonic=110.0,
        current_session_generation=7,
    )
    with pytest.raises(
        MatchbookHeartbeatSafetyError,
        match="already unsubscribed",
    ):
        stopped.mark_unsubscribed_after_provider_success(
            unsubscribed_at="2026-09-22T03:54:00Z",
            now_monotonic=120.0,
            current_session_generation=7,
        )
    with pytest.raises(
        MatchbookHeartbeatSafetyError,
        match="different session generation",
    ):
        lease.mark_unsubscribed_after_provider_success(
            unsubscribed_at=AT_2,
            now_monotonic=110.0,
            current_session_generation=8,
        )


def test_provider_heartbeat_expiry_cancellation_is_explicit_evidence() -> None:
    observation = MatchbookHeartbeatCancellationObservation(
        offer_id=123,
        cancellation_reason=MatchbookCancellationReason.HEARTBEAT_EXPIRY,
        observed_at=AT_2,
        provider_observation_sha256=SHA_A,
    )
    assert observation.heartbeat_expiry_proven is True
    assert observation.to_dict()["cancellation_reason"] == "heartbeat_expiry"


def test_user_request_cancellation_is_not_relabelled_heartbeat_expiry() -> None:
    observation = MatchbookHeartbeatCancellationObservation(
        offer_id=123,
        cancellation_reason=MatchbookCancellationReason.USER_REQUEST,
        observed_at=AT_2,
        provider_observation_sha256=SHA_A,
    )
    assert observation.heartbeat_expiry_proven is False


def test_cancellation_observation_requires_typed_reason_and_digest() -> None:
    with pytest.raises(ValueError, match="MatchbookCancellationReason"):
        MatchbookHeartbeatCancellationObservation(
            offer_id=123,
            cancellation_reason="heartbeat_expiry",  # type: ignore[arg-type]
            observed_at=AT_2,
            provider_observation_sha256=SHA_A,
        )
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        MatchbookHeartbeatCancellationObservation(
            offer_id=123,
            cancellation_reason=MatchbookCancellationReason.HEARTBEAT_EXPIRY,
            observed_at=AT_2,
            provider_observation_sha256="not-a-digest",
        )


def test_registration_identity_changes_for_material_provider_truth() -> None:
    first = _lease(requested=20, effective=20).registration
    changed_effective = replace(first, effective_timeout_seconds=21)
    changed_generation = replace(first, session_generation=8)
    assert changed_effective.registration_id != first.registration_id
    assert changed_generation.registration_id != first.registration_id
