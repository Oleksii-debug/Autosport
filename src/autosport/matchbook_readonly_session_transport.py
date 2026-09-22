from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Condition, RLock

from .matchbook_session_lifecycle import (
    MatchbookSessionLifecycle,
    RequestKind,
    RetryDisposition,
    SessionClockRollbackError,
    SessionLifecycleError,
    SessionReadGenerationTicket,
)
from .providers import ProviderUnavailableError

MATCHBOOK_READ_ONLY_PATHS = frozenset(
    {
        "/bpapi/rest/security/session",
        "/edge/rest/lookups/sports",
        "/edge/rest/events",
        "/edge/rest/account/balance",
        "/edge/rest/account/positions",
        "/edge/rest/reports/v1/transactions",
        "/edge/rest/reports/v2/bets/current",
        "/edge/rest/reports/v2/bets/settled",
        "/edge/rest/v2/offers",
    }
)


class MatchbookSessionTransportError(ProviderUnavailableError):
    """Secret-safe failure at the shared authenticated read transport boundary."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = (
            None if status_code is None else _validate_status(status_code)
        )
        self.retry_after_seconds = _validate_retry_after_seconds(
            retry_after_seconds
        )


class MatchbookAuthenticationUnavailable(MatchbookSessionTransportError):
    pass


class MatchbookReadUnavailable(MatchbookSessionTransportError):
    pass


class MatchbookReadForbidden(MatchbookSessionTransportError):
    pass


class MatchbookStaleGenerationResponse(SessionLifecycleError):
    pass


@dataclass(frozen=True, slots=True)
class MatchbookLoginResponse:
    status_code: int
    session_token: str | None = field(default=None, repr=False)
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        _validate_status(self.status_code)
        _validate_retry_after_seconds(self.retry_after_seconds)
        if self.status_code == 200:
            _validate_session_token(self.session_token)
        elif self.session_token is not None:
            raise ValueError("non-200 login response must not carry a session token")


@dataclass(frozen=True, slots=True)
class MatchbookReadResponse:
    status_code: int
    payload: object = field(default=None, repr=False)
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        _validate_status(self.status_code)
        _validate_retry_after_seconds(self.retry_after_seconds)


@dataclass(frozen=True, slots=True)
class MatchbookCommittedRead:
    status_code: int
    payload: object = field(repr=False)
    generation_id: str

    def __post_init__(self) -> None:
        _validate_status(self.status_code)
        if self.status_code < 200 or self.status_code >= 300:
            raise ValueError("committed Matchbook read must carry a 2xx status")
        if not isinstance(self.generation_id, str):
            raise TypeError("generation_id must be str")
        if (
            not self.generation_id
            or self.generation_id != self.generation_id.strip()
            or any(character.isspace() for character in self.generation_id)
        ):
            raise ValueError("generation_id must be non-empty, trimmed and whitespace-free")


@dataclass(frozen=True, slots=True, repr=False)
class _LiveSession:
    generation_id: str
    session_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _LoginFailure:
    kind: str
    status_code: int | None = None
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"provider", "lifecycle"}:
            raise ValueError("unknown login failure kind")
        if self.status_code is not None:
            _validate_status(self.status_code)
        _validate_retry_after_seconds(self.retry_after_seconds)
        if self.kind == "lifecycle" and (
            self.status_code is not None or self.retry_after_seconds is not None
        ):
            raise ValueError("lifecycle login failure cannot carry provider metadata")


QueryPairs = tuple[tuple[str, str], ...]
LoginCallable = Callable[[], MatchbookLoginResponse]
ReadCallable = Callable[[str, str, QueryPairs], MatchbookReadResponse]
LogoutCallable = Callable[[str], int]
ClockNs = Callable[[], int]
GenerationFactory = Callable[[], str]


def _validate_status(value: object) -> int:
    if type(value) is not int:
        raise TypeError("HTTP status must be a non-boolean int")
    if value < 100 or value > 599:
        raise ValueError("HTTP status must be between 100 and 599")
    return value


def _validate_retry_after_seconds(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("retry_after_seconds must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("retry_after_seconds must be a finite non-negative number")
    return result


def _validate_session_token(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("session token must be str")
    if not value or value != value.strip():
        raise ValueError("session token must be non-empty and trimmed")
    if any(character in value for character in ("\r", "\n", "\x00")):
        raise ValueError("session token contains forbidden control characters")
    if len(value) > 4096:
        raise ValueError("session token is too long")
    return value


def _validate_path(path: object) -> str:
    if not isinstance(path, str):
        raise TypeError("path must be str")
    if path not in MATCHBOOK_READ_ONLY_PATHS:
        raise ValueError("path is not in the Matchbook read-only allowlist")
    return path


def _validate_query_pairs(value: object) -> QueryPairs:
    if type(value) is not tuple:
        raise TypeError("query must be an exact tuple of string pairs")
    output: list[tuple[str, str]] = []
    forbidden_keys = {
        "session-token",
        "session_token",
        "username",
        "password",
        "mfa",
        "mfa-token",
        "mfa_token",
    }
    for pair in value:
        if type(pair) is not tuple or len(pair) != 2:
            raise TypeError("query entries must be exact two-item tuples")
        key, item = pair
        if not isinstance(key, str) or not key:
            raise TypeError("query keys must be non-empty strings")
        if not isinstance(item, str):
            raise TypeError("query values must be strings")
        if any(character in key or character in item for character in ("\r", "\n", "\x00")):
            raise ValueError("query contains forbidden control characters")
        if key.strip().lower() in forbidden_keys:
            raise ValueError("credentials must not be carried in Matchbook query parameters")
        output.append((key, item))
    return tuple(output)


def _default_generation_factory() -> str:
    return uuid.uuid4().hex


class MatchbookReadOnlySessionTransport:
    """
    Process-local Matchbook authenticated read session shared by market/account readers.

    The raw token never leaves the injected read/logout callbacks and is never serialized.
    Session-generation truth is delegated to MatchbookSessionLifecycle. This class owns only
    single-flight login/re-login and bounded read retry orchestration.
    """

    def __init__(
        self,
        *,
        lifecycle: MatchbookSessionLifecycle,
        login: LoginCallable,
        read: ReadCallable,
        clock_ns: ClockNs = time.monotonic_ns,
        generation_factory: GenerationFactory = _default_generation_factory,
        logout: LogoutCallable | None = None,
    ) -> None:
        if not isinstance(lifecycle, MatchbookSessionLifecycle):
            raise TypeError("lifecycle must be MatchbookSessionLifecycle")
        if not callable(login):
            raise TypeError("login must be callable")
        if not callable(read):
            raise TypeError("read must be callable")
        if not callable(clock_ns):
            raise TypeError("clock_ns must be callable")
        if not callable(generation_factory):
            raise TypeError("generation_factory must be callable")
        if logout is not None and not callable(logout):
            raise TypeError("logout must be callable or None")

        self._lifecycle = lifecycle
        self._login = login
        self._read = read
        self._clock_ns = clock_ns
        self._generation_factory = generation_factory
        self._logout = logout

        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._live_session: _LiveSession | None = None
        self._login_in_progress = False
        self._login_flight_id = 0
        self._login_waiters: dict[int, int] = {}
        self._failed_login_flights: dict[int, _LoginFailure] = {}

    @property
    def generation_id(self) -> str | None:
        with self._lock:
            if self._live_session is None:
                return None
            return self._live_session.generation_id

    def read(
        self,
        *,
        path: str,
        query: QueryPairs = (),
    ) -> MatchbookCommittedRead:
        path = _validate_path(path)
        query = _validate_query_pairs(query)
        recovered_after_401 = False

        while True:
            session = self._ensure_active_session()
            request_ns = self._clock()
            try:
                ticket = self._lifecycle.capture_read_generation(
                    monotonic_ns=request_ns
                )
            except SessionClockRollbackError:
                raise
            except SessionLifecycleError:
                raise MatchbookAuthenticationUnavailable(
                    "Matchbook session is not available for authenticated read"
                ) from None

            if ticket.generation_id != session.generation_id:
                self._retire_ticket_without_provider_evidence(
                    ticket,
                    monotonic_ns=request_ns,
                )
                self._clear_matching_live_session(session.generation_id)
                raise MatchbookAuthenticationUnavailable(
                    "Matchbook session generation changed before read dispatch"
                )

            try:
                response = self._read(session.session_token, path, query)
            except Exception:
                self._invalidate_generation_after_network_failure(
                    session.generation_id
                )
                raise MatchbookReadUnavailable(
                    "Matchbook read transport failed"
                ) from None

            if not isinstance(response, MatchbookReadResponse):
                self._invalidate_generation_after_network_failure(
                    session.generation_id
                )
                raise MatchbookReadUnavailable(
                    "Matchbook read transport returned an invalid response type"
                )

            status = response.status_code
            if 200 <= status < 300:
                committed_generation = self._authorize_response_ticket(ticket)
                return MatchbookCommittedRead(
                    status_code=response.status_code,
                    payload=response.payload,
                    generation_id=committed_generation,
                )

            if status == 401:
                self._invalidate_generation_after_401(session.generation_id)
                disposition = self._lifecycle.retry_disposition_after_401(
                    request_kind=RequestKind.READ
                )
                if (
                    disposition
                    is not RetryDisposition.REAUTH_THEN_SINGLE_READ_RETRY
                ):
                    raise MatchbookAuthenticationUnavailable(
                        "Matchbook lifecycle does not permit read re-authentication"
                    )
                if recovered_after_401:
                    raise MatchbookAuthenticationUnavailable(
                        "Matchbook authentication remained unavailable after one re-login"
                    )
                recovered_after_401 = True
                self._ensure_active_session()
                continue

            # A non-positive HTTP response still consumes its exact generation ticket.
            # Retiring it here prevents ticket accumulation while preserving the rule
            # that no positive provider payload can survive a generation transition.
            self._authorize_response_ticket(ticket)
            if status == 403:
                raise MatchbookReadForbidden(
                    "Matchbook endpoint is forbidden for the authenticated account",
                    status_code=403,
                    retry_after_seconds=response.retry_after_seconds,
                )

            raise MatchbookReadUnavailable(
                f"Matchbook read unavailable with HTTP {status}",
                status_code=status,
                retry_after_seconds=response.retry_after_seconds,
            )

    def logout(self) -> None:
        if self._logout is None:
            raise ValueError("Matchbook logout transport is not configured")

        with self._lock:
            session = self._live_session
        if session is None:
            return

        try:
            status = _validate_status(self._logout(session.session_token))
        except MatchbookSessionTransportError:
            raise
        except Exception:
            raise MatchbookSessionTransportError(
                "Matchbook logout transport failed"
            ) from None

        if status != 200:
            raise MatchbookSessionTransportError(
                f"Matchbook logout unavailable with HTTP {status}",
                status_code=status,
            )

        with self._condition:
            current = self._live_session
            if current is None or current.generation_id != session.generation_id:
                return
            try:
                logout_ns = self._clock()
                self._lifecycle.record_logout_200(
                    generation_id=session.generation_id,
                    monotonic_ns=logout_ns,
                )
            except SessionLifecycleError:
                # Provider already confirmed logout. Never retain local token authority
                # merely because the local audit/lifecycle commit could not complete.
                self._live_session = None
                self._condition.notify_all()
                raise
            self._live_session = None
            self._condition.notify_all()

    def _authorize_response_ticket(
        self, ticket: SessionReadGenerationTicket
    ) -> str:
        try:
            commit_ns = self._clock()
        except SessionLifecycleError:
            self._clear_matching_live_session(ticket.generation_id)
            raise
        if commit_ns < ticket.issued_monotonic_ns:
            self._clear_matching_live_session(ticket.generation_id)
            raise SessionLifecycleError(
                "Matchbook read response clock regressed before generation commit"
            )
        try:
            return self._lifecycle.authorize_read_response_commit(
                ticket,
                monotonic_ns=commit_ns,
            )
        except SessionLifecycleError:
            raise MatchbookStaleGenerationResponse(
                "Matchbook response belongs to a stale session generation"
            ) from None

    def _clear_matching_live_session(self, generation_id: str) -> None:
        with self._condition:
            current = self._live_session
            if current is not None and current.generation_id == generation_id:
                self._live_session = None
                self._condition.notify_all()

    def _retire_ticket_without_provider_evidence(
        self,
        ticket: SessionReadGenerationTicket,
        *,
        monotonic_ns: int,
    ) -> None:
        try:
            self._lifecycle.authorize_read_response_commit(
                ticket,
                monotonic_ns=monotonic_ns,
            )
        except SessionLifecycleError:
            # The ticket may already have been invalidated by the concurrent
            # generation transition. Either way no provider request was sent.
            pass

    def _ensure_active_session(self) -> _LiveSession:
        while True:
            with self._condition:
                current = self._live_session
                if (
                    current is not None
                    and self._lifecycle.is_active
                    and self._lifecycle.generation_id == current.generation_id
                ):
                    return current

                if self._login_in_progress:
                    flight_id = self._login_flight_id
                    self._login_waiters[flight_id] = (
                        self._login_waiters.get(flight_id, 0) + 1
                    )
                    try:
                        while (
                            self._login_in_progress
                            and self._login_flight_id == flight_id
                        ):
                            self._condition.wait()
                        failure = self._failed_login_flights.get(flight_id)
                    finally:
                        remaining = self._login_waiters[flight_id] - 1
                        if remaining:
                            self._login_waiters[flight_id] = remaining
                        else:
                            self._login_waiters.pop(flight_id, None)
                            self._failed_login_flights.pop(flight_id, None)
                    if failure is not None and failure.kind == "provider":
                        raise MatchbookAuthenticationUnavailable(
                            "Matchbook login failed for the shared authentication flight",
                            status_code=failure.status_code,
                            retry_after_seconds=failure.retry_after_seconds,
                        )
                    if failure is not None and failure.kind == "lifecycle":
                        raise SessionLifecycleError(
                            "Matchbook shared login failed at local session lifecycle"
                        )
                    continue

                self._login_in_progress = True
                self._login_flight_id += 1
                flight_id = self._login_flight_id
                break

        try:
            response = self._login()
        except Exception:
            self._finish_login_failure(
                flight_id,
                failure=_LoginFailure("provider"),
            )
            raise MatchbookAuthenticationUnavailable(
                "Matchbook login transport failed"
            ) from None

        if not isinstance(response, MatchbookLoginResponse):
            self._finish_login_failure(
                flight_id,
                failure=_LoginFailure("provider"),
            )
            raise MatchbookAuthenticationUnavailable(
                "Matchbook login transport returned an invalid response type"
            )

        if response.status_code != 200:
            failure = _LoginFailure(
                "provider",
                status_code=response.status_code,
                retry_after_seconds=response.retry_after_seconds,
            )
            self._finish_login_failure(flight_id, failure=failure)
            raise MatchbookAuthenticationUnavailable(
                f"Matchbook login unavailable with HTTP {response.status_code}",
                status_code=response.status_code,
                retry_after_seconds=response.retry_after_seconds,
            )

        token = response.session_token
        assert token is not None
        try:
            generation_id = self._generation_factory()
            if not isinstance(generation_id, str):
                raise SessionLifecycleError(
                    "session generation factory must return str"
                )
            if generation_id == token:
                raise SessionLifecycleError(
                    "session generation identity must be independent from raw token"
                )
            login_ns = self._clock()
            self._lifecycle.record_login_200(
                generation_id=generation_id,
                monotonic_ns=login_ns,
            )
        except SessionLifecycleError:
            self._finish_login_failure(
                flight_id,
                failure=_LoginFailure("lifecycle"),
            )
            raise
        except Exception:
            self._finish_login_failure(
                flight_id,
                failure=_LoginFailure("lifecycle"),
            )
            raise SessionLifecycleError(
                "Matchbook session generation factory failed"
            ) from None

        live = _LiveSession(generation_id=generation_id, session_token=token)
        with self._condition:
            self._live_session = live
            self._login_in_progress = False
            self._condition.notify_all()
            return live

    def _finish_login_failure(
        self,
        flight_id: int,
        *,
        failure: _LoginFailure,
    ) -> None:
        if not isinstance(failure, _LoginFailure):
            raise TypeError("failure must be _LoginFailure")
        with self._condition:
            if self._login_flight_id != flight_id:
                raise AssertionError("login flight identity changed unexpectedly")
            self._live_session = None
            self._login_in_progress = False
            if self._login_waiters.get(flight_id, 0):
                self._failed_login_flights[flight_id] = failure
            self._condition.notify_all()

    def _invalidate_generation_after_401(self, generation_id: str) -> None:
        with self._condition:
            current = self._live_session
            if current is None or current.generation_id != generation_id:
                return
            try:
                observed_ns = self._clock()
            except SessionLifecycleError:
                self._live_session = None
                self._condition.notify_all()
                raise
            try:
                self._lifecycle.record_get_session_result(
                    generation_id=generation_id,
                    http_status=401,
                    monotonic_ns=observed_ns,
                )
            except SessionClockRollbackError:
                self._live_session = None
                self._condition.notify_all()
                raise
            except SessionLifecycleError:
                # A concurrent lifecycle transition may already have invalidated this
                # predecessor. Never let stale 401 evidence mutate a successor.
                pass
            self._live_session = None
            self._condition.notify_all()

    def _invalidate_generation_after_network_failure(
        self, generation_id: str
    ) -> None:
        with self._condition:
            current = self._live_session
            if current is None or current.generation_id != generation_id:
                return
            try:
                observed_ns = self._clock()
            except SessionLifecycleError:
                self._live_session = None
                self._condition.notify_all()
                raise
            try:
                self._lifecycle.record_network_failure(
                    generation_id=generation_id,
                    monotonic_ns=observed_ns,
                )
            except SessionClockRollbackError:
                self._live_session = None
                self._condition.notify_all()
                raise
            except SessionLifecycleError:
                # Clear local token authority regardless. A concurrent lifecycle
                # transition may already have invalidated this predecessor.
                pass
            self._live_session = None
            self._condition.notify_all()

    def _clock(self) -> int:
        try:
            value = self._clock_ns()
        except Exception:
            raise SessionLifecycleError("Matchbook session clock failed") from None
        if type(value) is not int or value < 0:
            raise SessionLifecycleError(
                "Matchbook session clock must return a non-negative integer"
            )
        return value
