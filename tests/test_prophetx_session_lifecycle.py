from datetime import datetime, timedelta, timezone, tzinfo
from hashlib import sha256
import json
from pathlib import Path

import pytest

import autosport.prophetx_session_lifecycle as prophetx_session_lifecycle
from autosport.prophetx_session_lifecycle import (
    CONSERVATIVE_SESSION_SLOT_HOLD,
    ProphetXLoginAdmission,
    ProphetXLoginAdmissionAction,
    ProphetXLoginFailureClass,
    ProphetXRenewalFailureClass,
    ProphetXSessionLifecycle,
    ProphetXSessionLifecycleError,
    ProphetXSessionScope,
    ProphetXSessionSnapshot,
    ProphetXSessionState,
)
from autosport.workspace_lock import (
    WorkspaceEconomicLockBusyError,
    WorkspaceEconomicLockError,
)


NOW = datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)


class _ChangingOffsetTz(tzinfo):
    def __init__(self) -> None:
        self.calls = 0

    def utcoffset(self, _dt: datetime | None) -> timedelta:
        self.calls += 1
        if self.calls == 1:
            return timedelta(0)
        return timedelta(hours=-12)

    def dst(self, _dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _dt: datetime | None) -> str:
        return "CHANGING"


class _InvalidOffsetTz(tzinfo):
    def utcoffset(self, _dt: datetime | None):
        return "invalid"

    def dst(self, _dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _dt: datetime | None) -> str:
        return "INVALID"


class _ExtremeOffsetTz(tzinfo):
    def utcoffset(self, _dt: datetime | None) -> timedelta:
        return timedelta(hours=23)

    def dst(self, _dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _dt: datetime | None) -> str:
        return "EXTREME"


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


def _dispatch(
    lifecycle: ProphetXSessionLifecycle,
    admission: ProphetXLoginAdmission,
    *,
    at: datetime,
) -> None:
    assert lifecycle.consume_effect_authority(admission, now=at) is True


def _active(lifecycle: ProphetXSessionLifecycle, at: datetime = NOW):
    admission = lifecycle.begin_login(
        now=at,
        access_token_available=False,
    )
    assert admission.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert admission.attempt_id is not None
    _dispatch(lifecycle, admission, at=at)
    snapshot = lifecycle.complete_login_success(
        attempt_id=admission.attempt_id,
        now=at,
        access_expires_at=at + timedelta(minutes=10),
    )
    assert snapshot.state is ProphetXSessionState.ACTIVE
    return snapshot


def test_lifecycle_time_normalization_uses_one_offset_observation(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    zone = _ChangingOffsetTz()
    supplied = datetime(2026, 10, 3, 20, 0, tzinfo=zone)

    admission = lifecycle.begin_login(
        now=supplied,
        access_token_available=False,
    )

    assert zone.calls == 1
    assert admission.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert admission.snapshot is not None
    assert admission.snapshot.last_transition_at == NOW
    assert admission.snapshot.attempt_started_at == NOW
    assert admission.snapshot.slot_hold_started_at == NOW
    assert admission.snapshot.slot_hold_until == NOW + CONSERVATIVE_SESSION_SLOT_HOLD


def test_invalid_timezone_offset_is_bounded_lifecycle_error(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    supplied = datetime(2026, 10, 3, 20, 0, tzinfo=_InvalidOffsetTz())

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="invalid timezone offset",
    ):
        lifecycle.begin_login(
            now=supplied,
            access_token_available=False,
        )

    assert not lifecycle.state_path.exists()


def test_timezone_normalization_overflow_fails_before_state_write(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    supplied = datetime(1, 1, 1, 0, 0, tzinfo=_ExtremeOffsetTz())

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="cannot be normalized to UTC",
    ):
        lifecycle.begin_login(
            now=supplied,
            access_token_available=False,
        )

    assert not lifecycle.state_path.exists()


@pytest.mark.parametrize("alias_level", ["root", "pool"])
def test_scope_directory_alias_is_rejected_before_pool_use(
    tmp_path,
    alias_level,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    scope = _scope()
    root = workspace / ".provider-session-lifecycle"
    try:
        if alias_level == "root":
            root.symlink_to(outside, target_is_directory=True)
        else:
            root.mkdir()
            (root / scope.pool_id).symlink_to(
                outside,
                target_is_directory=True,
            )
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink is unavailable: {exc}")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="session scope directory cannot be redirected",
    ):
        ProphetXSessionLifecycle(workspace, scope=scope)

    assert list(outside.iterdir()) == []


def test_relative_workspace_is_frozen_against_cwd_change(tmp_path, monkeypatch):
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    workspace = first_cwd / "workspace"
    workspace.mkdir(parents=True)
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    lifecycle = ProphetXSessionLifecycle(Path("workspace"), scope=_scope())
    original_state_path = lifecycle.state_path
    first = lifecycle.begin_login(
        now=NOW,
        access_token_available=False,
    )

    assert lifecycle.workspace == workspace.resolve()
    assert lifecycle.workspace.is_absolute()
    assert original_state_path.is_absolute()
    assert first.action is ProphetXLoginAdmissionAction.CREATE_LOGIN

    monkeypatch.chdir(second_cwd)
    second = lifecycle.begin_login(
        now=NOW + timedelta(seconds=1),
        access_token_available=False,
    )

    assert lifecycle.state_path == original_state_path
    assert second.action is ProphetXLoginAdmissionAction.WAIT_FOR_EXISTING_LOGIN
    assert second.snapshot is not None
    assert second.snapshot.attempt_id == first.attempt_id
    assert not (second_cwd / "workspace").exists()


def test_two_consumers_share_one_persisted_login_reservation(tmp_path):
    first = _lifecycle(tmp_path)
    second = _lifecycle(tmp_path)

    one = first.begin_login(now=NOW, access_token_available=False)
    two = second.begin_login(now=NOW, access_token_available=False)

    assert one.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert one.login_authorized is False
    assert first.consume_effect_authority(one, now=NOW) is True
    assert second.consume_effect_authority(one, now=NOW) is False
    assert first.consume_effect_authority(one, now=NOW) is False
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


def test_restart_without_local_token_waits_for_conservative_slot_horizon(tmp_path):
    original = _lifecycle(tmp_path)
    active = _active(original)
    restarted = _lifecycle(tmp_path)

    assert active.access_expires_at == NOW + timedelta(minutes=10)
    assert active.slot_hold_until == NOW + CONSERVATIVE_SESSION_SLOT_HOLD

    for minute in (1, 5, 9, 11, 19):
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
        now=active.slot_hold_until,
        access_token_available=False,
    )
    assert successor.action is ProphetXLoginAdmissionAction.CREATE_LOGIN


