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


class ReauthRole(str, Enum):
    LEADER = "LEADER"
    FOLLOWER = "FOLLOWER"


class ReauthStatus(str, Enum):
    IN_FLIGHT = "IN_FLIGHT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


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


@dataclass(frozen=True, slots=True)
class SessionReauthClaim:
    epoch_id: int
    generation_id: str
    role: ReauthRole

    def __post_init__(self) -> None:
        if type(self.epoch_id) is not int or self.epoch_id < 1:
            raise ValueError("epoch_id must be a positive non-boolean int")
        _validate_generation_id(self.generation_id)
        if not isinstance(self.role, ReauthRole):
            raise TypeError("role must be ReauthRole")


@dataclass(frozen=True, slots=True)
class SessionReauthOutcome:
    epoch_id: int
    expired_generation_id: str
    status: ReauthStatus
    successor_generation_id: str | None

    def __post_init__(self) -> None:
        if type(self.epoch_id) is not int or self.epoch_id < 1:
            raise ValueError("epoch_id must be a positive non-boolean int")
        _validate_generation_id(self.expired_generation_id)
        if not isinstance(self.status, ReauthStatus):
            raise TypeError("status must be ReauthStatus")
        if self.successor_generation_id is not None:
            _validate_generation_id(self.successor_generation_id)
        if self.status is ReauthStatus.SUCCEEDED:
            if self.successor_generation_id is None:
                raise ValueError("successful reauth requires successor_generation_id")
        elif self.successor_generation_id is not None:
            raise ValueError(
                "only successful reauth may expose successor_generation_id"
            )


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
        self._next_reauth_epoch_id = 1
        self._reauth_leader_claim: SessionReauthClaim | None = None
        self._reauth_follower_claim: SessionReauthClaim | None = None
        self._reauth_outcome: SessionReauthOutcome | None = None
        self._reauth_eligible_401_tickets: dict[int, SessionReadGenerationTicket] = {}
        self._reauth_ticket_claims: dict[int, SessionReauthClaim] = {}

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
            if self._reauth_is_in_flight():
                raise SessionLifecycleError(
                    "active read reauth epoch must be completed by its exact leader"
                )
            self._observe_clock(monotonic_ns)
            if generation_id in self._terminal_generations:
                raise SessionLifecycleError(
                    "login success must create a fresh local session generation"
                )
            if self._generation_id is not None and generation_id == self._generation_id:
                raise SessionLifecycleError(
                    "login success must not reuse the current session generation"
                )
            self._clear_completed_reauth_epoch()
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
            # Compare request emission to session-state clock authority, but do not
            # make independent concurrent READ emission order a new global baseline.
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
            if monotonic_ns < ticket.issued_monotonic_ns:
                self._issued_read_tickets.pop(ticket.ticket_id, None)
                raise SessionLifecycleError(
                    "read response commit precedes its generation ticket"
                )
            # Response ordering across independent concurrent reads is not session
            # generation ordering. Do not move the global session clock baseline.
            self._issued_read_tickets.pop(ticket.ticket_id, None)
            return ticket.generation_id

    def claim_read_reauth_after_401(
        self, ticket: SessionReadGenerationTicket, *, monotonic_ns: int
    ) -> SessionReauthClaim:
        if type(ticket) is not SessionReadGenerationTicket:
            raise TypeError("ticket must be exact SessionReadGenerationTicket")
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        with self._lock:
            if monotonic_ns < ticket.issued_monotonic_ns:
                raise SessionLifecycleError(
                    "401 observation precedes its product-issued read ticket"
                )

            if self._reauth_outcome is not None:
                outcome = self._reauth_outcome
                if ticket.generation_id == outcome.expired_generation_id:
                    if self._reauth_eligible_401_tickets.get(ticket.ticket_id) is not ticket:
                        raise SessionLifecycleError(
                            "401 read ticket was not issued here or was not in flight at epoch start"
                        )
                    existing = self._reauth_ticket_claims.get(ticket.ticket_id)
                    if existing is not None:
                        return existing
                    assert self._reauth_follower_claim is not None
                    self._reauth_ticket_claims[ticket.ticket_id] = self._reauth_follower_claim
                    return self._reauth_follower_claim
                if self._reauth_is_in_flight():
                    raise SessionLifecycleError(
                        "401 generation does not match active read reauth epoch"
                    )

            self._clear_completed_reauth_epoch()
            if self._issued_read_tickets.get(ticket.ticket_id) is not ticket:
                raise SessionLifecycleError(
                    "401 read ticket was not issued here or is no longer valid"
                )
            generation_id = ticket.generation_id
            if self._generation_id != generation_id:
                raise SessionLifecycleError("stale or unknown session generation evidence")
            if self._state is not SessionState.ACTIVE or self._clock_faulted:
                raise SessionLifecycleError(
                    "read 401 reauth requires an active session generation"
                )
            self._require_current_runtime_generation(generation_id)
            self._observe_clock(monotonic_ns)
            self._reauth_eligible_401_tickets = dict(self._issued_read_tickets)
            self._invalidate_read_tickets()
            self._last_observation_monotonic_ns = monotonic_ns
            self._last_http_status = 401
            self._state = SessionState.EXPIRED
            self._terminal_generations.add(generation_id)

            epoch_id = self._next_reauth_epoch_id
            self._next_reauth_epoch_id += 1
            self._reauth_leader_claim = SessionReauthClaim(
                epoch_id, generation_id, ReauthRole.LEADER
            )
            self._reauth_follower_claim = SessionReauthClaim(
                epoch_id, generation_id, ReauthRole.FOLLOWER
            )
            self._reauth_ticket_claims = {ticket.ticket_id: self._reauth_leader_claim}
            self._reauth_outcome = SessionReauthOutcome(
                epoch_id,
                generation_id,
                ReauthStatus.IN_FLIGHT,
                None,
            )
            return self._reauth_leader_claim

    def complete_read_reauth_success(
        self,
        claim: SessionReauthClaim,
        *,
        successor_generation_id: str,
        monotonic_ns: int,
    ) -> None:
        successor_generation_id = _validate_generation_id(successor_generation_id)
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        with self._lock:
            outcome = self._require_exact_reauth_leader(claim)
            if outcome.status is not ReauthStatus.IN_FLIGHT:
                raise SessionLifecycleError("read reauth epoch is already complete")
            if successor_generation_id == outcome.expired_generation_id:
                raise SessionLifecycleError(
                    "reauth success must create a fresh successor generation"
                )
            if successor_generation_id in self._terminal_generations:
                raise SessionLifecycleError(
                    "reauth success cannot resurrect a terminal generation"
                )
            self._observe_clock(monotonic_ns)
            self._invalidate_read_tickets()
            self._generation_id = successor_generation_id
            self._state = SessionState.ACTIVE
            self._login_monotonic_ns = monotonic_ns
            self._last_observation_monotonic_ns = monotonic_ns
            self._last_http_status = 200
            self._restart_requires_reauth = False
            self._reauth_outcome = SessionReauthOutcome(
                outcome.epoch_id,
                outcome.expired_generation_id,
                ReauthStatus.SUCCEEDED,
                successor_generation_id,
            )

    def complete_read_reauth_failure(
        self,
        claim: SessionReauthClaim,
        *,
        monotonic_ns: int,
    ) -> None:
        monotonic_ns = _validate_monotonic_ns(monotonic_ns)
        with self._lock:
            outcome = self._require_exact_reauth_leader(claim)
            if outcome.status is not ReauthStatus.IN_FLIGHT:
                raise SessionLifecycleError("read reauth epoch is already complete")
            self._observe_clock(monotonic_ns)
            self._last_observation_monotonic_ns = monotonic_ns
            self._reauth_outcome = SessionReauthOutcome(
                outcome.epoch_id,
                outcome.expired_generation_id,
                ReauthStatus.FAILED,
                None,
            )

    def read_reauth_outcome(
        self, claim: SessionReauthClaim
    ) -> SessionReauthOutcome:
        if type(claim) is not SessionReauthClaim:
            raise TypeError("claim must be exact SessionReauthClaim")
        with self._lock:
            if claim is not self._reauth_leader_claim and claim is not self._reauth_follower_claim:
                raise SessionLifecycleError(
                    "reauth claim was not issued here or is no longer current"
                )
            assert self._reauth_outcome is not None
            return self._reauth_outcome

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

    def _require_exact_reauth_leader(
        self, claim: SessionReauthClaim
    ) -> SessionReauthOutcome:
        if type(claim) is not SessionReauthClaim:
            raise TypeError("claim must be exact SessionReauthClaim")
        if claim is not self._reauth_leader_claim:
            raise SessionLifecycleError(
                "only the exact product-issued reauth leader may complete the epoch"
            )
        if self._reauth_outcome is None:
            raise SessionLifecycleError("reauth epoch is no longer current")
        return self._reauth_outcome

    def _reauth_is_in_flight(self) -> bool:
        return (
            self._reauth_outcome is not None
            and self._reauth_outcome.status is ReauthStatus.IN_FLIGHT
        )

    def _clear_completed_reauth_epoch(self) -> None:
        if self._reauth_outcome is None or self._reauth_is_in_flight():
            return
        self._reauth_leader_claim = None
        self._reauth_follower_claim = None
        self._reauth_outcome = None
        self._reauth_eligible_401_tickets.clear()
        self._reauth_ticket_claims.clear()

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
            if self._reauth_is_in_flight():
                assert self._reauth_outcome is not None
                self._reauth_outcome = SessionReauthOutcome(
                    self._reauth_outcome.epoch_id,
                    self._reauth_outcome.expired_generation_id,
                    ReauthStatus.FAILED,
                    None,
                )
            self._clock_faulted = True
            self._state = SessionState.CLOCK_FAULT
            self._restart_requires_reauth = True
            if self._generation_id is not None:
                self._terminal_generations.add(self._generation_id)
            raise SessionClockRollbackError("monotonic clock rollback detected")
