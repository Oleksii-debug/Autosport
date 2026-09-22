from __future__ import annotations

from dataclasses import replace

import pytest

from autosport.matchbook_session_lifecycle import (
    MATCHBOOK_APPROX_SESSION_LIFETIME_SECONDS,
    MatchbookSessionLifecycle,
    RequestKind,
    RetryDisposition,
    SessionClockRollbackError,
    SessionLifecycleError,
    SessionReadGenerationTicket,
    SessionState,
)


def ns(seconds: int) -> int:
    return seconds * 1_000_000_000


def active() -> MatchbookSessionLifecycle:
    lifecycle = MatchbookSessionLifecycle()
    lifecycle.record_login_200(generation_id="gen-1", monotonic_ns=ns(10))
    return lifecycle


def test_login_200_creates_runtime_active_generation_without_secret() -> None:
    lifecycle = active()

    assert lifecycle.state is SessionState.ACTIVE
    assert lifecycle.is_active is True
    snapshot = lifecycle.audit_snapshot()
    assert snapshot.generation_id == "gen-1"
    assert not hasattr(snapshot, "session_token")
    assert "token" not in repr(snapshot).lower()


def test_get_session_200_validates_same_generation_without_resetting_login_age() -> None:
    lifecycle = active()

    lifecycle.record_get_session_result(
        generation_id="gen-1", http_status=200, monotonic_ns=ns(100)
    )

    assert lifecycle.state is SessionState.ACTIVE
    assert lifecycle.session_age_hint_seconds(monotonic_ns=ns(110)) == 100.0


def test_get_session_401_expires_generation_and_it_cannot_resurrect() -> None:
    lifecycle = active()
    lifecycle.record_get_session_result(
        generation_id="gen-1", http_status=401, monotonic_ns=ns(20)
    )

    assert lifecycle.state is SessionState.EXPIRED
    assert lifecycle.is_active is False
    with pytest.raises(SessionLifecycleError):
        lifecycle.record_get_session_result(
            generation_id="gen-1", http_status=200, monotonic_ns=ns(21)
        )


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 504])
def test_ambiguous_provider_status_does_not_claim_expiry(status: int) -> None:
    lifecycle = active()

    lifecycle.record_get_session_result(
        generation_id="gen-1", http_status=status, monotonic_ns=ns(20)
    )

    assert lifecycle.state is SessionState.UNKNOWN
    assert lifecycle.is_active is False


def test_network_failure_is_unknown_not_expired() -> None:
    lifecycle = active()

    lifecycle.record_network_failure(generation_id="gen-1", monotonic_ns=ns(20))

    assert lifecycle.state is SessionState.UNKNOWN


def test_read_401_allows_only_bounded_reauth_then_single_read_retry() -> None:
    lifecycle = active()

    assert (
        lifecycle.retry_disposition_after_401(request_kind=RequestKind.READ)
        is RetryDisposition.REAUTH_THEN_SINGLE_READ_RETRY
    )


def test_write_401_requires_external_effect_reconciliation_before_retry() -> None:
    lifecycle = active()

    assert (
        lifecycle.retry_disposition_after_401(request_kind=RequestKind.WRITE)
        is RetryDisposition.RECONCILE_EXTERNAL_EFFECT_BEFORE_ANY_WRITE_RETRY
    )


def test_restart_never_restores_active_authority_from_audit_snapshot() -> None:
    original = active()
    snapshot = original.audit_snapshot()

    restored = MatchbookSessionLifecycle.from_audit_snapshot(snapshot)

    assert restored.state is SessionState.RESTART_REAUTH_REQUIRED
    assert restored.is_active is False
    assert restored.restart_requires_reauth is True
    with pytest.raises(SessionLifecycleError):
        restored.record_get_session_result(
            generation_id="gen-1", http_status=200, monotonic_ns=ns(20)
        )