def test_session_pool_exhaustion_is_distinct_and_not_tight_retry(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    _dispatch(lifecycle, admission, at=NOW)
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


def test_expired_access_token_cannot_bypass_conservative_slot_hold(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)

    blocked = lifecycle.begin_login(
        now=active.access_expires_at + timedelta(seconds=1),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == active.slot_hold_until

    persisted = lifecycle.read_snapshot()
    assert persisted.state is ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    assert persisted.session_lineage_id is None
    assert persisted.access_expires_at is None
    assert persisted.slot_hold_started_at == active.slot_hold_started_at
    assert persisted.slot_hold_until == active.slot_hold_until

    successor = lifecycle.begin_login(
        now=active.slot_hold_until,
        access_token_available=False,
    )
    assert successor.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert successor.snapshot.generation == persisted.generation + 1




def test_restart_rejects_shortened_residual_wait_slot_hold(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    transition_at = active.access_expires_at + timedelta(seconds=1)

    waiting = lifecycle.begin_login(
        now=transition_at,
        access_token_available=False,
    )
    assert (
        waiting.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert waiting.snapshot is not None
    assert waiting.snapshot.slot_hold_started_at == active.slot_hold_started_at
    assert waiting.snapshot.slot_hold_until == active.slot_hold_until

    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["slot_hold_until"] = (
        transition_at + timedelta(seconds=2)
    ).isoformat()
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    restarted = _lifecycle(tmp_path)
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="provider-slot hold is below its durable conservative floor",
    ):
        restarted.begin_login(
            now=transition_at + timedelta(seconds=3),
            access_token_available=False,
        )


def test_ambiguous_provider_result_preserves_conservative_slot_horizon(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    _dispatch(lifecycle, admission, at=NOW)
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
    _dispatch(lifecycle, admission, at=NOW)
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
    _dispatch(lifecycle, admission, at=NOW)
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


@pytest.mark.parametrize(
    "failure",
    [
        ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE,
        ProphetXLoginFailureClass.PROVIDER_UNAVAILABLE_PRE_SESSION,
    ],
)
def test_credential_rotation_cannot_bypass_transient_auth_backoff(
    tmp_path,
    failure,
):
    original = _lifecycle(tmp_path, revision="rev-1")
    admitted = original.begin_login(now=NOW, access_token_available=False)
    _dispatch(original, admitted, at=NOW)
    degraded = original.complete_login_failure(
        attempt_id=admitted.attempt_id,
        now=NOW + timedelta(seconds=1),
        failure=failure,
    )
    assert degraded.retry_not_before is not None

    rotated = _lifecycle(tmp_path, revision="rev-2")
    blocked = rotated.begin_login(
        now=NOW + timedelta(seconds=2),
        access_token_available=False,
    )
    assert blocked.action is ProphetXLoginAdmissionAction.RETRY_LATER
    assert blocked.retry_at == degraded.retry_not_before
    assert blocked.snapshot == degraded
    assert blocked.login_authorized is False

    recovered = rotated.begin_login(
        now=degraded.retry_not_before,
        access_token_available=False,
    )
    assert recovered.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert recovered.snapshot.credential_revision == "rev-2"


@pytest.mark.parametrize(
    "failure",
    [
        ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE,
        ProphetXLoginFailureClass.PROVIDER_UNAVAILABLE_PRE_SESSION,
    ],
)
def test_revocation_then_rotation_cannot_launder_transient_backoff(
    tmp_path,
    failure,
):
    original = _lifecycle(tmp_path, revision="rev-1")
    admitted = original.begin_login(now=NOW, access_token_available=False)
    _dispatch(original, admitted, at=NOW)
    degraded = original.complete_login_failure(
        attempt_id=admitted.attempt_id,
        now=NOW + timedelta(seconds=1),
        failure=failure,
    )
    revoked = original.record_credential_revoked(
        now=NOW + timedelta(seconds=2)
    )

    assert revoked.state is ProphetXSessionState.CREDENTIAL_REJECTED
    assert revoked.retry_not_before == degraded.retry_not_before

    rotated = _lifecycle(tmp_path, revision="rev-2")
    blocked = rotated.begin_login(
        now=NOW + timedelta(seconds=3),
        access_token_available=False,
    )
    assert blocked.action is ProphetXLoginAdmissionAction.RETRY_LATER
    assert blocked.retry_at == degraded.retry_not_before
    assert blocked.login_authorized is False

    recovered = rotated.begin_login(
        now=degraded.retry_not_before,
        access_token_available=False,
    )
    assert recovered.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert recovered.snapshot.credential_revision == "rev-2"


def test_credential_rejected_retained_retry_horizon_must_be_future():
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="retained retry horizon must be future",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.CREDENTIAL_REJECTED,
            generation=3,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            retry_not_before=NOW,
            last_failure_class=ProphetXLoginFailureClass.CREDENTIAL_REJECTED,
        )


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
    _dispatch(rejected, admission, at=NOW)
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


def test_provider_response_owns_access_expiry_but_not_shorter_slot_horizon(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)

    exact_expiry = NOW + timedelta(minutes=7)
    _dispatch(lifecycle, admission, at=NOW)
    active = lifecycle.complete_login_success(
        attempt_id=admission.attempt_id,
        now=NOW,
        access_expires_at=exact_expiry,
    )

    assert active.access_expires_at == exact_expiry
    assert active.slot_hold_until == NOW + CONSERVATIVE_SESSION_SLOT_HOLD
    blocked = lifecycle.begin_login(
        now=exact_expiry + timedelta(seconds=1),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == active.slot_hold_until


def test_long_provider_expiry_extends_slot_hold_beyond_conservative_floor(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)

    exact_expiry = NOW + timedelta(minutes=30)
    _dispatch(lifecycle, admission, at=NOW)
    active = lifecycle.complete_login_success(
        attempt_id=admission.attempt_id,
        now=NOW,
        access_expires_at=exact_expiry,
    )

    assert active.access_expires_at == exact_expiry
    assert active.slot_hold_until == exact_expiry
    successor = lifecycle.begin_login(
        now=exact_expiry + timedelta(seconds=1),
        access_token_available=False,
    )
    assert successor.action is ProphetXLoginAdmissionAction.CREATE_LOGIN


def test_provider_expiry_must_be_future_of_login_completion(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)

    _dispatch(lifecycle, admission, at=NOW)

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
    payload["slot_hold_started_at"] = (
        NOW - CONSERVATIVE_SESSION_SLOT_HOLD
    ).isoformat()
    payload["slot_hold_until"] = payload["last_transition_at"]
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="access expiry must follow",
    ):
        lifecycle.read_snapshot()


@pytest.mark.parametrize(
    "state,kwargs,match",
    [
        (
            ProphetXSessionState.LOGIN_IN_FLIGHT,
            {
                "attempt_id": "a" * 64,
                "attempt_started_at": NOW,
                "slot_hold_started_at": (
                    NOW - CONSERVATIVE_SESSION_SLOT_HOLD + timedelta(seconds=1)
                ),
                "slot_hold_until": NOW + timedelta(seconds=1),
            },
            "login_in_flight slot hold is below the conservative floor",
        ),
        (
            ProphetXSessionState.ACTIVE,
            {
                "session_lineage_id": "b" * 64,
                "access_expires_at": NOW + timedelta(seconds=1),
                "slot_hold_started_at": (
                    NOW - CONSERVATIVE_SESSION_SLOT_HOLD + timedelta(seconds=1)
                ),
                "slot_hold_until": NOW + timedelta(seconds=1),
            },
            "active slot hold is below the conservative floor",
        ),
        (
            ProphetXSessionState.SESSION_POOL_EXHAUSTED,
            {
                "slot_hold_started_at": (
                    NOW - CONSERVATIVE_SESSION_SLOT_HOLD + timedelta(seconds=1)
                ),
                "slot_hold_until": NOW + timedelta(seconds=1),
                "last_failure_class": (
                    ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED
                ),
            },
            "session-pool hold is below the conservative floor",
        ),
    ],
)
def test_persisted_floor_states_reject_shortened_slot_horizons(
    state,
    kwargs,
    match,
):
    with pytest.raises(ProphetXSessionLifecycleError, match=match):
        ProphetXSessionSnapshot(
            state=state,
            generation=14,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            **kwargs,
        )


@pytest.mark.parametrize(
    "login_failure,renewal_failure",
    [
        (ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT, None),
        (None, ProphetXRenewalFailureClass.AMBIGUOUS_PROVIDER_RESULT),
    ],
)
def test_ambiguous_wait_rejects_shortened_conservative_hold(
    login_failure,
    renewal_failure,
):
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="ambiguous provider result hold is below the conservative floor",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
            generation=15,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            slot_hold_started_at=NOW - CONSERVATIVE_SESSION_SLOT_HOLD + timedelta(seconds=1),
            slot_hold_until=NOW + timedelta(seconds=1),
            last_failure_class=login_failure,
            last_renewal_failure_class=renewal_failure,
        )


def test_refresh_success_wait_rejects_shortened_conservative_hold():
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="wait hold is below the conservative refresh floor",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
            generation=16,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            access_expires_at=NOW + timedelta(minutes=5),
            slot_hold_started_at=NOW - timedelta(minutes=15),
            slot_hold_until=NOW + timedelta(minutes=5),
        )


def test_restart_rejects_shortened_inflight_hold_before_new_login(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    lifecycle.begin_login(now=NOW, access_token_available=False)
    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["slot_hold_started_at"] = (
        NOW - CONSERVATIVE_SESSION_SLOT_HOLD + timedelta(seconds=1)
    ).isoformat()
    payload["slot_hold_until"] = (NOW + timedelta(seconds=1)).isoformat()
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="login_in_flight slot hold is below the conservative floor",
    ):
        _lifecycle(tmp_path).begin_login(
            now=NOW + timedelta(seconds=2),
            access_token_available=False,
        )


def test_provider_slot_wait_requires_explicit_hold_horizon(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    _dispatch(lifecycle, admission, at=NOW)
    lifecycle.complete_login_failure(
        attempt_id=admission.attempt_id,
        now=NOW,
        failure=ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED,
    )
    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["slot_hold_started_at"] = None
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


def test_renewal_lock_contention_waits_without_login_fallback(tmp_path, monkeypatch):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )

    class BusyLock:
        def __init__(self, workspace):
            self.workspace = workspace

        def __enter__(self):
            raise WorkspaceEconomicLockBusyError("owned by another process")

        def __exit__(self, exc_type, exc_value, traceback):
            return None

    monkeypatch.setattr(
        prophetx_session_lifecycle,
        "WorkspaceEconomicLock",
        BusyLock,
    )

    blocked = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )

    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_EXISTING_RENEWAL
    )
    assert blocked.snapshot is None
    assert blocked.attempt_id is None
    assert blocked.retry_at is None
    assert blocked.login_authorized is False


