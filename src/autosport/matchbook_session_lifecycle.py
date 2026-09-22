from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


MATCHBOOK_LOGIN_DOC_URL: Final = "https://developers.matchbook.com/reference/login"
MATCHBOOK_GET_SESSION_DOC_URL: Final = "https://developers.matchbook.com/reference/get-session"
MATCHBOOK_APPROX_SESSION_LIFETIME_SECONDS: Final = 6 * 60 * 60


class SessionLifecycleError(RuntimeError):
    """Base error for invalid Matchbook session-lifecycle evidence."""


class SessionClockRollbackError(SessionLifecycleError):
    """Raised when process monotonic time moves backwards."""


class SessionState(str, Enum):
    COLD = "COLD"
    ACTIVE = "ACTIVE"
    UNKNOWN = "UNKNOWN"
    EXPIRED = "EXPIRED"
    RESTART_REAUTH_REQUIRED = "RESTART_REAUTH_REQUIRED"
    CLOCK_FAULT = "CLOCK_FAULT"


class RequestKind(str, Enum):
    READ = "READ"
    WRITE = "WRITE"


class RetryDisposition(str, Enum):
    NONE = "NONE"
    REAUTH_THEN_SINGLE_READ_RETRY = "REAUTH_THEN_SINGLE_READ_RETRY"
    RECONCILE_EXTERNAL_EFFECT_BEFORE_ANY_WRITE_RETRY = (
        "RECONCILE_EXTERNAL_EFFECT_BEFORE_ANY_WRITE_RETRY"
    )


@dataclass(frozen=True, slots=True)
class SessionAuditSnapshot:
    generation_id: str | None
    state: SessionState
    login_monotonic_ns: int | None
    last_observation_monotonic_ns: int | None
    last_http_status: int | None
    restart_requires_reauth: bool
    clock_faulted: bool
    login_doc_url: str = MATCHBOOK_LOGIN_DOC_URL
    get_session_doc_url: str = MATCHBOOK_GET_SESSION_DOC_URL

    def __post_init__(self) -> None:
        if self.generation_id is not None:
            _validate_generation_id(self.generation_id)
        for name, value in (
            ("login_monotonic_ns", self.login_monotonic_ns),
            ("last_observation_monotonic_ns", self.last_observation_monotonic_ns),
        ):
            if value is not None:
                _validate_monotonic_ns(value, name)
        if self.last_http_status is not None:
            _validate_http_status(self.last_http_status)
        if type(self.restart_requires_reauth) is not bool:
            raise TypeError("restart_requires_reauth must be bool")
        if type(self.clock_faulted) is not bool:
            raise TypeError("clock_faulted must be bool")


