from __future__ import annotations

import json

import pytest

from autosport.prophetx_account_link import (
    AccountLinkBlocked,
    AccountLinkInputError,
    AccountLinkState,
    AccountLinkStateError,
    DiagnosticCode,
    LoginDisposition,
    LoginOutcome,
    ProviderAuthFailure,
    ProphetXAccountLinkController,
)


class FakeClock:
    def __init__(self, value: int = 10_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class FakeTransport:
    def __init__(self) -> None:
        self.begin_outcome = LoginOutcome(
            LoginDisposition.AUTHENTICATED,
            session_ref="session-ref-1",
        )
        self.verify_outcome = LoginOutcome(
            LoginDisposition.AUTHENTICATED,
            session_ref="session-ref-2",
        )
        self.begin_failure = None
        self.send_failure = None
        self.verify_failure = None
        self.begin_calls = []
        self.send_calls = []
        self.verify_calls = []
        self.cancel_calls = []

    def begin_login(self, *, email, password, device_id):
        self.begin_calls.append((email, password, device_id))
        if self.begin_failure is not None:
            raise self.begin_failure
        return self.begin_outcome

    def send_verification_code(self, *, challenge_ref):
        self.send_calls.append(challenge_ref)
        if self.send_failure is not None:
            raise self.send_failure

    def verify_two_factor(self, *, challenge_ref, code):
        self.verify_calls.append((challenge_ref, code))
        if self.verify_failure is not None:
            raise self.verify_failure
        return self.verify_outcome

    def cancel_challenge(self, *, challenge_ref):
        self.cancel_calls.append(challenge_ref)


class FakeSink:
    def __init__(self) -> None:
        self.calls = []
        self.failure = None
        self.result = "credential-ref-1"
        self.remove_calls = []
        self.remove_failure = None

    def store_prophetx_credentials(self, *, access_key, secret_key, environment):
        self.calls.append((access_key, secret_key, environment))
        if self.failure is not None:
            raise self.failure
        return self.result

    def remove_prophetx_credentials(self, *, credential_ref):
        self.remove_calls.append(credential_ref)
        if self.remove_failure is not None:
            raise self.remove_failure


def controller(*, transport=None, sink=None, clock=None):
    transport = transport or FakeTransport()
    sink = sink or FakeSink()
    clock = clock or FakeClock()
    ctl = ProphetXAccountLinkController(
        transport=transport,
        credential_sink=sink,
        device_id="stable-device-1",
        clock_ns=clock,
        resend_interval_seconds=30,
    )
    ctl.open_login()
    return ctl, transport, sink, clock


def make_2fa(ctl, transport):
    transport.begin_outcome = LoginOutcome(
        LoginDisposition.TWO_FACTOR_REQUIRED,
        challenge_ref="challenge-ref-1",
    )
    ctl.submit_login(email="operator@example.test", password="PASSWORD_SECRET")
    assert ctl.state is AccountLinkState.TWO_FACTOR_REQUIRED


def snapshot_text(ctl):
    return json.dumps(ctl.public_snapshot().to_dict(), sort_keys=True)


def test_login_success_is_authenticated_but_never_execution_enabled():
    ctl, transport, _, _ = controller()
    ctl.submit_login(email="operator@example.test", password="PASSWORD_SECRET")

    assert ctl.state is AccountLinkState.AUTHENTICATED_SESSION
    snap = ctl.public_snapshot()
    assert snap.execution_enabled is False
    assert snap.real_money_execution is False
    assert snap.token_contract_verified is False
    assert transport.begin_calls == [
        ("operator@example.test", "PASSWORD_SECRET", "stable-device-1")
    ]


def test_opaque_password_preserves_provider_significant_surrounding_whitespace():
    ctl, transport, _, _ = controller()
    password = "  PASSWORD_SECRET  "

    ctl.submit_login(email="operator@example.test", password=password)

    assert transport.begin_calls == [
        ("operator@example.test", password, "stable-device-1")
    ]


def test_opaque_2fa_code_preserves_provider_significant_surrounding_whitespace():
    ctl, transport, _, _ = controller()
    make_2fa(ctl, transport)
    ctl.send_verification_code()
    code = "  123456  "

    ctl.verify_two_factor(code=code)

    assert transport.verify_calls == [("challenge-ref-1", code)]


def test_opaque_manual_api_secrets_are_stored_without_normalization():
    ctl, _, sink, _ = controller()
    access_key = "  ACCESS_SECRET  "
    secret_key = "  SUPER_SECRET  "

    ctl.import_approved_api_token(
        access_key=access_key,
        secret_key=secret_key,
        environment="sandbox",
    )

    assert sink.calls == [(access_key, secret_key, "sandbox")]


def test_2fa_required_is_deterministic_and_public_state_has_no_secret_context():
    ctl, transport, _, _ = controller()
    make_2fa(ctl, transport)

    text = snapshot_text(ctl)
    assert ctl.state is AccountLinkState.TWO_FACTOR_REQUIRED
    assert "PASSWORD_SECRET" not in text
    assert "operator@example.test" not in text
    assert "stable-device-1" not in text
    assert "challenge-ref-1" not in text
    assert "session-ref" not in text


def test_send_code_throttles_repeated_keypress_before_second_dispatch():
    ctl, transport, _, clock = controller()
    make_2fa(ctl, transport)

    ctl.send_verification_code()
    ctl.send_verification_code()

    assert transport.send_calls == ["challenge-ref-1"]
    snap = ctl.public_snapshot()
    assert snap.diagnostic_code == DiagnosticCode.VERIFICATION_THROTTLED.value
    assert snap.resend_after_ms == 30_000
    assert snap.can_send_verification_code is False

    clock.value += 30_000_000_000
    ctl.send_verification_code()
    assert transport.send_calls == ["challenge-ref-1", "challenge-ref-1"]


def test_failed_sms_dispatch_is_still_locally_throttled():
    ctl, transport, _, _ = controller()
    make_2fa(ctl, transport)
    transport.send_failure = ProviderAuthFailure(
        DiagnosticCode.PROVIDER_UNAVAILABLE,
        raw_detail="<html>TOKEN_SECRET</html>",
    )

    ctl.send_verification_code()
    ctl.send_verification_code()

    assert len(transport.send_calls) == 1
    text = snapshot_text(ctl)
    assert "TOKEN_SECRET" not in text
    assert "<html>" not in text


def test_wrong_code_is_recoverable_without_persisting_code_or_raw_error():
    ctl, transport, _, _ = controller()
    make_2fa(ctl, transport)
    ctl.send_verification_code()
    transport.verify_failure = ProviderAuthFailure(
        DiagnosticCode.VERIFICATION_FAILED,
        raw_detail="CODE_SECRET server body",
    )

    ctl.verify_two_factor(code="CODE_SECRET")

    assert ctl.state is AccountLinkState.AUTH_ERROR
    assert ctl.public_snapshot().can_verify_two_factor is True
    text = snapshot_text(ctl)
    assert "CODE_SECRET" not in text
    assert "server body" not in text


def test_successful_2fa_reaches_authenticated_session_without_exposing_code():
    ctl, transport, _, _ = controller()
    make_2fa(ctl, transport)
    ctl.send_verification_code()

    ctl.verify_two_factor(code="123456")

    assert ctl.state is AccountLinkState.AUTHENTICATED_SESSION
    assert transport.verify_calls == [("challenge-ref-1", "123456")]
    text = snapshot_text(ctl)
    assert "123456" not in text
    assert "challenge-ref-1" not in text


def test_provider_html_or_exception_text_never_becomes_public_diagnostic():
    ctl, transport, _, _ = controller()
    transport.begin_failure = ProviderAuthFailure(
        DiagnosticCode.PROVIDER_CONFIGURATION,
        raw_detail="<html>Authorization: Bearer TOKEN_SECRET</html>",
    )

    ctl.submit_login(email="operator@example.test", password="PASSWORD_SECRET")

    snap = ctl.public_snapshot()
    assert snap.diagnostic_code == DiagnosticCode.PROVIDER_CONFIGURATION.value
    text = snapshot_text(ctl)
    for secret in ("TOKEN_SECRET", "PASSWORD_SECRET", "Authorization", "<html>"):
        assert secret not in text


def test_untyped_hostile_transport_failure_is_bounded():
    class HostileError(RuntimeError):
        def __str__(self):
            raise RuntimeError("must not render")

    ctl, transport, _, _ = controller()
    transport.begin_failure = HostileError()

    ctl.submit_login(email="operator@example.test", password="PASSWORD_SECRET")

    assert ctl.state is AccountLinkState.PROVIDER_UNAVAILABLE
    assert ctl.public_snapshot().diagnostic_code == "PROVIDER_UNAVAILABLE"


def test_transport_rebinding_cannot_redirect_existing_2fa_challenge():
    first = FakeTransport()
    hostile = FakeTransport()
    hostile.send_calls = []
    ctl, _, _, _ = controller(transport=first)
    make_2fa(ctl, first)

    ctl._transport = hostile
    ctl.send_verification_code()

    assert first.send_calls == ["challenge-ref-1"]
    assert hostile.send_calls == []


def test_credential_sink_rebinding_cannot_redirect_secret_storage():
    first = FakeSink()
    hostile = FakeSink()
    ctl, _, _, _ = controller(sink=first)
    ctl._credential_sink = hostile

    ctl.import_approved_api_token(
        access_key="ACCESS_SECRET",
        secret_key="SUPER_SECRET",
        environment="sandbox",
    )

    assert first.calls == [("ACCESS_SECRET", "SUPER_SECRET", "sandbox")]
    assert hostile.calls == []


def test_clock_rebinding_cannot_bypass_existing_resend_throttle():
    clock = FakeClock()
    ctl, transport, _, _ = controller(clock=clock)
    make_2fa(ctl, transport)
    ctl.send_verification_code()

    hostile_calls = []

    def hostile_clock():
        hostile_calls.append(True)
        return clock.value + 10**15

    ctl._clock_ns = hostile_clock
    snap = ctl.public_snapshot()
    ctl.send_verification_code()

    assert hostile_calls == []
    assert snap.resend_after_ms == 30_000
    assert transport.send_calls == ["challenge-ref-1"]


def test_direct_key_generation_is_fail_closed_without_any_transport_call():
    ctl, transport, _, _ = controller()
    ctl.submit_login(email="operator@example.test", password="PASSWORD_SECRET")
    before = (
        len(transport.begin_calls),
        len(transport.send_calls),
        len(transport.verify_calls),
    )

    with pytest.raises(AccountLinkBlocked, match="token contract is unverified"):
        ctl.request_direct_key_generation()

    assert ctl.state is AccountLinkState.KEY_GENERATION_BLOCKED_UNVERIFIED_TOKEN_CONTRACT
    assert (
        len(transport.begin_calls),
        len(transport.send_calls),
        len(transport.verify_calls),
    ) == before
    snap = ctl.public_snapshot()
    assert snap.direct_key_generation_available is False
    assert snap.execution_enabled is False


def test_manual_approved_token_import_uses_sink_once_and_public_snapshot_hides_reference():
    ctl, _, sink, _ = controller()

    ctl.import_approved_api_token(
        access_key="ACCESS_SECRET",
        secret_key="SUPER_SECRET",
        environment="sandbox",
    )

    assert sink.calls == [("ACCESS_SECRET", "SUPER_SECRET", "sandbox")]
    snap = ctl.public_snapshot()
    assert snap.state == AccountLinkState.LINKED_CREDENTIAL_STORED.value
    assert snap.credential_present is True
    text = snapshot_text(ctl)
    assert "credential-ref-1" not in text
    assert "ACCESS_SECRET" not in text
    assert "SUPER_SECRET" not in text
    assert snap.execution_enabled is False
    assert snap.real_money_execution is False


@pytest.mark.parametrize(
    "access_key,secret_key",
    [
        ("", "secret"),
        ("access", ""),
        (" ", "secret"),
        ("access", " "),
        (None, "secret"),
        ("access", None),
    ],
)
def test_partial_or_missing_manual_credentials_never_call_sink(access_key, secret_key):
    ctl, _, sink, _ = controller()

    with pytest.raises(AccountLinkInputError):
        ctl.import_approved_api_token(
            access_key=access_key,
            secret_key=secret_key,
            environment="sandbox",
        )

    assert sink.calls == []
    assert ctl.public_snapshot().credential_present is False


def test_second_manual_token_import_cannot_orphan_first_credential_reference():
    ctl, _, sink, _ = controller()
    ctl.import_approved_api_token(
        access_key="ACCESS_SECRET",
        secret_key="SUPER_SECRET",
        environment="sandbox",
    )
    first_ref = sink.result
    sink.result = "credential-ref-2"

    with pytest.raises(
        AccountLinkStateError,
        match="explicitly unlinked",
    ):
        ctl.import_approved_api_token(
            access_key="SECOND_ACCESS",
            secret_key="SECOND_SECRET",
            environment="sandbox",
        )

    assert len(sink.calls) == 1
    assert ctl.public_snapshot().credential_present is True
    assert ctl.public_snapshot().can_import_approved_api_token is False
    ctl.unlink()
    assert sink.remove_calls == [first_ref]


def test_failed_unlink_keeps_existing_credential_authoritative_and_blocks_relink():
    ctl, _, sink, _ = controller()
    ctl.import_approved_api_token(
        access_key="ACCESS_SECRET",
        secret_key="SUPER_SECRET",
        environment="sandbox",
    )
    sink.remove_failure = RuntimeError("secret backend unavailable")

    ctl.unlink()

    snap = ctl.public_snapshot()
    assert sink.remove_calls == ["credential-ref-1"]
    assert snap.credential_present is True
    assert snap.can_submit_login is False
    assert snap.can_import_approved_api_token is False
    assert ctl.surface_contract().focus_target == "linked_status"

    with pytest.raises(AccountLinkStateError, match="explicitly unlinked"):
        ctl.open_login()
    with pytest.raises(AccountLinkStateError, match="explicitly unlinked"):
        ctl.submit_login(email="other@example.test", password="OTHER_SECRET")
    with pytest.raises(AccountLinkStateError, match="explicitly unlinked"):
        ctl.import_approved_api_token(
            access_key="SECOND_ACCESS",
            secret_key="SECOND_SECRET",
            environment="production",
        )
    assert len(sink.calls) == 1

    ctl.cancel()
    assert ctl.state is AccountLinkState.LINKED_CREDENTIAL_STORED
    assert ctl.public_snapshot().credential_present is True



def test_store_exception_quarantines_secret_lifecycle_until_reconciliation():
    ctl, transport, sink, _ = controller()
    # The controller cannot know whether the backend committed before an exception
    # escaped, so a retry must not be allowed to mint a second secret lifecycle.
    make_2fa(ctl, transport)
    sink.failure = RuntimeError("credential backend acknowledgement unavailable")

    ctl.import_approved_api_token(
        access_key="ACCESS_SECRET",
        secret_key="SUPER_SECRET",
        environment="production",
    )

    assert sink.calls == [("ACCESS_SECRET", "SUPER_SECRET", "production")]
    snap = ctl.public_snapshot()
    assert snap.state == AccountLinkState.AUTH_ERROR.value
    assert snap.diagnostic_code == DiagnosticCode.CREDENTIAL_STORAGE_FAILED.value
    assert snap.credential_present is False
    assert snap.can_submit_login is False
    assert snap.can_import_approved_api_token is False
    assert snap.can_send_verification_code is False
    assert snap.can_verify_two_factor is False
    assert transport.cancel_calls == ["challenge-ref-1"]
    assert ctl.surface_contract().focus_target == "prophetx-account-link-status"
    assert ctl.surface_contract().primary_action is None

    with pytest.raises(AccountLinkStateError, match="secret-store reconciliation"):
        ctl.import_approved_api_token(
            access_key="SECOND_ACCESS",
            secret_key="SECOND_SECRET",
            environment="sandbox",
        )
    assert len(sink.calls) == 1

    ctl.cancel()
    assert ctl.state is AccountLinkState.AUTH_ERROR
    ctl.unlink()
    assert ctl.state is AccountLinkState.AUTH_ERROR
    assert sink.remove_calls == []


def test_invalid_store_receipt_quarantines_secret_lifecycle_until_reconciliation():
    ctl, transport, sink, _ = controller()
    # Exercise the ambiguous receipt while transient 2FA authority exists. Quarantine
    # must discard/cancel that context as well as blocking future credential writes.
    make_2fa(ctl, transport)
    # Model a sink that may have committed the secret but violated its receipt
    # contract. The controller cannot safely address or remove that possible write.
    sink.result = " invalid-credential-reference "

    ctl.import_approved_api_token(
        access_key="ACCESS_SECRET",
        secret_key="SUPER_SECRET",
        environment="production",
    )

    assert sink.calls == [("ACCESS_SECRET", "SUPER_SECRET", "production")]
    snap = ctl.public_snapshot()
    assert snap.state == AccountLinkState.AUTH_ERROR.value
    assert snap.diagnostic_code == DiagnosticCode.CREDENTIAL_STORAGE_FAILED.value
    assert snap.credential_present is False
    assert snap.can_submit_login is False
    assert snap.can_import_approved_api_token is False
    assert snap.can_send_verification_code is False
    assert snap.can_verify_two_factor is False
    assert transport.cancel_calls == ["challenge-ref-1"]
    assert ctl.surface_contract().focus_target == "prophetx-account-link-status"
    assert ctl.surface_contract().primary_action is None

    with pytest.raises(AccountLinkStateError, match="secret-store reconciliation"):
        ctl.open_login()
    with pytest.raises(AccountLinkStateError, match="secret-store reconciliation"):
        ctl.submit_login(email="operator@example.test", password="PASSWORD_SECRET")
    with pytest.raises(AccountLinkStateError, match="secret-store reconciliation"):
        ctl.send_verification_code()
    with pytest.raises(AccountLinkStateError, match="secret-store reconciliation"):
        ctl.verify_two_factor(code="123456")
    with pytest.raises(AccountLinkStateError, match="secret-store reconciliation"):
        ctl.import_approved_api_token(
            access_key="SECOND_ACCESS",
            secret_key="SECOND_SECRET",
            environment="sandbox",
        )

    assert len(sink.calls) == 1
    assert sink.remove_calls == []

    # Generic UI lifecycle actions must not pretend the ambiguous durable outcome
    # was cleared. A fresh process must re-resolve canonical secret-store truth.
    ctl.cancel()
    assert ctl.state is AccountLinkState.AUTH_ERROR
    assert ctl.public_snapshot().can_import_approved_api_token is False
    ctl.unlink()
    assert ctl.state is AccountLinkState.AUTH_ERROR
    assert sink.remove_calls == []
    assert ProphetXAccountLinkController.restart_safe_state(ctl.public_snapshot()) == (
        AccountLinkState.LOGIN_FORM,
        None,
    )


def test_sink_failure_is_bounded_and_retains_no_credential_reference():
    ctl, _, sink, _ = controller()
    sink.failure = RuntimeError("SUPER_SECRET storage details")

    ctl.import_approved_api_token(
        access_key="ACCESS_SECRET",
        secret_key="SUPER_SECRET",
        environment="production",
    )

    snap = ctl.public_snapshot()
    assert snap.state == AccountLinkState.AUTH_ERROR.value
    assert snap.diagnostic_code == DiagnosticCode.CREDENTIAL_STORAGE_FAILED.value
    assert snap.credential_present is False
    assert "SUPER_SECRET" not in snapshot_text(ctl)


def test_manual_production_credential_presence_does_not_claim_production_execution():
    ctl, _, _, _ = controller()

    ctl.import_approved_api_token(
        access_key="ACCESS_SECRET",
        secret_key="SUPER_SECRET",
        environment="production",
    )

    snap = ctl.public_snapshot()
    assert snap.credential_present is True
    assert snap.execution_enabled is False
    assert snap.real_money_execution is False


def test_restart_during_2fa_drops_transient_auth_context():
    ctl, transport, _, _ = controller()
    make_2fa(ctl, transport)
    ctl.send_verification_code()

    snap = ctl.public_snapshot()
    restored_state, restored_credential_ref = ctl.restart_safe_state(snap)

    assert restored_state is AccountLinkState.LOGIN_FORM
    assert restored_credential_ref is None
    text = json.dumps(snap.to_dict(), sort_keys=True)
    assert "PASSWORD_SECRET" not in text
    assert "challenge-ref-1" not in text


def test_restart_does_not_trust_public_snapshot_as_credential_authority():
    ctl, _, _, _ = controller()
    ctl.import_approved_api_token(
        access_key="ACCESS_SECRET",
        secret_key="SUPER_SECRET",
        environment="sandbox",
    )

    restored_state, restored_credential_ref = ctl.restart_safe_state(
        ctl.public_snapshot()
    )

    assert restored_state is AccountLinkState.LOGIN_FORM
    assert restored_credential_ref is None


def test_forged_serialized_snapshot_cannot_mint_linked_credential_state():
    forged = {
        "state": AccountLinkState.LINKED_CREDENTIAL_STORED.value,
        "credential_present": True,
        "credential_ref": "attacker-selected-reference",
    }

    restored_state, restored_credential_ref = (
        ProphetXAccountLinkController.restart_safe_state(forged)
    )

    assert restored_state is AccountLinkState.LOGIN_FORM
    assert restored_credential_ref is None


def test_cancel_drops_2fa_context_and_best_effort_cancels_native_challenge():
    ctl, transport, _, _ = controller()
    make_2fa(ctl, transport)

    ctl.cancel()

    assert ctl.state is AccountLinkState.NOT_LINKED
    assert transport.cancel_calls == ["challenge-ref-1"]
    snap = ctl.public_snapshot()
    assert snap.can_verify_two_factor is False
    assert snap.can_send_verification_code is False


def test_session_expiry_clears_session_and_requires_safe_reauthentication():
    ctl, _, _, _ = controller()
    ctl.submit_login(email="operator@example.test", password="PASSWORD_SECRET")

    ctl.session_expired()

    snap = ctl.public_snapshot()
    assert snap.state == AccountLinkState.SESSION_EXPIRED.value
    assert snap.can_submit_login is True
    assert snap.execution_enabled is False


def test_unlink_removes_only_exact_opaque_credential_reference():
    ctl, _, sink, _ = controller()
    ctl.import_approved_api_token(
        access_key="ACCESS_SECRET",
        secret_key="SUPER_SECRET",
        environment="sandbox",
    )

    ctl.unlink()

    assert sink.remove_calls == ["credential-ref-1"]
    assert ctl.state is AccountLinkState.NOT_LINKED
    assert ctl.public_snapshot().credential_present is False


def test_public_snapshot_truth_flags_cannot_promote_machine_or_real_money_claims():
    ctl, _, _, _ = controller()
    snap = ctl.public_snapshot()

    assert snap.real_money_execution is False
    assert snap.human_tested is False
    assert snap.nvda_verified is False
    assert snap.execution_enabled is False
    assert snap.token_contract_verified is False


def test_clock_rollback_does_not_free_sms_throttle():
    ctl, transport, _, clock = controller()
    make_2fa(ctl, transport)
    ctl.send_verification_code()

    clock.value -= 1

    snap = ctl.public_snapshot()
    assert snap.resend_after_ms == 30_000
    assert snap.can_send_verification_code is False
    ctl.send_verification_code()
    assert len(transport.send_calls) == 1


def test_surface_contract_has_stable_accessible_names_and_secret_masking():
    ctl, _, _, _ = controller()

    surface = ctl.surface_contract()

    assert dict(surface.field_accessible_names) == {
        "email": "Email",
        "password": "Password",
        "verification_code": "Verification code",
        "access_key": "Access key",
        "secret_key": "Secret key",
    }
    assert surface.masked_fields == (
        "password",
        "verification_code",
        "access_key",
        "secret_key",
    )
    assert "email" not in surface.masked_fields
    assert surface.verification_code_input_mode == "single_text_input"
    assert surface.status_region_id == "prophetx-account-link-status"
    assert surface.status_live_mode == "polite"


def test_surface_contract_moves_2fa_focus_without_exposing_secret_context():
    ctl, transport, _, _ = controller()
    make_2fa(ctl, transport)

    required = ctl.surface_contract()
    assert required.focus_target == "send_verification_code"
    assert required.primary_action == "send_verification_code"

    ctl.send_verification_code()
    code_entry = ctl.surface_contract()
    assert code_entry.focus_target == "verification_code"
    assert code_entry.primary_action == "verify_two_factor"


def test_authenticated_surface_never_offers_unverified_direct_key_generation():
    ctl, _, _, _ = controller()
    ctl.submit_login(email="operator@example.test", password="PASSWORD_SECRET")

    surface = ctl.surface_contract()

    assert surface.focus_target == "manual_api_token_import"
    assert surface.primary_action == "import_approved_api_token"
    assert "generate" not in (surface.primary_action or "")


def test_linked_surface_focuses_plain_status_and_has_no_secret_action():
    ctl, _, _, _ = controller()
    ctl.import_approved_api_token(
        access_key="ACCESS_SECRET",
        secret_key="SUPER_SECRET",
        environment="sandbox",
    )

    surface = ctl.surface_contract()

    assert surface.focus_target == "linked_status"
    assert surface.primary_action is None