def test_restart_requires_new_generation_id_for_fresh_login() -> None:
    restored = MatchbookSessionLifecycle.from_audit_snapshot(active().audit_snapshot())

    with pytest.raises(SessionLifecycleError):
        restored.record_login_200(generation_id="gen-1", monotonic_ns=ns(20))

    restored.record_login_200(generation_id="gen-2", monotonic_ns=ns(21))
    assert restored.state is SessionState.ACTIVE
    assert restored.generation_id == "gen-2"


def test_restart_does_not_compare_new_process_clock_to_old_monotonic_epoch() -> None:
    original = active()
    original.record_get_session_result(
        generation_id="gen-1", http_status=200, monotonic_ns=ns(100)
    )
    restored = MatchbookSessionLifecycle.from_audit_snapshot(original.audit_snapshot())

    assert restored.session_age_hint_seconds(monotonic_ns=1) is None
    restored.record_login_200(generation_id="gen-2", monotonic_ns=2)

    assert restored.state is SessionState.ACTIVE
    assert restored.generation_id == "gen-2"
    assert restored.session_age_hint_seconds(monotonic_ns=3) == 0.000000001


def test_logout_200_terminally_invalidates_generation() -> None:
    lifecycle = active()

    lifecycle.record_logout_200(generation_id="gen-1", monotonic_ns=ns(20))

    assert lifecycle.state is SessionState.EXPIRED
    with pytest.raises(SessionLifecycleError):
        lifecycle.record_get_session_result(
            generation_id="gen-1", http_status=200, monotonic_ns=ns(21)
        )


def test_stale_generation_evidence_is_rejected() -> None:
    lifecycle = active()

    with pytest.raises(SessionLifecycleError):
        lifecycle.record_get_session_result(
            generation_id="gen-0", http_status=200, monotonic_ns=ns(20)
        )


def test_fresh_login_rotates_generation_and_old_generation_stays_terminal() -> None:
    lifecycle = active()
    lifecycle.record_login_200(generation_id="gen-2", monotonic_ns=ns(20))

    with pytest.raises(SessionLifecycleError):
        lifecycle.record_get_session_result(
            generation_id="gen-1", http_status=200, monotonic_ns=ns(21)
        )
    assert lifecycle.generation_id == "gen-2"
    assert lifecycle.state is SessionState.ACTIVE


@pytest.mark.parametrize("operation", ["get_session", "network_failure", "logout"])
def test_stale_generation_timestamp_cannot_clock_fault_current_generation(
    operation: str,
) -> None:
    lifecycle = active()
    lifecycle.record_login_200(generation_id="gen-2", monotonic_ns=ns(20))
    before = lifecycle.audit_snapshot()

    with pytest.raises(
        SessionLifecycleError,
        match="stale or unknown session generation evidence",
    ):
        if operation == "get_session":
            lifecycle.record_get_session_result(
                generation_id="gen-1",
                http_status=200,
                monotonic_ns=ns(19),
            )
        elif operation == "network_failure":
            lifecycle.record_network_failure(
                generation_id="gen-1",
                monotonic_ns=ns(19),
            )
        else:
            lifecycle.record_logout_200(
                generation_id="gen-1",
                monotonic_ns=ns(19),
            )

    assert lifecycle.audit_snapshot() == before
    assert lifecycle.generation_id == "gen-2"
    assert lifecycle.state is SessionState.ACTIVE
    assert lifecycle.is_active is True


@pytest.mark.parametrize("operation", ["get_session", "network_failure", "logout"])
@pytest.mark.parametrize("evidence_second", [19, 21])
def test_terminal_generation_evidence_cannot_mutate_clock_or_expired_snapshot(
    operation: str,
    evidence_second: int,
) -> None:
    lifecycle = active()
    lifecycle.record_get_session_result(
        generation_id="gen-1", http_status=401, monotonic_ns=ns(20)
    )
    before = lifecycle.audit_snapshot()

    with pytest.raises(
        SessionLifecycleError,
        match="terminal session generation evidence cannot mutate lifecycle",
    ):
        if operation == "get_session":
            lifecycle.record_get_session_result(
                generation_id="gen-1",
                http_status=200,
                monotonic_ns=ns(evidence_second),
            )
        elif operation == "network_failure":
            lifecycle.record_network_failure(
                generation_id="gen-1",
                monotonic_ns=ns(evidence_second),
            )
        else:
            lifecycle.record_logout_200(
                generation_id="gen-1",
                monotonic_ns=ns(evidence_second),
            )

    assert lifecycle.audit_snapshot() == before
    assert lifecycle.state is SessionState.EXPIRED
    assert lifecycle.is_active is False
    assert lifecycle.restart_requires_reauth is False