def test_renewal_lock_integrity_failure_stays_fail_closed(tmp_path, monkeypatch):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )

    class BrokenLock:
        def __init__(self, workspace):
            self.workspace = workspace

        def __enter__(self):
            raise WorkspaceEconomicLockError("integrity failure")

        def __exit__(self, exc_type, exc_value, traceback):
            return None

    monkeypatch.setattr(
        prophetx_session_lifecycle,
        "WorkspaceEconomicLock",
        BrokenLock,
    )

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="cannot acquire ProphetX session-pool coordination lock",
    ):
        lifecycle.begin_renewal(
            now=due_at,
            refresh_token_lineage_id=active.session_lineage_id,
        )


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
    _dispatch(lifecycle, started, at=due_at)
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
    _dispatch(lifecycle, started, at=due_at)
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


@pytest.mark.parametrize(
    "failure",
    [
        ProphetXRenewalFailureClass.RETRYABLE,
        ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE,
    ],
)
def test_pre_expiry_renewal_failure_cannot_launder_backoff_at_token_expiry(
    tmp_path,
    failure,
):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    _dispatch(lifecycle, admission, at=NOW)
    active = lifecycle.complete_login_success(
        attempt_id=admission.attempt_id,
        now=NOW,
        access_expires_at=NOW + timedelta(minutes=30),
    )
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

    failed_at = active.access_expires_at - timedelta(seconds=1)
    _dispatch(lifecycle, started, at=due_at)
    failed = lifecycle.complete_renewal_failure(
        attempt_id=started.attempt_id,
        now=failed_at,
        failure=failure,
    )

    assert failed.state is ProphetXSessionState.RENEWAL_DUE
    assert failed.retry_not_before is not None
    assert failed.retry_not_before > active.access_expires_at
    assert failed.slot_hold_until == failed.retry_not_before

    blocked = lifecycle.begin_login(
        now=active.access_expires_at,
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == failed.retry_not_before
    assert blocked.login_authorized is False

    admitted = lifecycle.begin_login(
        now=failed.retry_not_before,
        access_token_available=False,
    )
    assert admitted.action is ProphetXLoginAdmissionAction.CREATE_LOGIN


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
    _dispatch(lifecycle, started, at=due_at)
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
    _dispatch(lifecycle, started, at=due_at)
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


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        (
            ProphetXRenewalFailureClass.RETRYABLE,
            ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
        ),
        (
            ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE,
            ProphetXSessionState.PROVIDER_UNAVAILABLE,
        ),
    ],
)
def test_late_renewal_failure_keeps_bounded_retry_before_replacement_login(
    tmp_path,
    failure,
    expected_state,
):
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

    failed_at = active.slot_hold_until + timedelta(seconds=1)
    _dispatch(lifecycle, started, at=due_at)
    failed = lifecycle.complete_renewal_failure(
        attempt_id=started.attempt_id,
        now=failed_at,
        failure=failure,
    )

    assert failed.state is expected_state
    assert failed.retry_not_before is not None
    assert failed.retry_not_before > failed_at
    assert failed.slot_hold_until is None
    assert failed.last_renewal_failure_class is failure

    blocked = lifecycle.begin_login(
        now=failed_at + timedelta(seconds=1),
        access_token_available=False,
    )
    assert blocked.action is ProphetXLoginAdmissionAction.RETRY_LATER
    assert blocked.retry_at == failed.retry_not_before
    assert blocked.login_authorized is False

    admitted = lifecycle.begin_login(
        now=failed.retry_not_before,
        access_token_available=False,
    )
    assert admitted.action is ProphetXLoginAdmissionAction.CREATE_LOGIN


