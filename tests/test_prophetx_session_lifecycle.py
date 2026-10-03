from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

import pytest

from autosport.prophetx_session_lifecycle import (
    CONSERVATIVE_SESSION_SLOT_HOLD,
    ProphetXLoginAdmissionAction,
    ProphetXLoginFailureClass,
    ProphetXRenewalFailureClass,
    ProphetXSessionLifecycle,
    ProphetXSessionLifecycleError,
    ProphetXSessionScope,
    ProphetXSessionState,
)


NOW = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)


def _scope(
    *,
    key: str = "key-a",
    revision: str = "rev-1",
    environment: str = "sandbox",
    role: str = "market-maker-primary",
) -> ProphetXSessionScope:
    return ProphetXSessionScope(
        environment=environment,
        access_key_identity_sha256=sha256(key.encode("ascii")).hexdigest(),
        credential_revision=revision,
        integration_role=role,
    )


def _lifecycle(tmp_path, **scope_kwargs) -> ProphetXSessionLifecycle:
    return ProphetXSessionLifecycle(
        tmp_path,
        scope=_scope(**scope_kwargs),
    )


def _active(lifecycle: ProphetXSessionLifecycle, at: datetime = NOW):
    admission = lifecycle.begin_login(
        now=at,
        access_token_available=False,
    )
    assert admission.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert admission.attempt_id is not None
    snapshot = lifecycle.complete_login_success(
        attempt_id=admission.attempt_id,
        now=at,
        access_expires_at=at + timedelta(minutes=10),
    )
    assert snapshot.state is ProphetXSessionState.ACTIVE
    return snapshot


def test_two_consumers_share_one_persisted_login_reservation(tmp_path):
    first = _lifecycle(tmp_path)
    second = _lifecycle(tmp_path)

    one = first.begin_login(now=NOW, access_token_available=False)
    two = second.begin_login(now=NOW, access_token_available=False)

    assert one.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert one.login_authorized is True
    assert two.action is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    assert two.login_authorized is False
    assert two.snapshot is not None
    assert two.snapshot.attempt_id == one.attempt_id


