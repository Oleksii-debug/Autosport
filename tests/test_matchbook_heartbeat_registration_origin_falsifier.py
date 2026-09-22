from autosport.matchbook_heartbeat_safety import (
    MatchbookHeartbeatLease,
    MatchbookHeartbeatLeaseState,
    MatchbookHeartbeatRegistration,
    register_after_provider_success,
)


def test_detached_registration_and_lease_cannot_mint_active_liveness() -> None:
    registration = MatchbookHeartbeatRegistration(
        session_generation=7,
        requested_timeout_seconds=30,
        effective_timeout_seconds=30,
        registered_at="2026-09-22T06:30:00Z",
    )
    detached = MatchbookHeartbeatLease(
        registration=registration,
        registered_monotonic=100.0,
    )

    assert (
        detached.state(
            now_monotonic=101.0,
            current_session_generation=7,
        )
        is not MatchbookHeartbeatLeaseState.ACTIVE
    )


def test_public_success_named_helper_cannot_substitute_for_provider_success() -> None:
    caller_minted = register_after_provider_success(
        session_generation=7,
        requested_timeout_seconds=30,
        effective_timeout_seconds=300,
        registered_at="2026-09-22T06:30:00Z",
        now_monotonic=100.0,
    )

    assert (
        caller_minted.state(
            now_monotonic=101.0,
            current_session_generation=7,
        )
        is not MatchbookHeartbeatLeaseState.ACTIVE
    )