def _validate_generation_id(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("generation_id must be str")
    if not value or value != value.strip():
        raise ValueError("generation_id must be non-empty and trimmed")
    if any(character.isspace() for character in value):
        raise ValueError("generation_id must not contain whitespace")
    if len(value) > 128:
        raise ValueError("generation_id is too long")
    return value


def _validate_monotonic_ns(value: object, name: str = "monotonic_ns") -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be a non-boolean int")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _validate_http_status(value: object) -> int:
    if type(value) is not int:
        raise TypeError("http_status must be a non-boolean int")
    if value < 100 or value > 599:
        raise ValueError("http_status must be between 100 and 599")
    return value


class MatchbookSessionLifecycle:
    """Fail-closed runtime authority for Matchbook session generations.

    This object deliberately owns no credentials or session token. It records only
    non-secret local generation identity plus provider-response evidence.

    Matchbook documents session lifetime as approximately six hours. That value is
    exposed only as a scheduling hint; it is not converted into exact expiry truth.
    """

    def __init__(self) -> None:
        self._generation_id: str | None = None
        self._state = SessionState.COLD
        self._login_monotonic_ns: int | None = None
        self._last_observation_monotonic_ns: int | None = None
        self._last_http_status: int | None = None
        self._clock_faulted = False
        self._restart_requires_reauth = False
        self._terminal_generations: set[str] = set()

    @classmethod
    def from_audit_snapshot(
        cls, snapshot: SessionAuditSnapshot
    ) -> MatchbookSessionLifecycle:
        if not isinstance(snapshot, SessionAuditSnapshot):
            raise TypeError("snapshot must be SessionAuditSnapshot")
        lifecycle = cls()
        lifecycle._generation_id = snapshot.generation_id
        lifecycle._login_monotonic_ns = snapshot.login_monotonic_ns
        lifecycle._last_observation_monotonic_ns = snapshot.last_observation_monotonic_ns
        lifecycle._last_http_status = snapshot.last_http_status
        lifecycle._clock_faulted = snapshot.clock_faulted
        if snapshot.generation_id is not None:
            lifecycle._terminal_generations.add(snapshot.generation_id)
        if snapshot.clock_faulted:
            lifecycle._state = SessionState.CLOCK_FAULT
            lifecycle._restart_requires_reauth = True
        elif snapshot.generation_id is None:
            lifecycle._state = SessionState.COLD
            lifecycle._restart_requires_reauth = False
        else:
            lifecycle._state = SessionState.RESTART_REAUTH_REQUIRED
            lifecycle._restart_requires_reauth = True
        return lifecycle

    @property
    def generation_id(self) -> str | None:
        return self._generation_id

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state is SessionState.ACTIVE and not self._clock_faulted

    @property
    def restart_requires_reauth(self) -> bool:
        return self._restart_requires_reauth

    def record_login_200(self, *, generation_id: str, monotonic_ns: int) -> None:
        generation_id = _validate_generation_id(generation_id)
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        self._observe_clock(monotonic_ns)
        if generation_id in self._terminal_generations:
            raise SessionLifecycleError(
                "login success must create a fresh local session generation"
            )
        if self._generation_id is not None and generation_id == self._generation_id:
            raise SessionLifecycleError(
                "login success must not reuse the current session generation"
            )
        if self._generation_id is not None:
            self._terminal_generations.add(self._generation_id)
        self._generation_id = generation_id
        self._state = SessionState.ACTIVE
        self._login_monotonic_ns = monotonic_ns
        self._last_observation_monotonic_ns = monotonic_ns
        self._last_http_status = 200
        self._restart_requires_reauth = False

    def record_get_session_result(
        self, *, generation_id: str, http_status: int, monotonic_ns: int
    ) -> None:
        generation_id = _validate_generation_id(generation_id)
        http_status = _validate_http_status(http_status)
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        self._observe_clock(monotonic_ns)
        self._require_current_runtime_generation(generation_id)

        if self._state in {SessionState.EXPIRED, SessionState.RESTART_REAUTH_REQUIRED}:
            raise SessionLifecycleError(
                "terminal or restarted generation cannot regain authority without fresh login"
            )

        self._last_observation_monotonic_ns = monotonic_ns
        self._last_http_status = http_status
        if http_status == 200:
            self._state = SessionState.ACTIVE
        elif http_status == 401:
            self._state = SessionState.EXPIRED
            self._terminal_generations.add(generation_id)
        else:
            self._state = SessionState.UNKNOWN

    def record_network_failure(
        self, *, generation_id: str, monotonic_ns: int
    ) -> None:
        generation_id = _validate_generation_id(generation_id)
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        self._observe_clock(monotonic_ns)
        self._require_current_runtime_generation(generation_id)
        if self._state in {SessionState.EXPIRED, SessionState.RESTART_REAUTH_REQUIRED}:
            return
        self._last_observation_monotonic_ns = monotonic_ns
        self._last_http_status = None
        self._state = SessionState.UNKNOWN

    def record_logout_200(
        self, *, generation_id: str, monotonic_ns: int
    ) -> None:
        generation_id = _validate_generation_id(generation_id)
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        self._observe_clock(monotonic_ns)
        self._require_current_runtime_generation(generation_id)
        self._last_observation_monotonic_ns = monotonic_ns
        self._last_http_status = 200
        self._state = SessionState.EXPIRED
        self._terminal_generations.add(generation_id)

    def session_age_hint_seconds(self, *, monotonic_ns: int) -> float | None:
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        self._observe_clock(monotonic_ns)
        if self._login_monotonic_ns is None:
            return None
        return (monotonic_ns - self._login_monotonic_ns) / 1_000_000_000

    def approximate_refresh_due(self, *, monotonic_ns: int) -> bool:
        age = self.session_age_hint_seconds(monotonic_ns=monotonic_ns)
        return (
            age is not None
            and age >= MATCHBOOK_APPROX_SESSION_LIFETIME_SECONDS
        )

    def retry_disposition_after_401(
        self, *, request_kind: RequestKind
    ) -> RetryDisposition:
        if not isinstance(request_kind, RequestKind):
            raise TypeError("request_kind must be RequestKind")
        if request_kind is RequestKind.READ:
            return RetryDisposition.REAUTH_THEN_SINGLE_READ_RETRY
        return RetryDisposition.RECONCILE_EXTERNAL_EFFECT_BEFORE_ANY_WRITE_RETRY

    def audit_snapshot(self) -> SessionAuditSnapshot:
        return SessionAuditSnapshot(
            generation_id=self._generation_id,
            state=self._state,
            login_monotonic_ns=self._login_monotonic_ns,
            last_observation_monotonic_ns=self._last_observation_monotonic_ns,
            last_http_status=self._last_http_status,
            restart_requires_reauth=self._restart_requires_reauth,
            clock_faulted=self._clock_faulted,
        )

    def _require_current_runtime_generation(self, generation_id: str) -> None:
        if self._generation_id is None or generation_id != self._generation_id:
            raise SessionLifecycleError("stale or unknown session generation evidence")
        if self._restart_requires_reauth:
            raise SessionLifecycleError(
                "restart requires a fresh login generation before provider validation"
            )
        if self._clock_faulted:
            raise SessionClockRollbackError(
                "session lifecycle is permanently fail-closed after clock rollback"
            )

    def _observe_clock(self, monotonic_ns: int) -> None:
        if self._clock_faulted:
            raise SessionClockRollbackError(
                "session lifecycle is permanently fail-closed after clock rollback"
            )
        previous = self._last_observation_monotonic_ns
        if previous is not None and monotonic_ns < previous:
            self._clock_faulted = True
            self._state = SessionState.CLOCK_FAULT
            self._restart_requires_reauth = True
            if self._generation_id is not None:
                self._terminal_generations.add(self._generation_id)
            raise SessionClockRollbackError("monotonic clock rollback detected")
