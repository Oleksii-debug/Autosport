from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

import pytest

from autosport.prophetx_session_lifecycle import (
    ACCESS_TOKEN_LIFETIME,
    ProphetXLoginAdmissionAction,
    ProphetXLoginFailureClass,
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
    )

    assert admission.action is ProphetXLoginAdmissionAction.REUSE_ACTIVE
    assert admission.snapshot == active
    assert admission.login_authorized is False


def test_restart_without_local_token_waits_for_natural_expiry(tmp_path):
    original = _lifecycle(tmp_path)
    active = _active(original)
    restarted = _lifecycle(tmp_path)

    for minute in (1, 5, 10, 19):
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
        now=NOW + ACCESS_TOKEN_LIFETIME + timedelta(seconds=1),
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
    assert failed.slot_hold_until == NOW + ACCESS_TOKEN_LIFETIME

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


def test_near_expiry_fails_closed_when_partner_refresh_contract_unqualified(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)

    admission = lifecycle.begin_login(
        now=active.access_expires_at - timedelta(minutes=1),
        access_token_available=True,
    )

    assert (
        admission.action
        is ProphetXLoginAdmissionAction.RENEWAL_CONTRACT_UNQUALIFIED
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
    assert failed.slot_hold_until == NOW + timedelta(seconds=2) + ACCESS_TOKEN_LIFETIME

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