def test_expired_renewal_backoff_can_extend_conservative_no_login_horizon(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["transient_failures"] = 32
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

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

    failed_at = active.slot_hold_until - timedelta(seconds=1)
    _dispatch(lifecycle, started, at=due_at)
    failed = lifecycle.complete_renewal_failure(
        attempt_id=started.attempt_id,
        now=failed_at,
        failure=ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE,
    )

    assert failed.state is ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    assert failed.slot_hold_until > active.slot_hold_until
    blocked = lifecycle.begin_login(
        now=active.slot_hold_until,
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == failed.slot_hold_until


def test_renewal_failure_after_short_expiry_preserves_provider_slot_hold(tmp_path):
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
    _dispatch(lifecycle, started, at=due_at)
    failed = lifecycle.complete_renewal_failure(
        attempt_id=started.attempt_id,
        now=active.access_expires_at + timedelta(seconds=1),
        failure=ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE,
    )

    assert failed.state is ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    assert failed.slot_hold_started_at == active.slot_hold_started_at
    assert failed.slot_hold_until == active.slot_hold_until
    blocked = lifecycle.begin_login(
        now=active.access_expires_at + timedelta(seconds=2),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == active.slot_hold_until

    fresh = lifecycle.begin_login(
        now=active.slot_hold_until,
        access_token_available=False,
    )
    assert fresh.action is ProphetXLoginAdmissionAction.CREATE_LOGIN


def test_credential_rotation_during_renewal_preserves_uncertainty_horizon(tmp_path):
    original = _lifecycle(tmp_path, revision="rev-1")
    active = _active(original)
    due_at = active.access_expires_at - timedelta(minutes=1)
    original.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    started = original.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    assert started.action is ProphetXLoginAdmissionAction.START_RENEWAL
    _dispatch(original, started, at=due_at)

    renewal_uncertainty = due_at + CONSERVATIVE_SESSION_SLOT_HOLD
    assert active.slot_hold_until < renewal_uncertainty

    rotated = _lifecycle(tmp_path, revision="rev-2")
    blocked = rotated.begin_login(
        now=active.slot_hold_until + timedelta(seconds=1),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == renewal_uncertainty
    assert blocked.snapshot.credential_revision == "rev-1"

    admitted = rotated.begin_login(
        now=renewal_uncertainty,
        access_token_available=False,
    )
    assert admitted.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert admitted.snapshot.credential_revision == "rev-2"

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="renewal attempt no longer owns current session authority",
    ):
        original.complete_renewal_failure(
            attempt_id=started.attempt_id,
            now=renewal_uncertainty + timedelta(seconds=1),
            failure=ProphetXRenewalFailureClass.AMBIGUOUS_PROVIDER_RESULT,
        )


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


@pytest.mark.parametrize(
    "login_failure,renewal_failure",
    [
        (ProphetXLoginFailureClass.CREDENTIAL_REJECTED, None),
        (None, ProphetXRenewalFailureClass.CREDENTIAL_REJECTED),
    ],
)
def test_credential_rejection_evidence_cannot_be_reclassified_as_wait(
    login_failure,
    renewal_failure,
):
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="credential-rejection evidence cannot appear outside",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
            generation=7,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            slot_hold_started_at=NOW,
            slot_hold_until=NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
            last_failure_class=login_failure,
            last_renewal_failure_class=renewal_failure,
        )


@pytest.mark.parametrize(
    "failure",
    [
        ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE,
        ProphetXLoginFailureClass.PROVIDER_UNAVAILABLE_PRE_SESSION,
        ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED,
    ],
)
def test_login_failure_evidence_cannot_be_laundered_into_wait_state(failure):
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="login failure evidence is incompatible with durable state",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
            generation=8,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            slot_hold_started_at=NOW,
            slot_hold_until=NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
            last_failure_class=failure,
        )


@pytest.mark.parametrize(
    "failure",
    [
        ProphetXRenewalFailureClass.RETRYABLE,
        ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE,
        ProphetXRenewalFailureClass.AMBIGUOUS_PROVIDER_RESULT,
    ],
)
def test_renewal_failure_evidence_cannot_be_laundered_into_active_state(
    failure,
):
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="renewal failure evidence is incompatible with durable state",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.ACTIVE,
            generation=9,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            session_lineage_id=sha256(b"session").hexdigest(),
            access_expires_at=NOW + timedelta(minutes=10),
            slot_hold_started_at=NOW,
            slot_hold_until=NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
            last_renewal_failure_class=failure,
        )


