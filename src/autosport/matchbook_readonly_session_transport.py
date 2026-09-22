from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Condition, RLock

from .matchbook_session_lifecycle import (
    MatchbookSessionLifecycle,
    SessionLifecycleError,
)

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


class MatchbookSessionTransportError(RuntimeError):
    """Secret-safe failure at the shared authenticated read transport boundary."""


class MatchbookAuthenticationUnavailable(MatchbookSessionTransportError):
    pass


class MatchbookReadUnavailable(MatchbookSessionTransportError):
    pass


class MatchbookReadForbidden(MatchbookSessionTransportError):
    pass


class MatchbookStaleGenerationResponse(MatchbookSessionTransportError):
    pass


@dataclass(frozen=True, slots=True)
class MatchbookLoginResponse:
    status_code: int
    session_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_status(self.status_code)
        if self.status_code == 200:
            _validate_session_token(self.session_token)
        elif self.session_token is not None:
            raise ValueError("non-200 login response must not carry a session token")


@dataclass(frozen=True, slots=True)
class MatchbookReadResponse:
    status_code: int
    payload: object = None

    def __post_init__(self) -> None:
        _validate_status(self.status_code)


@dataclass(frozen=True, slots=True, repr=False)
class _LiveSession:
    generation_id: str
    session_token: str = field(repr=False)


LoginCallable = Callable[[], MatchbookLoginResponse]
ReadCallable = Callable[[str, str], MatchbookReadResponse]
LogoutCallable = Callable[[str], int]
ClockNs = Callable[[], int]
GenerationFactory = Callable[[], str]


def _validate_status(value: object) -> int:
    if type(value) is not int:
        raise TypeError("HTTP status must be a non-boolean int")
    if value < 100 or value > 599:
        raise ValueError("HTTP status must be between 100 and 599")
    return value


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


def _default_generation_factory() -> str:
    return uuid.uuid4().hex


