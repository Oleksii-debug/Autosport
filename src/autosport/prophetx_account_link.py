from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from time import monotonic_ns
from typing import Callable, Protocol


class AccountLinkError(RuntimeError):
    """Base error for the bounded ProphetX account-link controller."""


class AccountLinkStateError(AccountLinkError):
    """Raised when an operator action is invalid for the current state."""


class AccountLinkInputError(AccountLinkError):
    """Raised for invalid local operator input without echoing secret material."""


class AccountLinkBlocked(AccountLinkError):
    """Raised when an externally unverified provider contract blocks progress."""


class AccountLinkState(str, Enum):
    NOT_LINKED = "NOT_LINKED"
    LOGIN_FORM = "LOGIN_FORM"
    AUTHENTICATING = "AUTHENTICATING"
    TWO_FACTOR_REQUIRED = "TWO_FACTOR_REQUIRED"
    TWO_FACTOR_SENDING = "TWO_FACTOR_SENDING"
    TWO_FACTOR_CODE_ENTRY = "TWO_FACTOR_CODE_ENTRY"
    TWO_FACTOR_VERIFYING = "TWO_FACTOR_VERIFYING"
    AUTHENTICATED_SESSION = "AUTHENTICATED_SESSION"
    KEY_GENERATION_BLOCKED_UNVERIFIED_TOKEN_CONTRACT = (
        "KEY_GENERATION_BLOCKED_UNVERIFIED_TOKEN_CONTRACT"
    )
    KEY_GENERATING = "KEY_GENERATING"
    LINKED_CREDENTIAL_STORED = "LINKED_CREDENTIAL_STORED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    AUTH_ERROR = "AUTH_ERROR"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class LoginDisposition(str, Enum):
    AUTHENTICATED = "AUTHENTICATED"
    TWO_FACTOR_REQUIRED = "TWO_FACTOR_REQUIRED"


class DiagnosticCode(str, Enum):
    NONE = "NONE"
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    VERIFICATION_THROTTLED = "VERIFICATION_THROTTLED"
    PROVIDER_CONFIGURATION = "PROVIDER_CONFIGURATION"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    CREDENTIAL_STORAGE_FAILED = "CREDENTIAL_STORAGE_FAILED"
    TOKEN_CONTRACT_UNVERIFIED = "TOKEN_CONTRACT_UNVERIFIED"


_PROVIDER_DIAGNOSTICS = frozenset(
    {
        DiagnosticCode.INVALID_CREDENTIALS,
        DiagnosticCode.VERIFICATION_FAILED,
        DiagnosticCode.PROVIDER_CONFIGURATION,
        DiagnosticCode.PROVIDER_UNAVAILABLE,
        DiagnosticCode.SESSION_EXPIRED,
    }
)


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    disposition: LoginDisposition
    session_ref: str | None = None
    challenge_ref: str | None = None

    def __post_init__(self) -> None:
        if type(self.disposition) is not LoginDisposition:
            raise TypeError("disposition must be exact LoginDisposition")
        if self.disposition is LoginDisposition.AUTHENTICATED:
            if not _opaque_ref(self.session_ref):
                raise ValueError("authenticated outcome requires opaque session reference")
            if self.challenge_ref is not None:
                raise ValueError("authenticated outcome cannot carry challenge reference")
        else:
            if not _opaque_ref(self.challenge_ref):
                raise ValueError("2FA outcome requires opaque challenge reference")
            if self.session_ref is not None:
                raise ValueError("2FA outcome cannot carry session reference")


class ProviderAuthFailure(Exception):
    """Typed provider failure whose raw detail is deliberately non-authoritative."""

    def __init__(
        self,
        code: DiagnosticCode,
        *,
        raw_detail: object | None = None,
    ) -> None:
        if type(code) is not DiagnosticCode or code not in _PROVIDER_DIAGNOSTICS:
            raise ValueError("unsupported provider diagnostic code")
        super().__init__(code.value)
        self.code = code
        self.raw_detail = raw_detail


class ProphetXAccountLinkTransport(Protocol):
    """Native-only auth transport boundary; never implemented by WebView JavaScript."""

    def begin_login(
        self,
        *,
        email: str,
        password: str,
        device_id: str,
    ) -> LoginOutcome: ...

    def send_verification_code(
        self,
        *,
        challenge_ref: str,
    ) -> None: ...

    def verify_two_factor(
        self,
        *,
        challenge_ref: str,
        code: str,
    ) -> LoginOutcome: ...

    def cancel_challenge(self, *, challenge_ref: str) -> None: ...