def test_credential_rejected_state_rejects_nonfuture_provider_slot_hold():
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="credential-rejected provider-slot hold must be future",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.CREDENTIAL_REJECTED,
            generation=8,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            slot_hold_started_at=NOW - CONSERVATIVE_SESSION_SLOT_HOLD,
            slot_hold_until=NOW,
            last_failure_class=ProphetXLoginFailureClass.CREDENTIAL_REJECTED,
        )


def test_persisted_credential_rejection_cannot_launder_through_wait_state(
    tmp_path,
):
    lifecycle = _lifecycle(tmp_path)
    _active(lifecycle)
    revoked = lifecycle.record_credential_revoked(
        now=NOW + timedelta(minutes=2)
    )
    assert revoked.slot_hold_until is not None

    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["state"] = ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY.value
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="credential-rejection evidence cannot appear outside",
    ):
        lifecycle.begin_login(
            now=revoked.slot_hold_until,
            access_token_available=False,
        )


def test_renewal_due_slot_hold_cannot_undercut_retry_horizon():
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="renewal slot hold cannot undercut retry horizon",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.RENEWAL_DUE,
            generation=4,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            session_lineage_id="a" * 64,
            access_expires_at=NOW + timedelta(minutes=1),
            slot_hold_started_at=NOW,
            slot_hold_until=NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
            retry_not_before=(
                NOW + CONSERVATIVE_SESSION_SLOT_HOLD + timedelta(seconds=1)
            ),
            transient_failures=1,
            last_renewal_failure_class=ProphetXRenewalFailureClass.RETRYABLE,
        )


@pytest.mark.parametrize(
    ("retry_not_before", "failure"),
    [
        (NOW + timedelta(seconds=5), None),
        (None, ProphetXRenewalFailureClass.RETRYABLE),
        (None, ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE),
    ],
)
def test_renewal_due_retry_horizon_must_match_failure_evidence(
    retry_not_before,
    failure,
):
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="retry horizon must match transient renewal failure evidence",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.RENEWAL_DUE,
            generation=4,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            session_lineage_id="a" * 64,
            access_expires_at=NOW + timedelta(minutes=1),
            slot_hold_started_at=NOW - timedelta(minutes=9),
            slot_hold_until=NOW + timedelta(minutes=11),
            retry_not_before=retry_not_before,
            transient_failures=1 if failure is not None else 0,
            last_renewal_failure_class=failure,
        )


@pytest.mark.parametrize(
    "renewal_failure",
    [
        ProphetXRenewalFailureClass.RETRYABLE,
        ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE,
        ProphetXRenewalFailureClass.AMBIGUOUS_PROVIDER_RESULT,
    ],
)
def test_provider_slot_wait_rejects_dual_failure_origins(renewal_failure):
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="login and renewal failure evidence cannot coexist",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
            generation=5,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            slot_hold_started_at=NOW,
            slot_hold_until=NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
            transient_failures=1,
            last_failure_class=ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT,
            last_renewal_failure_class=renewal_failure,
        )


def test_persisted_wait_rejects_laundered_dual_failure_origin(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    admission = lifecycle.begin_login(now=NOW, access_token_available=False)
    _dispatch(lifecycle, admission, at=NOW)
    lifecycle.complete_login_failure(
        attempt_id=admission.attempt_id,
        now=NOW + timedelta(seconds=1),
        failure=ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT,
    )

    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["last_renewal_failure_class"] = (
        ProphetXRenewalFailureClass.RETRYABLE.value
    )
    lifecycle.state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="login and renewal failure evidence cannot coexist",
    ):
        _lifecycle(tmp_path).read_snapshot()


@pytest.mark.parametrize(
    ("login_failure", "renewal_failure"),
    [
        (ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT, None),
        (None, ProphetXRenewalFailureClass.RETRYABLE),
        (None, ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE),
        (None, ProphetXRenewalFailureClass.AMBIGUOUS_PROVIDER_RESULT),
    ],
)
def test_refresh_success_wait_evidence_cannot_carry_failure_history(
    login_failure,
    renewal_failure,
):
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="refresh-success wait evidence cannot carry failure history",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
            generation=5,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            access_expires_at=NOW + timedelta(minutes=10),
            slot_hold_started_at=NOW,
            slot_hold_until=NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
            transient_failures=1,
            last_failure_class=login_failure,
            last_renewal_failure_class=renewal_failure,
        )


@pytest.mark.parametrize(
    "state",
    [
        ProphetXSessionState.NO_SESSION,
        ProphetXSessionState.ACTIVE,
        ProphetXSessionState.EXPIRED,
    ],
)
def test_nonfailure_states_reject_noncausal_transient_failure_count(state):
    active = state is ProphetXSessionState.ACTIVE
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="non-failure session state cannot carry transient failure count",
    ):
        ProphetXSessionSnapshot(
            state=state,
            generation=2,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            session_lineage_id="a" * 64 if active else None,
            access_expires_at=(
                NOW + timedelta(minutes=10) if active else None
            ),
            slot_hold_started_at=NOW if active else None,
            slot_hold_until=(
                NOW + CONSERVATIVE_SESSION_SLOT_HOLD if active else None
            ),
            transient_failures=1,
        )


@pytest.mark.parametrize(
    ("state", "login_failure", "renewal_failure", "slot_wait"),
    [
        (
            ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
            ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE,
            None,
            False,
        ),
        (
            ProphetXSessionState.PROVIDER_UNAVAILABLE,
            None,
            ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE,
            False,
        ),
        (
            ProphetXSessionState.SESSION_POOL_EXHAUSTED,
            ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED,
            None,
            True,
        ),
        (
            ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
            None,
            ProphetXRenewalFailureClass.AMBIGUOUS_PROVIDER_RESULT,
            True,
        ),
    ],
)
def test_transient_failure_evidence_requires_positive_failure_count(
    state,
    login_failure,
    renewal_failure,
    slot_wait,
):
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="transient failure evidence requires a positive failure count",
    ):
        ProphetXSessionSnapshot(
            state=state,
            generation=6,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            slot_hold_started_at=NOW if slot_wait else None,
            slot_hold_until=(
                NOW + CONSERVATIVE_SESSION_SLOT_HOLD
                if slot_wait
                else None
            ),
            retry_not_before=(
                None if slot_wait else NOW + timedelta(seconds=5)
            ),
            transient_failures=0,
            last_failure_class=login_failure,
            last_renewal_failure_class=renewal_failure,
        )