def test_same_coordinator_does_not_duplicate_inflight_login(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    first = lifecycle.begin_login(now=NOW, access_token_available=False)
    second = lifecycle.begin_login(
        now=NOW + timedelta(seconds=1),
        access_token_available=False,
    )

    assert first.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert second.action is ProphetXLoginAdmissionAction.WAIT_FOR_EXISTING_LOGIN
    assert second.snapshot is not None
    assert second.snapshot.attempt_id == first.attempt_id


def test_active_unexpired_session_reuses_local_token(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)

    admission = lifecycle.begin_login(
        now=NOW + timedelta(minutes=1),
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )

    assert admission.action is ProphetXLoginAdmissionAction.REUSE_ACTIVE
    assert admission.snapshot == active
    assert admission.login_authorized is False


def test_restart_without_local_token_waits_for_natural_expiry(tmp_path):
    original = _lifecycle(tmp_path)
    active = _active(original)
    restarted = _lifecycle(tmp_path)

    for minute in (1, 5, 9):
        admission = restarted.begin_login(
            now=NOW + timedelta(minutes=minute),
            access_token_available=False,
        )
        assert (
            admission.action
            is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
        )
        assert admission.retry_at == active.slot_hold_until
        assert admission.login_authorized is False

    successor = restarted.begin_login(
        now=active.access_expires_at + timedelta(seconds=1),
        access_token_available=False,
    )
    assert successor.action is ProphetXLoginAdmissionAction.CREATE_LOGIN


def test_session_pool_exhaustion_is_distinct_and_not_tight_retry(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    failed = lifecycle.complete_login_failure(
        attempt_id=admission.attempt_id,
        now=NOW,
        failure=ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED,
    )

    assert failed.state is ProphetXSessionState.SESSION_POOL_EXHAUSTED
    assert failed.slot_hold_until == NOW + CONSERVATIVE_SESSION_SLOT_HOLD

    retry = lifecycle.begin_login(
        now=NOW + timedelta(minutes=1),
        access_token_available=False,
    )
    assert (
        retry.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert retry.login_authorized is False


def test_credential_revocation_never_claims_provider_slot_freed(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    revoked = lifecycle.record_credential_revoked(
        now=NOW + timedelta(minutes=2)
    )

    assert revoked.state is ProphetXSessionState.CREDENTIAL_REJECTED
    assert revoked.slot_hold_until == active.slot_hold_until

    blocked = lifecycle.begin_login(
        now=NOW + timedelta(minutes=3),
        access_token_available=False,
    )
    assert blocked.action is ProphetXLoginAdmissionAction.CREDENTIAL_REJECTED

    still_blocked = lifecycle.begin_login(
        now=NOW + timedelta(hours=1),
        access_token_available=False,
    )
    assert still_blocked.action is ProphetXLoginAdmissionAction.CREDENTIAL_REJECTED


def test_separate_access_keys_have_independent_session_pools(tmp_path):
    key_a = _lifecycle(tmp_path, key="key-a")
    key_b = _lifecycle(tmp_path, key="key-b")

    a = key_a.begin_login(now=NOW, access_token_available=False)
    b = key_b.begin_login(now=NOW, access_token_available=False)

    assert a.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert b.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert key_a.state_path != key_b.state_path


def test_same_access_key_across_roles_is_explicit_conflict(tmp_path):
    first = _lifecycle(tmp_path, role="worker-a")
    second = _lifecycle(tmp_path, role="worker-b")
    assert (
        first.begin_login(now=NOW, access_token_available=False).action
        is ProphetXLoginAdmissionAction.CREATE_LOGIN
    )

    conflict = second.begin_login(
        now=NOW + timedelta(seconds=1),
        access_token_available=False,
    )
    assert (
        conflict.action
        is ProphetXLoginAdmissionAction.SHARED_ACCESS_KEY_CONFLICT
    )
    assert conflict.login_authorized is False


def test_near_expiry_requires_renewal_without_login_fallback(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)

    admission = lifecycle.begin_login(
        now=active.access_expires_at - timedelta(minutes=1),
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )

    assert (
        admission.action
        is ProphetXLoginAdmissionAction.RENEWAL_REQUIRED
    )
    assert admission.snapshot is not None
    assert admission.snapshot.state is ProphetXSessionState.RENEWAL_DUE
    assert admission.login_authorized is False


def test_expired_active_session_permits_one_successor_login(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)

    successor = lifecycle.begin_login(
        now=active.access_expires_at + timedelta(seconds=1),
        access_token_available=False,
    )
    assert successor.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert successor.snapshot.generation == active.generation + 1


def test_ambiguous_provider_result_preserves_conservative_slot_horizon(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    failed = lifecycle.complete_login_failure(
        attempt_id=admission.attempt_id,
        now=NOW + timedelta(seconds=2),
        failure=ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT,
    )

    assert failed.state is ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    assert failed.slot_hold_until == NOW + timedelta(seconds=2) + CONSERVATIVE_SESSION_SLOT_HOLD

    retry = lifecycle.begin_login(
        now=NOW + timedelta(minutes=10),
        access_token_available=False,
    )
    assert (
        retry.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )


def test_pre_session_failure_uses_bounded_deterministic_backoff(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    failed = lifecycle.complete_login_failure(
        attempt_id=admission.attempt_id,
        now=NOW,
        failure=ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE,
    )

    assert failed.state is ProphetXSessionState.AUTH_RETRYABLE_FAILURE
    assert failed.retry_not_before is not None
    assert NOW < failed.retry_not_before <= NOW + timedelta(minutes=5)

    early = lifecycle.begin_login(
        now=NOW + timedelta(seconds=1),
        access_token_available=False,
    )
    assert early.action is ProphetXLoginAdmissionAction.RETRY_LATER

    ready = lifecycle.begin_login(
        now=failed.retry_not_before,
        access_token_available=False,
    )
    assert ready.action is ProphetXLoginAdmissionAction.CREATE_LOGIN


def test_provider_unavailable_is_not_bad_credentials(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    failed = lifecycle.complete_login_failure(
        attempt_id=admission.attempt_id,
        now=NOW,
        failure=ProphetXLoginFailureClass.PROVIDER_UNAVAILABLE_PRE_SESSION,
    )

    assert failed.state is ProphetXSessionState.PROVIDER_UNAVAILABLE
    blocked = lifecycle.begin_login(
        now=NOW + timedelta(seconds=1),
        access_token_available=False,
    )
    assert blocked.action is ProphetXLoginAdmissionAction.RETRY_LATER


def test_credential_rotation_cannot_erase_old_slot_horizon(tmp_path):
    old = _lifecycle(tmp_path, revision="rev-old")
    active = _active(old)

    rotated = _lifecycle(tmp_path, revision="rev-new")
    blocked = rotated.begin_login(
        now=NOW + timedelta(minutes=5),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == active.slot_hold_until

    allowed = rotated.begin_login(
        now=active.slot_hold_until + timedelta(seconds=1),
        access_token_available=False,
    )
    assert allowed.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert allowed.snapshot.credential_revision == "rev-new"


def test_environment_is_part_of_provider_pool_identity(tmp_path):
    sandbox = _lifecycle(tmp_path, environment="sandbox")
    production = _lifecycle(tmp_path, environment="production")

    assert sandbox.state_path != production.state_path
    assert (
        sandbox.begin_login(now=NOW, access_token_available=False).action
        is ProphetXLoginAdmissionAction.CREATE_LOGIN
    )
    assert (
        production.begin_login(now=NOW, access_token_available=False).action
        is ProphetXLoginAdmissionAction.CREATE_LOGIN
    )


def test_crash_after_login_reservation_does_not_restart_login_storm(tmp_path):
    crashed = _lifecycle(tmp_path)
    first = crashed.begin_login(now=NOW, access_token_available=False)
    assert first.action is ProphetXLoginAdmissionAction.CREATE_LOGIN

    for seconds in (1, 10, 60, 300):
        restarted = _lifecycle(tmp_path)
        admission = restarted.begin_login(
            now=NOW + timedelta(seconds=seconds),
            access_token_available=False,
        )
        assert (
            admission.action
            is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
        )
        assert admission.attempt_id is None
        assert admission.login_authorized is False


def test_persisted_state_is_secret_free(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    _active(lifecycle)

    raw = lifecycle.state_path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert payload["access_key_identity_sha256"] == lifecycle.scope.access_key_identity_sha256
    assert "secret_key" not in raw
    assert "refresh_token" not in raw
    assert "access_token" not in raw
    assert "Bearer " not in raw


def test_credential_rejection_blocks_same_revision_but_rotation_can_recover(tmp_path):
    rejected = _lifecycle(tmp_path, revision="rev-1")
    admission = rejected.begin_login(now=NOW, access_token_available=False)
    state = rejected.complete_login_failure(
        attempt_id=admission.attempt_id,
        now=NOW,
        failure=ProphetXLoginFailureClass.CREDENTIAL_REJECTED,
    )
    assert state.state is ProphetXSessionState.CREDENTIAL_REJECTED

    assert (
        rejected.begin_login(
            now=NOW + timedelta(minutes=1),
            access_token_available=False,
        ).action
        is ProphetXLoginAdmissionAction.CREDENTIAL_REJECTED
    )

    rotated = _lifecycle(tmp_path, revision="rev-2")
    assert (
        rotated.begin_login(
            now=NOW + timedelta(minutes=1),
            access_token_available=False,
        ).action
        is ProphetXLoginAdmissionAction.CREATE_LOGIN
    )


def test_stale_or_foreign_attempt_cannot_complete(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    lifecycle.begin_login(now=NOW, access_token_available=False)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="not issued by this coordinator",
    ):
        lifecycle.complete_login_success(
            attempt_id="a" * 64,
            now=NOW,
            access_expires_at=NOW + timedelta(minutes=10),
        )


def test_corrupt_state_fails_closed_instead_of_minting_new_login(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    lifecycle.begin_login(now=NOW, access_token_available=False)
    lifecycle.state_path.write_text('{"state":"active"}', encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="unexpected schema",
    ):
        lifecycle.begin_login(
            now=NOW + timedelta(seconds=1),
            access_token_available=False,
        )


def test_time_and_token_availability_inputs_require_exact_types(tmp_path):
    lifecycle = _lifecycle(tmp_path)

    with pytest.raises(ProphetXSessionLifecycleError, match="timezone-aware"):
        lifecycle.begin_login(
            now=NOW.replace(tzinfo=None),
            access_token_available=False,
        )

    with pytest.raises(ProphetXSessionLifecycleError, match="exact bool"):
        lifecycle.begin_login(
            now=NOW,
            access_token_available=1,
        )


def test_raw_access_key_cannot_be_used_as_persisted_identity():
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="64-character SHA-256",
    ):
        ProphetXSessionScope(
            environment="sandbox",
            access_key_identity_sha256="raw-access-key",
            credential_revision="rev-1",
            integration_role="worker",
        )


def test_admission_never_claims_real_money_authority(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)

    assert admission.real_money_execution is False


def test_provider_response_owns_access_expiry_not_local_lifetime_guess(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)

    exact_expiry = NOW + timedelta(minutes=7)
    active = lifecycle.complete_login_success(
        attempt_id=admission.attempt_id,
        now=NOW,
        access_expires_at=exact_expiry,
    )

    assert active.access_expires_at == exact_expiry
    assert active.slot_hold_until == exact_expiry
    assert (
        lifecycle.begin_login(
            now=exact_expiry + timedelta(seconds=1),
            access_token_available=False,
        ).action
        is ProphetXLoginAdmissionAction.CREATE_LOGIN
    )


def test_provider_expiry_must_be_future_of_login_completion(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="must be later than login completion",
    ):
        lifecycle.complete_login_success(
            attempt_id=admission.attempt_id,
            now=NOW,
            access_expires_at=NOW,
        )


def test_active_state_rejects_stale_login_attempt_evidence(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    _active(lifecycle)
    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["attempt_id"] = "a" * 64
    payload["attempt_started_at"] = NOW.isoformat()
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="attempt evidence is only valid",
    ):
        lifecycle.read_snapshot()


def test_active_state_rejects_expiry_at_or_before_transition(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    _active(lifecycle)
    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["access_expires_at"] = payload["last_transition_at"]
    payload["slot_hold_until"] = payload["last_transition_at"]
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="access expiry must follow",
    ):
        lifecycle.read_snapshot()


def test_provider_slot_wait_requires_explicit_hold_horizon(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    lifecycle.complete_login_failure(
        attempt_id=admission.attempt_id,
        now=NOW,
        failure=ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED,
    )
    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["slot_hold_until"] = None
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="requires slot_hold_until",
    ):
        lifecycle.read_snapshot()


def test_renewal_is_single_flight_and_never_authorizes_login_fallback(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)

    due = lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    assert due.action is ProphetXLoginAdmissionAction.RENEWAL_REQUIRED

    started = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    assert started.action is ProphetXLoginAdmissionAction.START_RENEWAL
    assert started.attempt_id is not None
    assert started.login_authorized is False

    same_process = lifecycle.begin_renewal(
        now=due_at + timedelta(seconds=1),
        refresh_token_lineage_id=active.session_lineage_id,
    )
    assert (
        same_process.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_EXISTING_RENEWAL
    )

    other_process = _lifecycle(tmp_path)
    blocked = other_process.begin_login(
        now=due_at + timedelta(seconds=1),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.login_authorized is False


def test_refresh_success_without_slot_contract_enters_conservative_wait(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    started = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )

    completed_at = due_at + timedelta(seconds=1)
    renewed_expiry = due_at + timedelta(minutes=10)
    refreshed = lifecycle.complete_renewal_success(
        attempt_id=started.attempt_id,
        now=completed_at,
        access_expires_at=renewed_expiry,
    )

    assert refreshed.state is ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    assert refreshed.session_lineage_id is None
    assert refreshed.access_expires_at == renewed_expiry
    assert refreshed.slot_hold_until == completed_at + CONSERVATIVE_SESSION_SLOT_HOLD
    assert refreshed.transient_failures == 0
    assert refreshed.last_renewal_failure_class is None

    blocked = lifecycle.begin_login(
        now=completed_at + timedelta(minutes=1),
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == refreshed.slot_hold_until

def test_retryable_renewal_failure_retries_refresh_not_login(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    started = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    failed = lifecycle.complete_renewal_failure(
        attempt_id=started.attempt_id,
        now=due_at + timedelta(seconds=1),
        failure=ProphetXRenewalFailureClass.RETRYABLE,
    )

    assert failed.state is ProphetXSessionState.RENEWAL_DUE
    assert failed.retry_not_before is not None

    login_admission = lifecycle.begin_login(
        now=due_at + timedelta(seconds=2),
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    assert login_admission.action is ProphetXLoginAdmissionAction.RENEWAL_REQUIRED
    assert login_admission.retry_at == failed.retry_not_before
    assert login_admission.login_authorized is False

    renewal_admission = lifecycle.begin_renewal(
        now=due_at + timedelta(seconds=2),
        refresh_token_lineage_id=active.session_lineage_id,
    )
    assert renewal_admission.action is ProphetXLoginAdmissionAction.RETRY_LATER


def test_ambiguous_renewal_result_blocks_replacement_login_for_conservative_hold(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    started = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    failed_at = due_at + timedelta(seconds=1)
    failed = lifecycle.complete_renewal_failure(
        attempt_id=started.attempt_id,
        now=failed_at,
        failure=ProphetXRenewalFailureClass.AMBIGUOUS_PROVIDER_RESULT,
    )

    assert failed.state is ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    assert (
        failed.slot_hold_until
        == failed_at + CONSERVATIVE_SESSION_SLOT_HOLD
    )
    blocked = lifecycle.begin_login(
        now=active.access_expires_at + timedelta(minutes=1),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.login_authorized is False


def test_crash_during_renewal_becomes_bounded_ambiguous_session_hold(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )

    restarted = _lifecycle(tmp_path)
    blocked = restarted.begin_login(
        now=due_at + timedelta(minutes=1),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == due_at + CONSERVATIVE_SESSION_SLOT_HOLD

    recovered = restarted.begin_login(
        now=due_at + CONSERVATIVE_SESSION_SLOT_HOLD + timedelta(seconds=1),
        access_token_available=False,
    )
    assert recovered.action is ProphetXLoginAdmissionAction.CREATE_LOGIN


def test_credential_rejected_during_renewal_stays_fail_closed(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    started = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    rejected = lifecycle.complete_renewal_failure(
        attempt_id=started.attempt_id,
        now=due_at + timedelta(seconds=1),
        failure=ProphetXRenewalFailureClass.CREDENTIAL_REJECTED,
    )

    assert rejected.state is ProphetXSessionState.CREDENTIAL_REJECTED
    assert rejected.slot_hold_until == active.slot_hold_until
    assert (
        lifecycle.begin_login(
            now=due_at + timedelta(seconds=2),
            access_token_available=False,
        ).action
        is ProphetXLoginAdmissionAction.CREDENTIAL_REJECTED
    )


def test_unambiguous_renewal_failure_after_original_expiry_allows_fresh_login(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    started = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    failed = lifecycle.complete_renewal_failure(
        attempt_id=started.attempt_id,
        now=active.access_expires_at + timedelta(seconds=1),
        failure=ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE,
    )

    assert failed.state is ProphetXSessionState.EXPIRED
    fresh = lifecycle.begin_login(
        now=active.access_expires_at + timedelta(seconds=2),
        access_token_available=False,
    )
    assert fresh.action is ProphetXLoginAdmissionAction.CREATE_LOGIN


def test_revocation_during_renewal_preserves_ambiguous_refresh_slot_horizon(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )

    revoked_at = due_at + timedelta(seconds=1)
    revoked = lifecycle.record_credential_revoked(now=revoked_at)

    assert revoked.state is ProphetXSessionState.CREDENTIAL_REJECTED
    assert (
        revoked.slot_hold_until
        == due_at + CONSERVATIVE_SESSION_SLOT_HOLD
    )
    assert revoked.slot_hold_until > active.access_expires_at


def test_renewal_persisted_state_remains_secret_free(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )

    raw = lifecycle.state_path.read_text(encoding="utf-8")
    assert '"state":"renewing"' in raw
    assert "secret_key" not in raw
    assert "refresh_token" not in raw
    assert "access_token" not in raw
    assert "Bearer " not in raw


def test_renewal_before_lead_window_is_rejected(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="renewal is not due yet",
    ):
        lifecycle.begin_renewal(
            now=active.access_expires_at - timedelta(minutes=3),
            refresh_token_lineage_id=active.session_lineage_id,
        )


def test_refresh_success_clears_transient_backoff_without_minting_active(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )

    first_attempt = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    first_failed_at = due_at + timedelta(seconds=1)
    first_failure = lifecycle.complete_renewal_failure(
        attempt_id=first_attempt.attempt_id,
        now=first_failed_at,
        failure=ProphetXRenewalFailureClass.RETRYABLE,
    )
    assert first_failure.transient_failures == 1

    retry_at = first_failure.retry_not_before
    second_attempt = lifecycle.begin_renewal(
        now=retry_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    completed_at = retry_at + timedelta(seconds=1)
    refreshed = lifecycle.complete_renewal_success(
        attempt_id=second_attempt.attempt_id,
        now=completed_at,
        access_expires_at=completed_at + timedelta(minutes=30),
    )

    assert refreshed.state is ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    assert refreshed.access_expires_at == completed_at + timedelta(minutes=30)
    assert refreshed.transient_failures == 0
    assert refreshed.last_renewal_failure_class is None
    assert refreshed.slot_hold_until == completed_at + timedelta(minutes=30)

    restarted = _lifecycle(tmp_path)
    blocked = restarted.begin_login(
        now=completed_at + timedelta(minutes=21),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == refreshed.slot_hold_until

    admitted = restarted.begin_login(
        now=refreshed.slot_hold_until,
        access_token_available=False,
    )
    assert admitted.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert admitted.snapshot.transient_failures == 0

def test_available_access_token_requires_exact_current_session_lineage(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="requires session lineage evidence",
    ):
        lifecycle.begin_login(
            now=NOW + timedelta(minutes=1),
            access_token_available=True,
        )

    foreign_lineage = sha256(b"foreign-session").hexdigest()
    blocked = lifecycle.begin_login(
        now=NOW + timedelta(minutes=1),
        access_token_available=True,
        access_token_lineage_id=foreign_lineage,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == active.slot_hold_until
    assert blocked.login_authorized is False


def test_unavailable_access_token_rejects_lineage_claim(tmp_path):
    lifecycle = _lifecycle(tmp_path)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="cannot carry session lineage evidence",
    ):
        lifecycle.begin_login(
            now=NOW,
            access_token_available=False,
            access_token_lineage_id=sha256(b"stale").hexdigest(),
        )


@pytest.mark.parametrize(
    "state",
    [
        ProphetXSessionState.EXPIRED,
        ProphetXSessionState.NO_SESSION,
    ],
)
def test_empty_terminal_state_cannot_hide_provider_slot_hold(tmp_path, state):
    lifecycle = _lifecycle(tmp_path)
    _active(lifecycle)
    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["state"] = state.value
    payload["session_lineage_id"] = None
    payload["access_expires_at"] = None
    payload["retry_not_before"] = None
    # Keep the previously active provider-slot horizon to model contradictory
    # persisted authority. A terminal-state label must not bypass that evidence.
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="cannot carry session, slot, or retry evidence",
    ):
        lifecycle.begin_login(
            now=NOW + timedelta(minutes=1),
            access_token_available=False,
        )


def test_completion_clock_rollback_cannot_shorten_ambiguous_slot_hold(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="cannot precede persisted lifecycle time",
    ):
        lifecycle.complete_login_failure(
            attempt_id=admission.attempt_id,
            now=NOW - timedelta(seconds=1),
            failure=ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT,
        )

    restarted = _lifecycle(tmp_path)
    blocked = restarted.begin_login(
        now=NOW + timedelta(seconds=1),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == NOW + CONSERVATIVE_SESSION_SLOT_HOLD


def test_state_changing_calls_reject_clock_rollback(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="cannot precede persisted lifecycle time",
    ):
        lifecycle.record_credential_revoked(
            now=active.last_transition_at - timedelta(microseconds=1)
        )


def test_inflight_state_rejects_divergent_attempt_transition_time(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    lifecycle.begin_login(now=NOW, access_token_available=False)
    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["attempt_started_at"] = (
        NOW - timedelta(seconds=1)
    ).isoformat()
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="attempt start must equal transition time",
    ):
        lifecycle.read_snapshot()


def test_renewal_requires_exact_refresh_token_lineage(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="refresh token requires session lineage evidence",
    ):
        lifecycle.begin_renewal(now=due_at)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="refresh token lineage does not match active session",
    ):
        lifecycle.begin_renewal(
            now=due_at,
            refresh_token_lineage_id=sha256(b"foreign-refresh").hexdigest(),
        )

    snapshot = lifecycle.read_snapshot()
    assert snapshot.state is ProphetXSessionState.RENEWAL_DUE
    assert snapshot.session_lineage_id == active.session_lineage_id


def test_same_process_stale_renewal_recovers_after_uncertainty_deadline(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    started = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    assert started.action is ProphetXLoginAdmissionAction.START_RENEWAL

    uncertainty_deadline = due_at + CONSERVATIVE_SESSION_SLOT_HOLD
    recovered = lifecycle.begin_login(
        now=uncertainty_deadline + timedelta(seconds=1),
        access_token_available=False,
    )

    assert recovered.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert recovered.attempt_id != started.attempt_id
    assert recovered.login_authorized is True


def test_available_token_without_durable_state_fails_closed(tmp_path):
    lifecycle = _lifecycle(tmp_path)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="cannot be reconciled without durable session state",
    ):
        lifecycle.begin_login(
            now=NOW,
            access_token_available=True,
            access_token_lineage_id=sha256(b"orphan-token").hexdigest(),
        )

    assert lifecycle.read_snapshot() is None


def test_same_process_stale_login_reservation_recovers_after_hold(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    first = lifecycle.begin_login(now=NOW, access_token_available=False)
    assert first.action is ProphetXLoginAdmissionAction.CREATE_LOGIN

    recovered = lifecycle.begin_login(
        now=NOW + CONSERVATIVE_SESSION_SLOT_HOLD + timedelta(seconds=1),
        access_token_available=False,
    )
    assert recovered.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert recovered.attempt_id != first.attempt_id

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="login attempt no longer owns current session-pool admission",
    ):
        lifecycle.complete_login_failure(
            attempt_id=first.attempt_id,
            now=NOW + CONSERVATIVE_SESSION_SLOT_HOLD + timedelta(seconds=2),
            failure=ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment", "e" * 4097),
        ("revision", "r" * 4097),
        ("role", "x" * 4097),
    ],
)
def test_scope_text_is_bounded(field, value):
    kwargs = {field: value}

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="exceeds the bounded text contract",
    ):
        _scope(**kwargs)


def test_state_file_size_is_bounded_before_json_parse(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    lifecycle.begin_login(now=NOW, access_token_available=False)
    lifecycle.state_path.write_text(
        "{" + (" " * (64 * 1024)) + "}",
        encoding="utf-8",
    )

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="exceeds the bounded file-size contract",
    ):
        lifecycle.read_snapshot()


@pytest.mark.parametrize(
    "state",
    [
        ProphetXSessionState.SESSION_POOL_EXHAUSTED,
        ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
    ],
)
def test_provider_slot_wait_rejects_nonfuture_hold(tmp_path, state):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    lifecycle.complete_login_failure(
        attempt_id=admission.attempt_id,
        now=NOW + timedelta(seconds=1),
        failure=ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT,
    )
    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["state"] = state.value
    payload["slot_hold_until"] = payload["last_transition_at"]
    if state is ProphetXSessionState.SESSION_POOL_EXHAUSTED:
        payload["last_failure_class"] = (
            ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED.value
        )
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="requires a future hold horizon",
    ):
        lifecycle.begin_login(
            now=NOW + timedelta(minutes=1),
            access_token_available=False,
        )


@pytest.mark.parametrize(
    "state",
    [
        ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
        ProphetXSessionState.PROVIDER_UNAVAILABLE,
    ],
)
def test_retryable_state_rejects_nonfuture_retry_horizon(tmp_path, state):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    failure = (
        ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE
        if state is ProphetXSessionState.AUTH_RETRYABLE_FAILURE
        else ProphetXLoginFailureClass.PROVIDER_UNAVAILABLE_PRE_SESSION
    )
    lifecycle.complete_login_failure(
        attempt_id=admission.attempt_id,
        now=NOW + timedelta(seconds=1),
        failure=failure,
    )
    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["retry_not_before"] = payload["last_transition_at"]
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="requires a future retry horizon",
    ):
        lifecycle.begin_login(
            now=NOW + timedelta(minutes=1),
            access_token_available=False,
        )


def test_wait_state_rejects_access_expiry_beyond_slot_hold():
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="slot hold cannot precede observed access expiry",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
            generation=1,
            credential_revision="cred-v1",
            integration_role="market-maker",
            last_transition_at=NOW,
            access_expires_at=NOW + timedelta(minutes=30),
            slot_hold_until=NOW + timedelta(minutes=20),
        )


def test_session_pool_exhaustion_cannot_claim_access_expiry():
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="exhaustion cannot carry access-expiry evidence",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.SESSION_POOL_EXHAUSTED,
            generation=1,
            credential_revision="cred-v1",
            integration_role="market-maker",
            last_transition_at=NOW,
            access_expires_at=NOW + timedelta(minutes=10),
            slot_hold_until=NOW + timedelta(minutes=20),
        )


def test_raw_provider_slot_bool_cannot_authorize_refresh_promotion(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    started = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        lifecycle.complete_renewal_success(
            attempt_id=started.attempt_id,
            now=due_at + timedelta(seconds=1),
            access_expires_at=due_at + timedelta(minutes=10),
            provider_session_slot_preservation_proven=True,
        )

    still_renewing = lifecycle.read_snapshot()
    assert still_renewing.state is ProphetXSessionState.RENEWING
    assert still_renewing.attempt_id == started.attempt_id

def test_admission_cannot_forge_login_authority_without_reservation():
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="requires its exact durable reservation",
    ):
        ProphetXLoginAdmission(
            action=ProphetXLoginAdmissionAction.CREATE_LOGIN,
            snapshot=None,
            attempt_id="a" * 64,
            retry_at=NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
        )


def test_admission_rejects_attempt_id_on_non_effect_action(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="only effect-start admissions may expose an attempt id",
    ):
        ProphetXLoginAdmission(
            action=ProphetXLoginAdmissionAction.REUSE_ACTIVE,
            snapshot=active,
            attempt_id="a" * 64,
        )


def test_state_file_duplicate_json_key_fails_closed_before_schema_use(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    lifecycle.begin_login(now=NOW, access_token_available=False)
    raw = lifecycle.state_path.read_text(encoding="utf-8").rstrip()
    duplicate = raw[:-1] + ',"provider":"prophetx"}\n'
    lifecycle.state_path.write_text(duplicate, encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="duplicate JSON key: provider",
    ):
        lifecycle.read_snapshot()

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="duplicate JSON key: provider",
    ):
        lifecycle.begin_login(
            now=NOW + timedelta(seconds=1),
            access_token_available=False,
        )


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_state_file_nonstandard_json_constant_fails_closed(tmp_path, constant):
    lifecycle = _lifecycle(tmp_path)
    lifecycle.begin_login(now=NOW, access_token_available=False)
    raw = lifecycle.state_path.read_text(encoding="utf-8")
    poisoned = raw.replace('"generation":0', f'"generation":{constant}', 1)
    assert poisoned != raw
    lifecycle.state_path.write_text(poisoned, encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="non-standard JSON constant",
    ):
        lifecycle.read_snapshot()