class ProphetXCredentialSink(Protocol):
    """Existing secret-lifecycle authority adapter; storage must be atomic."""

    def store_prophetx_credentials(
        self,
        *,
        access_key: str,
        secret_key: str,
        environment: str,
    ) -> str: ...

    def remove_prophetx_credentials(self, *, credential_ref: str) -> None: ...


def _canonical_text(value: object, name: str, *, max_length: int = 4096) -> str:
    if type(value) is not str:
        raise AccountLinkInputError(f"{name} must be text")
    cleaned = value.strip()
    if not cleaned or "\x00" in cleaned or len(cleaned) > max_length:
        raise AccountLinkInputError(f"{name} is invalid")
    try:
        cleaned.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AccountLinkInputError(f"{name} is invalid") from exc
    return cleaned


def _secret_text(value: object, name: str) -> str:
    """Validate an opaque secret without normalizing its provider-significant text."""

    if type(value) is not str:
        raise AccountLinkInputError(f"{name} must be text")
    if (
        not value
        or not value.strip()
        or "\x00" in value
        or len(value) > 16384
    ):
        raise AccountLinkInputError(f"{name} is invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AccountLinkInputError(f"{name} is invalid") from exc
    return value


def _opaque_ref(value: object) -> bool:
    if type(value) is not str:
        return False
    if not value or value != value.strip() or "\x00" in value or len(value) > 512:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _environment(value: object) -> str:
    raw = _canonical_text(value, "environment", max_length=32).lower()
    if raw not in {"sandbox", "production"}:
        raise AccountLinkInputError("environment must be sandbox or production")
    return raw


@dataclass(frozen=True, slots=True)
class AccountLinkSurfaceContract:
    """Provider-specific semantic contract for the canonical Windows shell.

    This is machine-checkable presentation metadata only. It does not assert that
    a physical UIA tree or NVDA speech output has been human-verified.
    """

    focus_target: str
    primary_action: str | None
    status_region_id: str = "prophetx-account-link-status"
    status_live_mode: str = "polite"
    verification_code_input_mode: str = "single_text_input"
    field_accessible_names: tuple[tuple[str, str], ...] = (
        ("email", "Email"),
        ("password", "Password"),
        ("verification_code", "Verification code"),
        ("access_key", "Access key"),
        ("secret_key", "Secret key"),
    )
    masked_fields: tuple[str, ...] = (
        "password",
        "verification_code",
        "access_key",
        "secret_key",
    )


@dataclass(frozen=True, slots=True)
class AccountLinkPublicSnapshot:
    state: str
    diagnostic_code: str
    can_submit_login: bool
    can_send_verification_code: bool
    can_verify_two_factor: bool
    can_import_approved_api_token: bool
    credential_present: bool
    resend_after_ms: int
    direct_key_generation_available: bool
    execution_enabled: bool
    real_money_execution: bool
    human_tested: bool
    nvda_verified: bool
    token_contract_verified: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "diagnostic_code": self.diagnostic_code,
            "can_submit_login": self.can_submit_login,
            "can_send_verification_code": self.can_send_verification_code,
            "can_verify_two_factor": self.can_verify_two_factor,
            "can_import_approved_api_token": self.can_import_approved_api_token,
            "credential_present": self.credential_present,
            "resend_after_ms": self.resend_after_ms,
            "direct_key_generation_available": self.direct_key_generation_available,
            "execution_enabled": self.execution_enabled,
            "real_money_execution": self.real_money_execution,
            "human_tested": self.human_tested,
            "nvda_verified": self.nvda_verified,
            "token_contract_verified": self.token_contract_verified,
        }