def test_renewal_credential_rejection_does_not_increment_transient_count(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    first = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    _dispatch(lifecycle, first, at=due_at)
    failed = lifecycle.complete_renewal_failure(
        attempt_id=first.attempt_id,
        now=due_at + timedelta(seconds=1),
        failure=ProphetXRenewalFailureClass.RETRYABLE,
    )
    assert failed.transient_failures == 1

    second = lifecycle.begin_renewal(
        now=failed.retry_not_before,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    _dispatch(lifecycle, second, at=failed.retry_not_before)
    rejected = lifecycle.complete_renewal_failure(
        attempt_id=second.attempt_id,
        now=failed.retry_not_before + timedelta(seconds=1),
        failure=ProphetXRenewalFailureClass.CREDENTIAL_REJECTED,
    )

    assert rejected.state is ProphetXSessionState.CREDENTIAL_REJECTED
    assert rejected.transient_failures == failed.transient_failures


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


@pytest.mark.parametrize(
    "state",
    [
        ProphetXSessionState.RENEWAL_DUE,
        ProphetXSessionState.RENEWING,
    ],
)
def test_persisted_renewal_state_cannot_precede_lead_window(state):
    access_expires_at = NOW + timedelta(minutes=10)
    kwargs = {
        "state": state,
        "generation": 3,
        "credential_revision": "rev-1",
        "integration_role": "market-maker-primary",
        "last_transition_at": NOW,
        "session_lineage_id": sha256(b"session").hexdigest(),
        "access_expires_at": access_expires_at,
        "slot_hold_started_at": NOW,
        "slot_hold_until": NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
    }
    if state is ProphetXSessionState.RENEWING:
        kwargs["attempt_id"] = sha256(b"renewal-attempt").hexdigest()
        kwargs["attempt_started_at"] = NOW

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="renewal state cannot precede its lead window",
    ):
        ProphetXSessionSnapshot(**kwargs)


@pytest.mark.parametrize(
    "state",
    [
        ProphetXSessionState.RENEWAL_DUE,
        ProphetXSessionState.RENEWING,
    ],
)
def test_persisted_renewal_state_accepts_time_inside_lead_window(state):
    access_expires_at = NOW + timedelta(minutes=10)
    due_at = access_expires_at - timedelta(minutes=1)
    kwargs = {
        "state": state,
        "generation": 4,
        "credential_revision": "rev-1",
        "integration_role": "market-maker-primary",
        "last_transition_at": due_at,
        "session_lineage_id": sha256(b"session").hexdigest(),
        "access_expires_at": access_expires_at,
        "slot_hold_started_at": NOW,
        "slot_hold_until": NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
    }
    if state is ProphetXSessionState.RENEWING:
        kwargs["attempt_id"] = sha256(b"renewal-attempt").hexdigest()
        kwargs["attempt_started_at"] = due_at

    snapshot = ProphetXSessionSnapshot(**kwargs)
    assert snapshot.state is state


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
    _dispatch(lifecycle, first_attempt, at=due_at)
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
    _dispatch(lifecycle, second_attempt, at=retry_at)
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

    _dispatch(lifecycle, admission, at=NOW)

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
    assert recovered.login_authorized is False
    assert lifecycle.consume_effect_authority(
        recovered,
        now=uncertainty_deadline + timedelta(seconds=1),
    ) is True


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


def test_state_writer_rejects_its_own_oversized_unicode_encoding(tmp_path):
    boundary_text = "😀" * 4096
    scope = ProphetXSessionScope(
        environment=boundary_text,
        access_key_identity_sha256=sha256(b"key-a").hexdigest(),
        credential_revision=boundary_text,
        integration_role=boundary_text,
    )
    lifecycle = ProphetXSessionLifecycle(tmp_path, scope=scope)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="exceeds the bounded file-size contract",
    ):
        lifecycle.begin_login(
            now=NOW,
            access_token_available=False,
        )

    assert lifecycle.state_path.exists() is False


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
    "state,expected_failure,wrong_failure",
    [
        (
            ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
            ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE,
            ProphetXLoginFailureClass.PROVIDER_UNAVAILABLE_PRE_SESSION,
        ),
        (
            ProphetXSessionState.PROVIDER_UNAVAILABLE,
            ProphetXLoginFailureClass.PROVIDER_UNAVAILABLE_PRE_SESSION,
            ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE,
        ),
        (
            ProphetXSessionState.SESSION_POOL_EXHAUSTED,
            ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED,
            ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT,
        ),
    ],
)
def test_login_failure_state_rejects_contradictory_failure_class(
    state,
    expected_failure,
    wrong_failure,
):
    kwargs = {
        "state": state,
        "generation": 1,
        "credential_revision": "rev-1",
        "integration_role": "market-maker-primary",
        "last_transition_at": NOW,
        "last_failure_class": wrong_failure,
    }
    if state is ProphetXSessionState.SESSION_POOL_EXHAUSTED:
        kwargs["slot_hold_started_at"] = NOW
        kwargs["slot_hold_until"] = NOW + CONSERVATIVE_SESSION_SLOT_HOLD
    else:
        kwargs["retry_not_before"] = NOW + timedelta(seconds=5)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="failure evidence does not match durable state",
    ):
        ProphetXSessionSnapshot(**kwargs)

    kwargs["last_failure_class"] = expected_failure
    snapshot = ProphetXSessionSnapshot(**kwargs)
    assert snapshot.last_failure_class is expected_failure


@pytest.mark.parametrize(
    "state,failure",
    [
        (
            ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
            ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE,
        ),
        (
            ProphetXSessionState.PROVIDER_UNAVAILABLE,
            ProphetXLoginFailureClass.PROVIDER_UNAVAILABLE_PRE_SESSION,
        ),
        (
            ProphetXSessionState.SESSION_POOL_EXHAUSTED,
            ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED,
        ),
    ],
)
def test_login_failure_state_rejects_renewal_failure_evidence(state, failure):
    kwargs = {
        "state": state,
        "generation": 1,
        "credential_revision": "rev-1",
        "integration_role": "market-maker-primary",
        "last_transition_at": NOW,
        "last_failure_class": failure,
        "last_renewal_failure_class": ProphetXRenewalFailureClass.RETRYABLE,
    }
    if state is ProphetXSessionState.SESSION_POOL_EXHAUSTED:
        kwargs["slot_hold_started_at"] = NOW
        kwargs["slot_hold_until"] = NOW + CONSERVATIVE_SESSION_SLOT_HOLD
    else:
        kwargs["retry_not_before"] = NOW + timedelta(seconds=5)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="failure evidence does not match durable state",
    ):
        ProphetXSessionSnapshot(**kwargs)