def test_approximate_six_hour_lifetime_is_only_a_refresh_hint() -> None:
    lifecycle = MatchbookSessionLifecycle()
    lifecycle.record_login_200(generation_id="gen-1", monotonic_ns=0)

    assert lifecycle.approximate_refresh_due(
        monotonic_ns=ns(MATCHBOOK_APPROX_SESSION_LIFETIME_SECONDS - 1)
    ) is False
    assert lifecycle.approximate_refresh_due(
        monotonic_ns=ns(MATCHBOOK_APPROX_SESSION_LIFETIME_SECONDS)
    ) is True
    assert lifecycle.state is SessionState.ACTIVE


def test_get_session_200_does_not_reset_original_login_age_horizon() -> None:
    lifecycle = MatchbookSessionLifecycle()
    lifecycle.record_login_200(generation_id="gen-1", monotonic_ns=0)
    lifecycle.record_get_session_result(
        generation_id="gen-1",
        http_status=200,
        monotonic_ns=ns(MATCHBOOK_APPROX_SESSION_LIFETIME_SECONDS - 10),
    )

    assert lifecycle.approximate_refresh_due(
        monotonic_ns=ns(MATCHBOOK_APPROX_SESSION_LIFETIME_SECONDS)
    ) is True


def test_monotonic_rollback_latches_permanent_fail_closed_state() -> None:
    lifecycle = active()
    lifecycle.record_get_session_result(
        generation_id="gen-1", http_status=200, monotonic_ns=ns(20)
    )

    with pytest.raises(SessionClockRollbackError):
        lifecycle.record_network_failure(
            generation_id="gen-1", monotonic_ns=ns(19)
        )
    assert lifecycle.state is SessionState.CLOCK_FAULT
    assert lifecycle.is_active is False

    with pytest.raises(SessionClockRollbackError):
        lifecycle.record_login_200(generation_id="gen-2", monotonic_ns=ns(21))


@pytest.mark.parametrize(
    "bad_generation", ["", " gen", "gen ", "gen 1", "\tgen", "x" * 129]
)
def test_generation_identity_is_strict(bad_generation: str) -> None:
    lifecycle = MatchbookSessionLifecycle()

    with pytest.raises(ValueError):
        lifecycle.record_login_200(generation_id=bad_generation, monotonic_ns=0)


@pytest.mark.parametrize("bad_status", [99, 600, True, 200.0])
def test_http_status_validation_is_strict(bad_status: object) -> None:
    lifecycle = active()

    with pytest.raises((TypeError, ValueError)):
        lifecycle.record_get_session_result(
            generation_id="gen-1",
            http_status=bad_status,  # type: ignore[arg-type]
            monotonic_ns=ns(20),
        )


def test_cold_snapshot_round_trip_stays_cold() -> None:
    cold = MatchbookSessionLifecycle()
    restored = MatchbookSessionLifecycle.from_audit_snapshot(cold.audit_snapshot())

    assert restored.state is SessionState.COLD
    assert restored.restart_requires_reauth is False


def test_read_generation_ticket_authorizes_one_current_positive_commit() -> None:
    lifecycle = active()
    ticket = lifecycle.capture_read_generation(monotonic_ns=ns(20))

    generation = lifecycle.authorize_read_response_commit(
        ticket,
        monotonic_ns=ns(21),
    )

    assert generation == "gen-1"
    assert lifecycle.state is SessionState.ACTIVE
    with pytest.raises(SessionLifecycleError, match="no longer valid"):
        lifecycle.authorize_read_response_commit(ticket, monotonic_ns=ns(22))