class MatchbookReadOnlySessionTransport:
    """
    One process-local Matchbook authenticated read session shared by market/account readers.

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
        self._last_completed_login_flight_id = 0
        self._last_login_failure_status: int | None = None

    @property
    def generation_id(self) -> str | None:
        with self._lock:
            if self._live_session is None:
                return None
            return self._live_session.generation_id

    def read(self, *, path: str) -> MatchbookReadResponse:
        path = _validate_path(path)
        recovered_after_401 = False

        while True:
            session = self._ensure_active_session()
            issued_ns = self._clock()
            try:
                ticket = self._lifecycle.capture_read_generation(
                    monotonic_ns=issued_ns
                )
            except SessionLifecycleError as exc:
                raise MatchbookAuthenticationUnavailable(
                    "Matchbook session is not available for authenticated read"
                ) from None

            if ticket.generation_id != session.generation_id:
                raise MatchbookAuthenticationUnavailable(
                    "Matchbook session generation changed before read dispatch"
                )

            try:
                response = self._read(session.session_token, path)
            except Exception:
                raise MatchbookReadUnavailable(
                    "Matchbook read transport failed"
                ) from None

            if not isinstance(response, MatchbookReadResponse):
                raise MatchbookReadUnavailable(
                    "Matchbook read transport returned an invalid response type"
                )

            status = response.status_code
            if 200 <= status < 300:
                try:
                    self._lifecycle.authorize_read_response_commit(
                        ticket,
                        monotonic_ns=self._clock(),
                    )
                except SessionLifecycleError:
                    raise MatchbookStaleGenerationResponse(
                        "Matchbook response belongs to a stale session generation"
                    ) from None
                return response

            if status == 401:
                self._invalidate_generation_after_401(session.generation_id)
                if recovered_after_401:
                    raise MatchbookAuthenticationUnavailable(
                        "Matchbook authentication remained unavailable after one re-login"
                    )
                recovered_after_401 = True
                self._ensure_active_session()
                continue

            if status == 403:
                raise MatchbookReadForbidden(
                    "Matchbook endpoint is forbidden for the authenticated account"
                )

            raise MatchbookReadUnavailable(
                f"Matchbook read unavailable with HTTP {status}"
            )

    def logout(self) -> None:
        if self._logout is None:
            raise MatchbookSessionTransportError(
                "Matchbook logout transport is not configured"
            )

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
                f"Matchbook logout unavailable with HTTP {status}"
            )

        with self._condition:
            current = self._live_session
            if current is None or current.generation_id != session.generation_id:
                return
            try:
                self._lifecycle.record_logout_200(
                    generation_id=session.generation_id,
                    monotonic_ns=self._clock(),
                )
            except SessionLifecycleError:
                self._live_session = None
                self._condition.notify_all()
                raise MatchbookSessionTransportError(
                    "Matchbook logout could not be committed to session lifecycle"
                ) from None
            self._live_session = None
            self._condition.notify_all()

    def _ensure_active_session(self) -> _LiveSession:
        waited_for_flight: int | None = None

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
                    waited_for_flight = self._login_flight_id
                    self._condition.wait()
                    if (
                        waited_for_flight == self._last_completed_login_flight_id
                        and self._live_session is None
                        and self._last_login_failure_status is not None
                    ):
                        raise MatchbookAuthenticationUnavailable(
                            "Matchbook login failed for the shared authentication flight"
                        )
                    continue

                self._login_in_progress = True
                self._login_flight_id += 1
                flight_id = self._login_flight_id
                self._last_login_failure_status = None
                break

        try:
            response = self._login()
        except Exception:
            self._finish_login_failure(flight_id, status_code=0)
            raise MatchbookAuthenticationUnavailable(
                "Matchbook login transport failed"
            ) from None

        if not isinstance(response, MatchbookLoginResponse):
            self._finish_login_failure(flight_id, status_code=0)
            raise MatchbookAuthenticationUnavailable(
                "Matchbook login transport returned an invalid response type"
            )

        if response.status_code != 200:
            self._finish_login_failure(flight_id, status_code=response.status_code)
            raise MatchbookAuthenticationUnavailable(
                f"Matchbook login unavailable with HTTP {response.status_code}"
            )

        token = response.session_token
        assert token is not None
        try:
            generation_id = self._generation_factory()
            if not isinstance(generation_id, str):
                raise TypeError("generation factory must return str")
            self._lifecycle.record_login_200(
                generation_id=generation_id,
                monotonic_ns=self._clock(),
            )
        except Exception:
            self._finish_login_failure(flight_id, status_code=0)
            raise MatchbookAuthenticationUnavailable(
                "Matchbook login could not establish a fresh session generation"
            ) from None

        live = _LiveSession(generation_id=generation_id, session_token=token)
        with self._condition:
            self._live_session = live
            self._login_in_progress = False
            self._last_completed_login_flight_id = flight_id
            self._last_login_failure_status = None
            self._condition.notify_all()
            return live

    def _finish_login_failure(self, flight_id: int, *, status_code: int) -> None:
        with self._condition:
            if self._login_flight_id != flight_id:
                raise AssertionError("login flight identity changed unexpectedly")
            self._live_session = None
            self._login_in_progress = False
            self._last_completed_login_flight_id = flight_id
            self._last_login_failure_status = status_code
            self._condition.notify_all()

    def _invalidate_generation_after_401(self, generation_id: str) -> None:
        with self._condition:
            current = self._live_session
            if current is None or current.generation_id != generation_id:
                return
            try:
                self._lifecycle.record_get_session_result(
                    generation_id=generation_id,
                    http_status=401,
                    monotonic_ns=self._clock(),
                )
            except SessionLifecycleError:
                # A concurrent lifecycle transition may already have invalidated this
                # predecessor. Never let stale 401 evidence mutate a successor.
                pass
            self._live_session = None
            self._condition.notify_all()

    def _clock(self) -> int:
        try:
            value = self._clock_ns()
        except Exception:
            raise MatchbookSessionTransportError(
                "Matchbook session clock failed"
            ) from None
        if type(value) is not int or value < 0:
            raise MatchbookSessionTransportError(
                "Matchbook session clock must return a non-negative integer"
            )
        return value
