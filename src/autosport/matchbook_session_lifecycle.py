from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Final

MATCHBOOK_LOGIN_DOC_URL: Final = "https://developers.matchbook.com/reference/login"
MATCHBOOK_GET_SESSION_DOC_URL: Final = "https://developers.matchbook.com/reference/get-session"
MATCHBOOK_APPROX_SESSION_LIFETIME_SECONDS: Final = 6 * 60 * 60


class SessionLifecycleError(RuntimeError):
    pass


class SessionClockRollbackError(SessionLifecycleError):
    pass


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


@dataclass(frozen=True, slots=True)
class SessionReadGenerationTicket:
    ticket_id: int
    generation_id: str
    issued_monotonic_ns: int

    def __post_init__(self) -> None:
        if type(self.ticket_id) is not int or self.ticket_id < 1:
            raise ValueError("ticket_id must be a positive non-boolean int")
        _validate_generation_id(self.generation_id)
        _validate_monotonic_ns(self.issued_monotonic_ns, "issued_monotonic_ns")


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
    def __init__(self) -> None:
        self._lock = RLock()
        self._generation_id: str | None = None
        self._state = SessionState.COLD
        self._login_monotonic_ns: int | None = None
        self._last_observation_monotonic_ns: int | None = None
        self._last_http_status: int | None = None
        self._clock_faulted = False
        self._restart_requires_reauth = False
        self._terminal_generations: set[str] = set()
        self._next_read_ticket_id = 1
        self._issued_read_tickets: dict[int, SessionReadGenerationTicket] = {}

    @classmethod
    def from_audit_snapshot(
        cls, snapshot: SessionAuditSnapshot
    ) -> MatchbookSessionLifecycle:
        if not isinstance(snapshot, SessionAuditSnapshot):
            raise TypeError("snapshot must be SessionAuditSnapshot")
        lifecycle = cls()
        lifecycle._generation_id = snapshot.generation_id
        lifecycle._last_http_status = snapshot.last_http_status
        lifecycle._login_monotonic_ns = None
        lifecycle._last_observation_monotonic_ns = None
        lifecycle._clock_faulted = False
        if snapshot.generation_id is not None:
            lifecycle._terminal_generations.add(snapshot.generation_id)
            lifecycle._state = SessionState.RESTART_REAUTH_REQUIRED
            lifecycle._restart_requires_reauth = True
        elif snapshot.clock_faulted:
            lifecycle._state = SessionState.RESTART_REAUTH_REQUIRED
            lifecycle._restart_requires_reauth = True
        return lifecycle

    @property
    def generation_id(self) -> str | None:
        with self._lock:
            return self._generation_id

    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._state is SessionState.ACTIVE and not self._clock_faulted

    @property
    def restart_requires_reauth(self) -> bool:
        with self._lock:
            return self._restart_requires_reauth

    def record_login_200(self, *, generation_id: str, monotonic_ns: int) -> None:
        generation_id = _validate_generation_id(generation_id)
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        with self._lock:
            self._observe_clock(monotonic_ns)
            if generation_id in self._terminal_generations:
                raise SessionLifecycleError(
                    "login success must create a fresh local session generation"
                )
            if self._generation_id is not None and generation_id == self._generation_id:
                raise SessionLifecycleError(
                    "login success must not reuse the current session generation"
                )
            self._invalidate_read_tickets()
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
        with self._lock:
            self._require_current_runtime_generation(generation_id)
            self._observe_clock(monotonic_ns)
            self._last_observation_monotonic_ns = monotonic_ns
            self._last_http_status = http_status
            if http_status == 200:
                self._state = SessionState.ACTIVE
            elif http_status == 401:
                self._invalidate_read_tickets()
                self._state = SessionState.EXPIRED
                self._terminal_generations.add(generation_id)
            else:
                self._invalidate_read_tickets()
                self._state = SessionState.UNKNOWN

    def record_network_failure(
        self, *, generation_id: str, monotonic_ns: int
    ) -> None:
        generation_id = _validate_generation_id(generation_id)
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        with self._lock:
            self._require_current_runtime_generation(generation_id)
            self._observe_clock(monotonic_ns)
            self._invalidate_read_tickets()
            self._last_observation_monotonic_ns = monotonic_ns
            self._last_http_status = None
            self._state = SessionState.UNKNOWN

    def record_logout_200(
        self, *, generation_id: str, monotonic_ns: int
    ) -> None:
        generation_id = _validate_generation_id(generation_id)
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        with self._lock:
            self._require_current_runtime_generation(generation_id)
            self._observe_clock(monotonic_ns)
            self._invalidate_read_tickets()
            self._last_observation_monotonic_ns = monotonic_ns
            self._last_http_status = 200
            self._state = SessionState.EXPIRED
            self._terminal_generations.add(generation_id)

    def capture_read_generation(
        self, *, monotonic_ns: int
    ) -> SessionReadGenerationTicket:
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        with self._lock:
            if self._generation_id is None or not self.is_active:
                raise SessionLifecycleError(
                    "authenticated read requires an active session generation"
                )
            self._require_current_runtime_generation(self._generation_id)
            self._observe_clock(monotonic_ns)
            ticket = SessionReadGenerationTicket(
                self._next_read_ticket_id,
                self._generation_id,
                monotonic_ns,
            )
            self._next_read_ticket_id += 1
            self._issued_read_tickets[ticket.ticket_id] = ticket
            return ticket

    def authorize_read_response_commit(
        self,
        ticket: SessionReadGenerationTicket,
        *,
        monotonic_ns: int,
    ) -> str:
        if type(ticket) is not SessionReadGenerationTicket:
            raise TypeError("ticket must be exact SessionReadGenerationTicket")
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        with self._lock:
            if self._issued_read_tickets.get(ticket.ticket_id) is not ticket:
                raise SessionLifecycleError(
                    "read generation ticket was not issued here or is no longer valid"
                )
            if self._generation_id != ticket.generation_id:
                self._issued_read_tickets.pop(ticket.ticket_id, None)
                raise SessionLifecycleError(
                    "read response belongs to a stale session generation"
                )
            if not self.is_active:
                self._issued_read_tickets.pop(ticket.ticket_id, None)
                raise SessionLifecycleError(
                    "positive read response requires a still-active session generation"
                )
            self._require_current_runtime_generation(ticket.generation_id)
            self._observe_clock(monotonic_ns)
            self._issued_read_tickets.pop(ticket.ticket_id, None)
            return ticket.generation_id

    def session_age_hint_seconds(self, *, monotonic_ns: int) -> float | None:
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        with self._lock:
            self._observe_clock(monotonic_ns)
            if self._login_monotonic_ns is None:
                return None
            return (monotonic_ns - self._login_monotonic_ns) / 1_000_000_000

    def approximate_refresh_due(self, *, monotonic_ns: int) -> bool:
        age = self.session_age_hint_seconds(monotonic_ns=monotonic_ns)
        return age is not None and age >= MATCHBOOK_APPROX_SESSION_LIFETIME_SECONDS

    def retry_disposition_after_401(
        self, *, request_kind: RequestKind
    ) -> RetryDisposition:
        if not isinstance(request_kind, RequestKind):
            raise TypeError("request_kind must be RequestKind")
        if request_kind is RequestKind.READ:
            return RetryDisposition.REAUTH_THEN_SINGLE_READ_RETRY
        return RetryDisposition.RECONCILE_EXTERNAL_EFFECT_BEFORE_ANY_WRITE_RETRY

    def audit_snapshot(self) -> SessionAuditSnapshot:
        with self._lock:
            return SessionAuditSnapshot(
                self._generation_id,
                self._state,
                self._login_monotonic_ns,
                self._last_observation_monotonic_ns,
                self._last_http_status,
                self._restart_requires_reauth,
                self._clock_faulted,
            )

    def _require_current_runtime_generation(self, generation_id: str) -> None:
        if self._generation_id is None or generation_id != self._generation_id:
            raise SessionLifecycleError("stale or unknown session generation evidence")
        if self._state is SessionState.EXPIRED:
            raise SessionLifecycleError(
                "terminal session generation evidence cannot mutate lifecycle"
            )
        if self._restart_requires_reauth:
            raise SessionLifecycleError(
                "restart requires a fresh login generation before provider validation"
            )
        if self._clock_faulted:
            raise SessionClockRollbackError(
                "session lifecycle is permanently fail-closed after clock rollback"
            )

    def _invalidate_read_tickets(self) -> None:
        self._issued_read_tickets.clear()

    def _observe_clock(self, monotonic_ns: int) -> None:
        if self._clock_faulted:
            raise SessionClockRollbackError(
                "session lifecycle is permanently fail-closed after clock rollback"
            )
        previous = self._last_observation_monotonic_ns
        if previous is not None and monotonic_ns < previous:
            self._invalidate_read_tickets()
            self._clock_faulted = True
            self._state = SessionState.CLOCK_FAULT
            self._restart_requires_reauth = True
            if self._generation_id is not None:
                self._terminal_generations.add(self._generation_id)
            raise SessionClockRollbackError("monotonic clock rollback detected")