class ProphetXAccountLinkController:
    """Secret-minimizing operator state for ProphetX account linking.

    This controller deliberately does not implement HTTP, WebView DOM integration,
    credential persistence, provider capability promotion or order execution.

    Passwords, verification codes and imported API secrets exist only as call
    arguments long enough to cross an injected native boundary. Public snapshots
    contain neither those values nor the account email, device id, auth challenge,
    or provider session reference.
    """

    def __init__(
        self,
        *,
        transport: ProphetXAccountLinkTransport,
        credential_sink: ProphetXCredentialSink,
        device_id: str,
        clock_ns: Callable[[], int] = monotonic_ns,
        resend_interval_seconds: int = 30,
    ) -> None:
        if transport is None or credential_sink is None:
            raise ValueError("transport and credential_sink are required")
        begin_login = getattr(transport, "begin_login", None)
        send_verification_code = getattr(transport, "send_verification_code", None)
        verify_two_factor = getattr(transport, "verify_two_factor", None)
        cancel_challenge = getattr(transport, "cancel_challenge", None)
        store_credentials = getattr(
            credential_sink,
            "store_prophetx_credentials",
            None,
        )
        remove_credentials = getattr(
            credential_sink,
            "remove_prophetx_credentials",
            None,
        )
        if not all(
            callable(item)
            for item in (
                begin_login,
                send_verification_code,
                verify_two_factor,
                cancel_challenge,
            )
        ):
            raise TypeError("transport must provide the complete account-link auth surface")
        if not callable(store_credentials) or not callable(remove_credentials):
            raise TypeError(
                "credential_sink must provide atomic ProphetX store/remove authority"
            )
        self._transport = transport
        self._credential_sink = credential_sink
        self._begin_login = begin_login
        self._send_verification_code = send_verification_code
        self._verify_two_factor = verify_two_factor
        self._cancel_challenge = cancel_challenge
        self._store_credentials = store_credentials
        self._remove_credentials = remove_credentials
        self._device_id = _canonical_text(device_id, "device_id", max_length=512)
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        if type(resend_interval_seconds) is not int or resend_interval_seconds < 1:
            raise ValueError("resend_interval_seconds must be positive int")
        self._clock_ns = clock_ns
        self._clock_call = clock_ns
        self._resend_interval_ns = resend_interval_seconds * 1_000_000_000

        self._state = AccountLinkState.NOT_LINKED
        self._diagnostic = DiagnosticCode.NONE
        self._challenge_ref: str | None = None
        self._session_ref: str | None = None
        self._credential_ref: str | None = None
        # A successful sink call with an invalid receipt is an ambiguous durable
        # outcome: the secret may already exist but cannot be addressed safely.
        # Quarantine this controller until process restart + canonical secret-store
        # reconciliation rather than allowing another write to orphan/overwrite it.
        self._credential_lifecycle_uncertain = False
        self._last_verification_send_ns: int | None = None

    @property
    def state(self) -> AccountLinkState:
        return self._state

    def open_login(self) -> None:
        self._require_credential_lifecycle_resolved()
        if self._credential_ref is not None:
            raise AccountLinkStateError("linked credential must be explicitly unlinked first")
        self._clear_auth_context(cancel_challenge=True)
        self._state = AccountLinkState.LOGIN_FORM
        self._diagnostic = DiagnosticCode.NONE

    def submit_login(self, *, email: str, password: str) -> None:
        self._require_credential_lifecycle_resolved()
        if self._credential_ref is not None:
            raise AccountLinkStateError("linked credential must be explicitly unlinked first")
        if self._state not in {
            AccountLinkState.LOGIN_FORM,
            AccountLinkState.AUTH_ERROR,
            AccountLinkState.PROVIDER_UNAVAILABLE,
            AccountLinkState.SESSION_EXPIRED,
        }:
            raise AccountLinkStateError("login is not available in current state")

        email_value = _canonical_text(email, "email", max_length=512)
        password_value = _secret_text(password, "password")
        self._clear_auth_context(cancel_challenge=True)
        self._state = AccountLinkState.AUTHENTICATING
        self._diagnostic = DiagnosticCode.NONE

        try:
            outcome = self._begin_login(
                email=email_value,
                password=password_value,
                device_id=self._device_id,
            )
        except ProviderAuthFailure as exc:
            self._apply_provider_failure(exc)
            return
        except Exception:
            self._state = AccountLinkState.PROVIDER_UNAVAILABLE
            self._diagnostic = DiagnosticCode.PROVIDER_UNAVAILABLE
            return
        finally:
            password_value = ""
            email_value = ""

        self._accept_login_outcome(outcome)

    def send_verification_code(self) -> None:
        if self._state not in {
            AccountLinkState.TWO_FACTOR_REQUIRED,
            AccountLinkState.TWO_FACTOR_CODE_ENTRY,
            AccountLinkState.AUTH_ERROR,
            AccountLinkState.PROVIDER_UNAVAILABLE,
        }:
            raise AccountLinkStateError("verification-code send is not available")
        if not _opaque_ref(self._challenge_ref):
            raise AccountLinkStateError("2FA challenge context is unavailable")

        now = self._read_clock()
        remaining = self._resend_remaining_ns(now)
        if remaining > 0:
            self._diagnostic = DiagnosticCode.VERIFICATION_THROTTLED
            return

        challenge_ref = self._challenge_ref
        self._last_verification_send_ns = now
        self._state = AccountLinkState.TWO_FACTOR_SENDING
        self._diagnostic = DiagnosticCode.NONE
        try:
            self._send_verification_code(challenge_ref=challenge_ref)
        except ProviderAuthFailure as exc:
            self._apply_provider_failure(exc, preserve_challenge=True)
            return
        except Exception:
            self._state = AccountLinkState.PROVIDER_UNAVAILABLE
            self._diagnostic = DiagnosticCode.PROVIDER_UNAVAILABLE
            return
        self._state = AccountLinkState.TWO_FACTOR_CODE_ENTRY

    def verify_two_factor(self, *, code: str) -> None:
        if self._state not in {
            AccountLinkState.TWO_FACTOR_CODE_ENTRY,
            AccountLinkState.AUTH_ERROR,
            AccountLinkState.PROVIDER_UNAVAILABLE,
        }:
            raise AccountLinkStateError("2FA verification is not available")
        if not _opaque_ref(self._challenge_ref):
            raise AccountLinkStateError("2FA challenge context is unavailable")
        code_value = _secret_text(code, "verification code")
        challenge_ref = self._challenge_ref
        self._state = AccountLinkState.TWO_FACTOR_VERIFYING
        self._diagnostic = DiagnosticCode.NONE
        try:
            outcome = self._verify_two_factor(
                challenge_ref=challenge_ref,
                code=code_value,
            )
        except ProviderAuthFailure as exc:
            self._apply_provider_failure(exc, preserve_challenge=True)
            return
        except Exception:
            self._state = AccountLinkState.PROVIDER_UNAVAILABLE
            self._diagnostic = DiagnosticCode.PROVIDER_UNAVAILABLE
            return
        finally:
            code_value = ""
        self._accept_login_outcome(outcome)

    def request_direct_key_generation(self) -> None:
        if self._state not in {
            AccountLinkState.AUTHENTICATED_SESSION,
            AccountLinkState.KEY_GENERATION_BLOCKED_UNVERIFIED_TOKEN_CONTRACT,
        }:
            raise AccountLinkStateError("direct key generation requires authenticated session")
        self._state = AccountLinkState.KEY_GENERATION_BLOCKED_UNVERIFIED_TOKEN_CONTRACT
        self._diagnostic = DiagnosticCode.TOKEN_CONTRACT_UNVERIFIED
        raise AccountLinkBlocked("ProphetX Direct Link token contract is unverified")

    def import_approved_api_token(
        self,
        *,
        access_key: str,
        secret_key: str,
        environment: str,
    ) -> None:
        """Store provider-approved manually provisioned keys through the injected sink.

        This fallback does not claim that the Direct Link/2FA path ran, does not
        prove production entitlement, and cannot enable execution.
        """

        self._require_credential_lifecycle_resolved()
        if self._credential_ref is not None:
            raise AccountLinkStateError(
                "linked credential must be explicitly unlinked first"
            )

        access_value = _secret_text(access_key, "access key")
        secret_value = _secret_text(secret_key, "secret key")
        env_value = _environment(environment)

        try:
            credential_ref = self._store_credentials(
                access_key=access_value,
                secret_key=secret_value,
                environment=env_value,
            )
        except Exception:
            self._credential_ref = None
            self._state = AccountLinkState.AUTH_ERROR
            self._diagnostic = DiagnosticCode.CREDENTIAL_STORAGE_FAILED
            return
        finally:
            access_value = ""
            secret_value = ""

        if not _opaque_ref(credential_ref):
            self._credential_ref = None
            self._credential_lifecycle_uncertain = True
            self._state = AccountLinkState.AUTH_ERROR
            self._diagnostic = DiagnosticCode.CREDENTIAL_STORAGE_FAILED
            return
        self._clear_auth_context(cancel_challenge=True)
        self._credential_lifecycle_uncertain = False
        self._credential_ref = credential_ref
        self._state = AccountLinkState.LINKED_CREDENTIAL_STORED
        self._diagnostic = DiagnosticCode.NONE

    def session_expired(self) -> None:
        self._clear_auth_context(cancel_challenge=True)
        if self._credential_lifecycle_uncertain:
            self._state = AccountLinkState.AUTH_ERROR
            self._diagnostic = DiagnosticCode.CREDENTIAL_STORAGE_FAILED
            return
        self._state = AccountLinkState.SESSION_EXPIRED
        self._diagnostic = DiagnosticCode.SESSION_EXPIRED

    def cancel(self) -> None:
        self._clear_auth_context(cancel_challenge=True)
        if self._credential_lifecycle_uncertain:
            self._state = AccountLinkState.AUTH_ERROR
            self._diagnostic = DiagnosticCode.CREDENTIAL_STORAGE_FAILED
            return
        if self._credential_ref is not None:
            self._state = AccountLinkState.LINKED_CREDENTIAL_STORED
        else:
            self._state = AccountLinkState.NOT_LINKED
        self._diagnostic = DiagnosticCode.NONE

    def unlink(self) -> None:
        """Remove the exact stored credential through its original lifecycle authority."""

        if self._credential_ref is None:
            self.cancel()
            return
        credential_ref = self._credential_ref
        try:
            self._remove_credentials(credential_ref=credential_ref)
        except Exception:
            # The delete outcome is not proven. Retain the exact opaque reference and
            # block relinking so a possibly-still-stored secret cannot be orphaned.
            self._state = AccountLinkState.AUTH_ERROR
            self._diagnostic = DiagnosticCode.CREDENTIAL_STORAGE_FAILED
            return
        self._credential_ref = None
        self._credential_lifecycle_uncertain = False
        self.cancel()

    def surface_contract(self) -> AccountLinkSurfaceContract:
        """Return deterministic focus/action semantics without rendering a UI."""

        state = self._state
        challenge_available = _opaque_ref(self._challenge_ref)
        if self._credential_lifecycle_uncertain:
            focus_target = "prophetx-account-link-status"
            primary_action = None
        elif self._credential_ref is not None:
            focus_target = "linked_status"
            primary_action = None
        elif state is AccountLinkState.TWO_FACTOR_REQUIRED:
            focus_target = "send_verification_code"
            primary_action = "send_verification_code"
        elif state is AccountLinkState.TWO_FACTOR_CODE_ENTRY or (
            challenge_available
            and state
            in {
                AccountLinkState.AUTH_ERROR,
                AccountLinkState.PROVIDER_UNAVAILABLE,
            }
        ):
            focus_target = "verification_code"
            primary_action = "verify_two_factor"
        elif state is AccountLinkState.KEY_GENERATION_BLOCKED_UNVERIFIED_TOKEN_CONTRACT:
            focus_target = "manual_api_token_import"
            primary_action = "import_approved_api_token"
        elif state is AccountLinkState.LINKED_CREDENTIAL_STORED:
            focus_target = "linked_status"
            primary_action = None
        elif state in {
            AccountLinkState.AUTHENTICATING,
            AccountLinkState.TWO_FACTOR_SENDING,
            AccountLinkState.TWO_FACTOR_VERIFYING,
            AccountLinkState.KEY_GENERATING,
        }:
            focus_target = "prophetx-account-link-status"
            primary_action = None
        elif state is AccountLinkState.AUTHENTICATED_SESSION:
            focus_target = "manual_api_token_import"
            primary_action = "import_approved_api_token"
        else:
            focus_target = "email"
            primary_action = "submit_login"
        return AccountLinkSurfaceContract(
            focus_target=focus_target,
            primary_action=primary_action,
        )

    def public_snapshot(self) -> AccountLinkPublicSnapshot:
        now = self._read_clock()
        remaining_ns = self._resend_remaining_ns(now)
        resend_after_ms = (remaining_ns + 999_999) // 1_000_000 if remaining_ns else 0
        state = self._state
        challenge_available = _opaque_ref(self._challenge_ref)
        return AccountLinkPublicSnapshot(
            state=state.value,
            diagnostic_code=self._diagnostic.value,
            can_submit_login=bool(
                self._credential_ref is None
                and not self._credential_lifecycle_uncertain
                and state
                in {
                    AccountLinkState.LOGIN_FORM,
                    AccountLinkState.AUTH_ERROR,
                    AccountLinkState.PROVIDER_UNAVAILABLE,
                    AccountLinkState.SESSION_EXPIRED,
                }
            ),
            can_send_verification_code=bool(
                challenge_available
                and state
                in {
                    AccountLinkState.TWO_FACTOR_REQUIRED,
                    AccountLinkState.TWO_FACTOR_CODE_ENTRY,
                    AccountLinkState.AUTH_ERROR,
                    AccountLinkState.PROVIDER_UNAVAILABLE,
                }
                and remaining_ns == 0
            ),
            can_verify_two_factor=bool(
                challenge_available
                and state
                in {
                    AccountLinkState.TWO_FACTOR_CODE_ENTRY,
                    AccountLinkState.AUTH_ERROR,
                    AccountLinkState.PROVIDER_UNAVAILABLE,
                }
            ),
            can_import_approved_api_token=bool(
                self._credential_ref is None
                and not self._credential_lifecycle_uncertain
            ),
            credential_present=self._credential_ref is not None,
            resend_after_ms=int(resend_after_ms),
            direct_key_generation_available=False,
            execution_enabled=False,
            real_money_execution=False,
            human_tested=False,
            nvda_verified=False,
            token_contract_verified=False,
        )

    @classmethod
    def restart_safe_state(
        cls,
        snapshot: AccountLinkPublicSnapshot | dict[str, object],
    ) -> tuple[AccountLinkState, str | None]:
        """Return a non-authoritative safe entry point after process restart.

        A serialized/public UI snapshot is presentation data, not credential-store
        authority. It can never rehydrate a linked credential by itself. Product
        composition must re-resolve credential presence through the #1575-owned
        secret lifecycle authority and then construct fresh runtime state.
        """

        payload = (
            snapshot.to_dict()
            if type(snapshot) is AccountLinkPublicSnapshot
            else snapshot
        )
        if type(payload) is not dict:
            raise AccountLinkInputError("snapshot must be a public snapshot mapping")
        return AccountLinkState.LOGIN_FORM, None

    def _accept_login_outcome(self, outcome: LoginOutcome) -> None:
        if type(outcome) is not LoginOutcome:
            self._state = AccountLinkState.AUTH_ERROR
            self._diagnostic = DiagnosticCode.INVALID_CREDENTIALS
            return
        if outcome.disposition is LoginDisposition.AUTHENTICATED:
            self._challenge_ref = None
            self._last_verification_send_ns = None
            self._session_ref = outcome.session_ref
            self._state = AccountLinkState.AUTHENTICATED_SESSION
            self._diagnostic = DiagnosticCode.NONE
            return
        self._session_ref = None
        self._challenge_ref = outcome.challenge_ref
        self._last_verification_send_ns = None
        self._state = AccountLinkState.TWO_FACTOR_REQUIRED
        self._diagnostic = DiagnosticCode.NONE

    def _apply_provider_failure(
        self,
        exc: ProviderAuthFailure,
        *,
        preserve_challenge: bool = False,
    ) -> None:
        self._diagnostic = exc.code
        if exc.code is DiagnosticCode.PROVIDER_UNAVAILABLE:
            self._state = AccountLinkState.PROVIDER_UNAVAILABLE
        elif exc.code is DiagnosticCode.SESSION_EXPIRED:
            self._state = AccountLinkState.SESSION_EXPIRED
        else:
            self._state = AccountLinkState.AUTH_ERROR
        if not preserve_challenge:
            self._challenge_ref = None
            self._last_verification_send_ns = None
        self._session_ref = None

    def _clear_auth_context(self, *, cancel_challenge: bool) -> None:
        challenge_ref = self._challenge_ref
        self._challenge_ref = None
        self._session_ref = None
        self._last_verification_send_ns = None
        if cancel_challenge and _opaque_ref(challenge_ref):
            try:
                self._cancel_challenge(challenge_ref=challenge_ref)
            except Exception:
                pass

    def _require_credential_lifecycle_resolved(self) -> None:
        if self._credential_lifecycle_uncertain:
            raise AccountLinkStateError(
                "credential lifecycle requires secret-store reconciliation"
            )

    def _read_clock(self) -> int:
        try:
            value = self._clock_call()
        except Exception as exc:
            raise AccountLinkStateError("monotonic clock unavailable") from exc
        if type(value) is not int or value < 0:
            raise AccountLinkStateError("monotonic clock returned invalid value")
        return value

    def _resend_remaining_ns(self, now: int) -> int:
        prior = self._last_verification_send_ns
        if prior is None:
            return 0
        if now < prior:
            return self._resend_interval_ns
        elapsed = now - prior
        return max(0, self._resend_interval_ns - elapsed)