def test_copied_or_caller_constructed_read_ticket_cannot_mint_commit_authority() -> None:
    lifecycle = active()
    issued = lifecycle.capture_read_generation(monotonic_ns=ns(20))
    copied = replace(issued)
    caller_built = SessionReadGenerationTicket(
        ticket_id=issued.ticket_id + 1,
        generation_id="gen-1",
        issued_monotonic_ns=ns(20),
    )

    with pytest.raises(SessionLifecycleError, match="not issued here"):
        lifecycle.authorize_read_response_commit(copied, monotonic_ns=ns(21))
    with pytest.raises(SessionLifecycleError, match="not issued here"):
        lifecycle.authorize_read_response_commit(caller_built, monotonic_ns=ns(21))

    assert lifecycle.authorize_read_response_commit(
        issued,
        monotonic_ns=ns(21),
    ) == "gen-1"


def test_late_predecessor_read_response_cannot_mutate_successor_generation_clock() -> None:
    lifecycle = active()
    ticket = lifecycle.capture_read_generation(monotonic_ns=ns(20))
    lifecycle.record_login_200(generation_id="gen-2", monotonic_ns=ns(30))
    before = lifecycle.audit_snapshot()

    with pytest.raises(SessionLifecycleError, match="no longer valid"):
        lifecycle.authorize_read_response_commit(ticket, monotonic_ns=ns(21))

    assert lifecycle.audit_snapshot() == before
    assert lifecycle.generation_id == "gen-2"
    assert lifecycle.state is SessionState.ACTIVE


@pytest.mark.parametrize("invalidator", ["401", "ambiguous", "network", "logout"])
def test_auth_uncertainty_or_terminal_transition_invalidates_inflight_read_ticket(
    invalidator: str,
) -> None:
    lifecycle = active()
    ticket = lifecycle.capture_read_generation(monotonic_ns=ns(20))

    if invalidator == "401":
        lifecycle.record_get_session_result(
            generation_id="gen-1", http_status=401, monotonic_ns=ns(21)
        )
    elif invalidator == "ambiguous":
        lifecycle.record_get_session_result(
            generation_id="gen-1", http_status=503, monotonic_ns=ns(21)
        )
    elif invalidator == "network":
        lifecycle.record_network_failure(
            generation_id="gen-1", monotonic_ns=ns(21)
        )
    else:
        lifecycle.record_logout_200(
            generation_id="gen-1", monotonic_ns=ns(21)
        )

    before = lifecycle.audit_snapshot()
    with pytest.raises(SessionLifecycleError, match="no longer valid"):
        lifecycle.authorize_read_response_commit(ticket, monotonic_ns=ns(22))
    assert lifecycle.audit_snapshot() == before


def test_restart_cannot_reuse_process_local_read_generation_ticket() -> None:
    original = active()
    ticket = original.capture_read_generation(monotonic_ns=ns(20))
    restored = MatchbookSessionLifecycle.from_audit_snapshot(original.audit_snapshot())
    restored.record_login_200(generation_id="gen-2", monotonic_ns=ns(1))
    before = restored.audit_snapshot()

    with pytest.raises(SessionLifecycleError, match="not issued here"):
        restored.authorize_read_response_commit(ticket, monotonic_ns=ns(2))

    assert restored.audit_snapshot() == before


def test_read_ticket_capture_requires_active_session_and_respects_clock_rollback() -> None:
    lifecycle = MatchbookSessionLifecycle()
    with pytest.raises(SessionLifecycleError, match="active session generation"):
        lifecycle.capture_read_generation(monotonic_ns=0)

    lifecycle.record_login_200(generation_id="gen-1", monotonic_ns=ns(10))
    lifecycle.capture_read_generation(monotonic_ns=ns(20))
    with pytest.raises(SessionClockRollbackError):
        lifecycle.capture_read_generation(monotonic_ns=ns(19))

    assert lifecycle.state is SessionState.CLOCK_FAULT
    assert lifecycle.is_active is False