@pytest.mark.parametrize(
    "state,renewal_failure",
    [
        (
            ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
            ProphetXRenewalFailureClass.RETRYABLE,
        ),
        (
            ProphetXSessionState.PROVIDER_UNAVAILABLE,
            ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE,
        ),
    ],
)
def test_retryable_durable_state_accepts_matching_renewal_origin(
    state,
    renewal_failure,
):
    snapshot = ProphetXSessionSnapshot(
        state=state,
        generation=5,
        credential_revision="rev-1",
        integration_role="market-maker-primary",
        last_transition_at=NOW,
        retry_not_before=NOW + timedelta(seconds=5),
        last_renewal_failure_class=renewal_failure,
    )

    assert snapshot.state is state
    assert snapshot.last_failure_class is None
    assert snapshot.last_renewal_failure_class is renewal_failure


@pytest.mark.parametrize(
    "last_login_failure,last_renewal_failure",
    [
        (None, None),
        (
            ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE,
            None,
        ),
        (
            None,
            ProphetXRenewalFailureClass.RETRYABLE,
        ),
        (
            ProphetXLoginFailureClass.CREDENTIAL_REJECTED,
            ProphetXRenewalFailureClass.CREDENTIAL_REJECTED,
        ),
    ],
)
def test_credential_rejected_state_requires_exact_single_rejection_cause(
    last_login_failure,
    last_renewal_failure,
):
    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="credential-rejected state requires exact rejection evidence",
    ):
        ProphetXSessionSnapshot(
            state=ProphetXSessionState.CREDENTIAL_REJECTED,
            generation=1,
            credential_revision="rev-1",
            integration_role="market-maker-primary",
            last_transition_at=NOW,
            last_failure_class=last_login_failure,
            last_renewal_failure_class=last_renewal_failure,
        )


def test_credential_rejected_state_accepts_login_or_renewal_rejection():
    login_rejected = ProphetXSessionSnapshot(
        state=ProphetXSessionState.CREDENTIAL_REJECTED,
        generation=1,
        credential_revision="rev-1",
        integration_role="market-maker-primary",
        last_transition_at=NOW,
        last_failure_class=ProphetXLoginFailureClass.CREDENTIAL_REJECTED,
    )
    renewal_rejected = ProphetXSessionSnapshot(
        state=ProphetXSessionState.CREDENTIAL_REJECTED,
        generation=2,
        credential_revision="rev-1",
        integration_role="market-maker-primary",
        last_transition_at=NOW,
        last_renewal_failure_class=ProphetXRenewalFailureClass.CREDENTIAL_REJECTED,
    )

    assert login_rejected.state is ProphetXSessionState.CREDENTIAL_REJECTED
    assert renewal_rejected.state is ProphetXSessionState.CREDENTIAL_REJECTED


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
    _dispatch(lifecycle, admission, at=NOW)
    lifecycle.complete_login_failure(
        attempt_id=admission.attempt_id,
        now=NOW + timedelta(seconds=1),
        failure=ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT,
    )
    payload = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
    payload["state"] = state.value
    payload["slot_hold_started_at"] = (
        NOW
        + timedelta(seconds=1)
        - CONSERVATIVE_SESSION_SLOT_HOLD
    ).isoformat()
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
    _dispatch(lifecycle, admission, at=NOW)
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
            slot_hold_started_at=NOW,
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
            slot_hold_started_at=NOW,
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

    _dispatch(lifecycle, started, at=due_at)

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

def test_login_dispatch_rebases_restart_slot_horizon(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    issued = lifecycle.begin_login(now=NOW, access_token_available=False)
    dispatch_at = NOW + timedelta(minutes=5)

    assert lifecycle.consume_effect_authority(
        issued,
        now=dispatch_at,
    ) is True
    dispatched = lifecycle.read_snapshot()
    assert dispatched.state is ProphetXSessionState.LOGIN_IN_FLIGHT
    assert dispatched.last_transition_at == dispatch_at
    assert dispatched.attempt_started_at == dispatch_at
    assert dispatched.slot_hold_started_at == dispatch_at
    assert (
        dispatched.slot_hold_until
        == dispatch_at + CONSERVATIVE_SESSION_SLOT_HOLD
    )

    restarted = _lifecycle(tmp_path)
    old_reservation_horizon = NOW + CONSERVATIVE_SESSION_SLOT_HOLD
    blocked = restarted.begin_login(
        now=old_reservation_horizon + timedelta(seconds=1),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == dispatched.slot_hold_until

    recovered = restarted.begin_login(
        now=dispatched.slot_hold_until,
        access_token_available=False,
    )
    assert recovered.action is ProphetXLoginAdmissionAction.CREATE_LOGIN


def test_renewal_dispatch_rebases_crash_uncertainty_horizon(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    issued = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    dispatch_at = due_at + timedelta(seconds=30)

    assert lifecycle.consume_effect_authority(
        issued,
        now=dispatch_at,
    ) is True
    dispatched = lifecycle.read_snapshot()
    assert dispatched.state is ProphetXSessionState.RENEWING
    assert dispatched.last_transition_at == dispatch_at
    assert dispatched.attempt_started_at == dispatch_at
    assert dispatched.slot_hold_started_at == dispatch_at
    assert (
        dispatched.slot_hold_until
        == dispatch_at + CONSERVATIVE_SESSION_SLOT_HOLD
    )

    restarted = _lifecycle(tmp_path)
    old_uncertainty_horizon = due_at + CONSERVATIVE_SESSION_SLOT_HOLD
    blocked = restarted.begin_login(
        now=old_uncertainty_horizon + timedelta(seconds=1),
        access_token_available=False,
    )
    assert (
        blocked.action
        is ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
    )
    assert blocked.retry_at == dispatched.slot_hold_until


def test_expired_login_reservation_cannot_consume_stale_effect_authority(
    tmp_path,
):
    lifecycle = _lifecycle(tmp_path)
    issued = lifecycle.begin_login(now=NOW, access_token_available=False)

    assert lifecycle.consume_effect_authority(
        issued,
        now=NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
    ) is False
    unchanged = lifecycle.read_snapshot()
    assert unchanged == issued.snapshot

    renewed = lifecycle.begin_login(
        now=NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
        access_token_available=False,
    )
    assert renewed.action is ProphetXLoginAdmissionAction.CREATE_LOGIN
    assert renewed.attempt_id != issued.attempt_id
    assert lifecycle.consume_effect_authority(
        issued,
        now=NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
    ) is False


def test_expired_renewal_cannot_consume_stale_effect_authority(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    issued = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )

    assert lifecycle.consume_effect_authority(
        issued,
        now=active.access_expires_at,
    ) is False
    unchanged = lifecycle.read_snapshot()
    assert unchanged == issued.snapshot


def test_structurally_valid_forged_login_admission_has_no_effect_authority(
    tmp_path,
):
    lifecycle = _lifecycle(tmp_path)
    attempt = "a" * 64
    snapshot = ProphetXSessionSnapshot(
        state=ProphetXSessionState.LOGIN_IN_FLIGHT,
        generation=1,
        credential_revision="rev-1",
        integration_role="market-maker-primary",
        last_transition_at=NOW,
        attempt_id=attempt,
        attempt_started_at=NOW,
        slot_hold_started_at=NOW,
        slot_hold_until=NOW + CONSERVATIVE_SESSION_SLOT_HOLD,
    )
    forged = ProphetXLoginAdmission(
        action=ProphetXLoginAdmissionAction.CREATE_LOGIN,
        snapshot=snapshot,
        attempt_id=attempt,
        retry_at=snapshot.slot_hold_until,
    )

    assert forged.login_authorized is False
    assert lifecycle.consume_effect_authority(forged, now=NOW) is False


def test_reconstructed_login_admission_cannot_reuse_issued_attempt_authority(
    tmp_path,
):
    lifecycle = _lifecycle(tmp_path)
    issued = lifecycle.begin_login(now=NOW, access_token_available=False)
    forged = ProphetXLoginAdmission(
        action=issued.action,
        snapshot=issued.snapshot,
        attempt_id=issued.attempt_id,
        retry_at=issued.retry_at,
    )

    assert lifecycle.consume_effect_authority(issued, now=NOW) is True
    assert lifecycle.consume_effect_authority(forged, now=NOW) is False

    restarted = _lifecycle(tmp_path)
    assert restarted.consume_effect_authority(issued, now=NOW) is False


def test_login_completion_requires_consumed_effect_authority(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    issued = lifecycle.begin_login(now=NOW, access_token_available=False)

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="login effect authority was not consumed",
    ):
        lifecycle.complete_login_success(
            attempt_id=issued.attempt_id,
            now=NOW + timedelta(seconds=1),
            access_expires_at=NOW + timedelta(minutes=10),
        )

    assert lifecycle.read_snapshot() == issued.snapshot
    assert issued.attempt_id in lifecycle._issued_effect_admissions


def test_renewal_completion_requires_consumed_effect_authority(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    issued = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="renewal effect authority was not consumed",
    ):
        lifecycle.complete_renewal_failure(
            attempt_id=issued.attempt_id,
            now=due_at + timedelta(seconds=1),
            failure=ProphetXRenewalFailureClass.RETRYABLE,
        )

    assert lifecycle.read_snapshot() == issued.snapshot
    assert issued.attempt_id in lifecycle._issued_effect_admissions


def test_consumed_effect_authority_cannot_be_reused_after_login_completion(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    issued = lifecycle.begin_login(now=NOW, access_token_available=False)
    assert lifecycle.consume_effect_authority(issued, now=NOW) is True

    lifecycle.complete_login_failure(
        attempt_id=issued.attempt_id,
        now=NOW + timedelta(seconds=1),
        failure=ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE,
    )

    assert lifecycle.consume_effect_authority(issued, now=NOW) is False


def test_completed_login_attempt_is_removed_from_effect_registry(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    issued = lifecycle.begin_login(now=NOW, access_token_available=False)
    assert issued.attempt_id in lifecycle._issued_effect_admissions

    _dispatch(lifecycle, issued, at=NOW)
    lifecycle.complete_login_success(
        attempt_id=issued.attempt_id,
        now=NOW + timedelta(seconds=1),
        access_expires_at=NOW + timedelta(minutes=10),
    )

    assert issued.attempt_id not in lifecycle._issued_effect_admissions


def test_completed_renewal_attempt_is_removed_from_effect_registry(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    issued = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    assert issued.attempt_id in lifecycle._issued_effect_admissions

    _dispatch(lifecycle, issued, at=due_at)
    lifecycle.complete_renewal_failure(
        attempt_id=issued.attempt_id,
        now=due_at + timedelta(seconds=1),
        failure=ProphetXRenewalFailureClass.RETRYABLE,
    )

    assert issued.attempt_id not in lifecycle._issued_effect_admissions


def test_reconstructed_renewal_admission_cannot_reuse_issued_authority(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    active = _active(lifecycle)
    due_at = active.access_expires_at - timedelta(minutes=1)
    lifecycle.begin_login(
        now=due_at,
        access_token_available=True,
        access_token_lineage_id=active.session_lineage_id,
    )
    issued = lifecycle.begin_renewal(
        now=due_at,
        refresh_token_lineage_id=active.session_lineage_id,
    )
    forged = ProphetXLoginAdmission(
        action=issued.action,
        snapshot=issued.snapshot,
        attempt_id=issued.attempt_id,
        retry_at=issued.retry_at,
    )

    assert lifecycle.consume_effect_authority(issued, now=due_at) is True
    assert lifecycle.consume_effect_authority(forged, now=due_at) is False
    assert lifecycle.consume_effect_authority(issued, now=due_at) is False


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


def test_state_file_oversized_json_integer_fails_closed(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    lifecycle.begin_login(now=NOW, access_token_available=False)
    raw = lifecycle.state_path.read_text(encoding="utf-8")
    assert '"generation":0' in raw
    raw = raw.replace(
        '"generation":0',
        '"generation":' + ("9" * 5000),
        1,
    )
    assert len(raw.encode("utf-8")) < 64 * 1024
    lifecycle.state_path.write_text(raw, encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="unreadable or corrupt",
    ):
        lifecycle.read_snapshot()


def test_state_file_excessive_json_nesting_fails_closed(tmp_path):
    lifecycle = _lifecycle(tmp_path)
    lifecycle.begin_login(now=NOW, access_token_available=False)
    raw = lifecycle.state_path.read_text(encoding="utf-8")
    assert '"generation":0' in raw
    depth = 1500
    raw = raw.replace(
        '"generation":0',
        '"generation":' + ("[" * depth) + "0" + ("]" * depth),
        1,
    )
    assert len(raw.encode("utf-8")) < 64 * 1024
    lifecycle.state_path.write_text(raw, encoding="utf-8")

    with pytest.raises(
        ProphetXSessionLifecycleError,
        match="unreadable or corrupt",
    ):
        lifecycle.read_snapshot()


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
