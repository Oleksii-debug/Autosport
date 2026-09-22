from __future__ import annotations

from collections.abc import Callable
from threading import Barrier, Event, Lock, Thread

import pytest

from autosport.matchbook_readonly_session_transport import (
    MATCHBOOK_READ_ONLY_PATHS,
    MatchbookAuthenticationUnavailable,
    MatchbookLoginResponse,
    MatchbookReadForbidden,
    MatchbookReadOnlySessionTransport,
    MatchbookReadResponse,
    MatchbookReadUnavailable,
    MatchbookSessionTransportError,
    MatchbookStaleGenerationResponse,
)
from autosport.matchbook_session_lifecycle import (
    MatchbookSessionLifecycle,
    SessionState,
)


class TickClock:
    def __init__(self, start: int = 100) -> None:
        self._lock = Lock()
        self._value = start

    def __call__(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


class GenerationFactory:
    def __init__(self, start: int = 0) -> None:
        self._lock = Lock()
        self._value = start

    def __call__(self) -> str:
        with self._lock:
            self._value += 1
            return f"gen-{self._value}"


class LoginFactory:
    def __init__(self) -> None:
        self._lock = Lock()
        self.calls = 0

    def __call__(self) -> MatchbookLoginResponse:
        with self._lock:
            self.calls += 1
            call = self.calls
        return MatchbookLoginResponse(200, f"token-{call}")


def build_transport(
    *,
    read: Callable[[str, str], MatchbookReadResponse],
    login: Callable[[], MatchbookLoginResponse] | None = None,
    lifecycle: MatchbookSessionLifecycle | None = None,
    generation_factory: Callable[[], str] | None = None,
    logout: Callable[[str], int] | None = None,
) -> tuple[
    MatchbookReadOnlySessionTransport,
    MatchbookSessionLifecycle,
    TickClock,
    LoginFactory | None,
]:
    lifecycle = lifecycle or MatchbookSessionLifecycle()
    clock = TickClock()
    default_login: LoginFactory | None = None
    if login is None:
        default_login = LoginFactory()
        login = default_login
    transport = MatchbookReadOnlySessionTransport(
        lifecycle=lifecycle,
        login=login,
        read=read,
        clock_ns=clock,
        generation_factory=generation_factory or GenerationFactory(),
        logout=logout,
    )
    return transport, lifecycle, clock, default_login


def run_in_thread(
    function: Callable[[], object],
    *,
    results: list[object],
    errors: list[BaseException],
) -> Thread:
    def target() -> None:
        try:
            results.append(function())
        except BaseException as exc:  # pragma: no cover - asserted by caller
            errors.append(exc)

    thread = Thread(target=target)
    thread.start()
    return thread


def test_first_read_logs_in_once_and_commits_generation_bound_response() -> None:
    observed: list[tuple[str, str]] = []

    def read(token: str, path: str) -> MatchbookReadResponse:
        observed.append((token, path))
        return MatchbookReadResponse(200, {"ok": True})

    transport, lifecycle, _, login = build_transport(read=read)

    response = transport.read(path="/edge/rest/events")

    assert response.payload == {"ok": True}
    assert login is not None and login.calls == 1
    assert observed == [("token-1", "/edge/rest/events")]
    assert transport.generation_id == "gen-1"
    assert lifecycle.state is SessionState.ACTIVE
    assert lifecycle._issued_read_tickets == {}


def test_concurrent_cold_reads_share_one_login_flight() -> None:
    login_started = Event()
    release_login = Event()
    login_lock = Lock()
    login_calls = 0

    def login() -> MatchbookLoginResponse:
        nonlocal login_calls
        with login_lock:
            login_calls += 1
            call = login_calls
        login_started.set()
        assert release_login.wait(timeout=2.0)
        return MatchbookLoginResponse(200, f"token-{call}")

    def read(token: str, path: str) -> MatchbookReadResponse:
        return MatchbookReadResponse(200, token)

    transport, lifecycle, _, _ = build_transport(read=read, login=login)
    results: list[object] = []
    errors: list[BaseException] = []

    first = run_in_thread(
        lambda: transport.read(path="/edge/rest/events"),
        results=results,
        errors=errors,
    )
    assert login_started.wait(timeout=1.0)
    second = run_in_thread(
        lambda: transport.read(path="/edge/rest/account/balance"),
        results=results,
        errors=errors,
    )

    with transport._condition:
        assert transport._login_waiters.get(1) == 1
    release_login.set()

    first.join(timeout=2.0)
    second.join(timeout=2.0)
    assert first.is_alive() is False
    assert second.is_alive() is False
    assert errors == []
    assert login_calls == 1
    assert sorted(result.payload for result in results) == ["token-1", "token-1"]
    assert lifecycle.state is SessionState.ACTIVE


def test_two_concurrent_401s_trigger_exactly_one_successor_login() -> None:
    login = LoginFactory()
    arm_401 = Event()
    pair = Barrier(2)
    read_lock = Lock()
    old_generation_reads = 0

    def read(token: str, path: str) -> MatchbookReadResponse:
        nonlocal old_generation_reads
        if token == "token-1" and arm_401.is_set():
            with read_lock:
                old_generation_reads += 1
            pair.wait(timeout=2.0)
            return MatchbookReadResponse(401)
        return MatchbookReadResponse(200, token)

    transport, lifecycle, _, _ = build_transport(read=read, login=login)
    assert transport.read(path="/edge/rest/events").payload == "token-1"
    arm_401.set()

    results: list[object] = []
    errors: list[BaseException] = []
    first = run_in_thread(
        lambda: transport.read(path="/edge/rest/events"),
        results=results,
        errors=errors,
    )
    second = run_in_thread(
        lambda: transport.read(path="/edge/rest/account/positions"),
        results=results,
        errors=errors,
    )
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert errors == []
    assert old_generation_reads == 2
    assert login.calls == 2
    assert [result.payload for result in results].count("token-2") == 2
    assert transport.generation_id == "gen-2"
    assert lifecycle.state is SessionState.ACTIVE


def test_late_predecessor_200_cannot_be_relabelled_as_successor_truth() -> None:
    login = LoginFactory()
    armed = Event()
    late_started = Event()
    release_late = Event()
    successor_returned = Event()
    lock = Lock()
    old_calls = 0

    def read(token: str, path: str) -> MatchbookReadResponse:
        nonlocal old_calls
        if token == "token-1" and armed.is_set():
            with lock:
                old_calls += 1
                call = old_calls
            if call == 1:
                late_started.set()
                assert release_late.wait(timeout=2.0)
                return MatchbookReadResponse(200, "late-old")
            return MatchbookReadResponse(401)
        if token == "token-2":
            successor_returned.set()
            return MatchbookReadResponse(200, "successor")
        return MatchbookReadResponse(200, "warm")

    transport, lifecycle, _, _ = build_transport(read=read, login=login)
    assert transport.read(path="/edge/rest/events").payload == "warm"
    armed.set()

    late_results: list[object] = []
    late_errors: list[BaseException] = []
    late = run_in_thread(
        lambda: transport.read(path="/edge/rest/events"),
        results=late_results,
        errors=late_errors,
    )
    assert late_started.wait(timeout=1.0)

    successor_results: list[object] = []
    successor_errors: list[BaseException] = []
    successor = run_in_thread(
        lambda: transport.read(path="/edge/rest/account/positions"),
        results=successor_results,
        errors=successor_errors,
    )
    assert successor_returned.wait(timeout=2.0)
    successor.join(timeout=2.0)
    assert successor_errors == []
    assert successor_results[0].payload == "successor"

    release_late.set()
    late.join(timeout=2.0)

    assert late_results == []
    assert len(late_errors) == 1
    assert isinstance(late_errors[0], MatchbookStaleGenerationResponse)
    assert transport.generation_id == "gen-2"
    assert lifecycle.state is SessionState.ACTIVE
    assert login.calls == 2


def test_403_never_triggers_relogin_and_retires_ticket() -> None:
    login = LoginFactory()
    forbidden = True

    def read(token: str, path: str) -> MatchbookReadResponse:
        if forbidden:
            return MatchbookReadResponse(403)
        return MatchbookReadResponse(200, "allowed")

    transport, lifecycle, _, _ = build_transport(read=read, login=login)

    with pytest.raises(MatchbookReadForbidden):
        transport.read(path="/edge/rest/account/positions")

    assert login.calls == 1
    assert lifecycle.state is SessionState.ACTIVE
    assert lifecycle._issued_read_tickets == {}

    forbidden = False
    assert transport.read(path="/edge/rest/events").payload == "allowed"
    assert login.calls == 1


@pytest.mark.parametrize("status", [400, 404, 429, 500, 502, 503, 504])
def test_non_401_provider_failure_does_not_relogin_or_leak_ticket(status: int) -> None:
    login = LoginFactory()

    def read(token: str, path: str) -> MatchbookReadResponse:
        return MatchbookReadResponse(status)

    transport, lifecycle, _, _ = build_transport(read=read, login=login)

    with pytest.raises(MatchbookReadUnavailable, match=str(status)):
        transport.read(path="/edge/rest/events")

    assert login.calls == 1
    assert lifecycle.state is SessionState.ACTIVE
    assert lifecycle._issued_read_tickets == {}


def test_network_failure_is_typed_secret_safe_and_next_call_uses_fresh_generation() -> None:
    login = LoginFactory()
    fail = True

    def read(token: str, path: str) -> MatchbookReadResponse:
        if fail:
            raise RuntimeError(f"provider failed session-token={token}")
        return MatchbookReadResponse(200, token)

    transport, lifecycle, _, _ = build_transport(read=read, login=login)

    with pytest.raises(MatchbookReadUnavailable) as exc_info:
        transport.read(path="/edge/rest/events")

    assert "token-1" not in str(exc_info.value)
    assert lifecycle.state is SessionState.UNKNOWN
    assert transport.generation_id is None

    fail = False
    assert transport.read(path="/edge/rest/events").payload == "token-2"
    assert login.calls == 2
    assert transport.generation_id == "gen-2"


def test_persistent_401_stops_after_one_bounded_relogin() -> None:
    login = LoginFactory()

    def read(token: str, path: str) -> MatchbookReadResponse:
        return MatchbookReadResponse(401)

    transport, lifecycle, _, _ = build_transport(read=read, login=login)

    with pytest.raises(
        MatchbookAuthenticationUnavailable,
        match="after one re-login",
    ):
        transport.read(path="/edge/rest/events")

    assert login.calls == 2
    assert transport.generation_id is None
    assert lifecycle.state is SessionState.EXPIRED


def test_concurrent_failed_login_is_single_flight_and_all_waiters_fail() -> None:
    started = Event()
    release = Event()
    call_lock = Lock()
    calls = 0

    def login() -> MatchbookLoginResponse:
        nonlocal calls
        with call_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=2.0)
        return MatchbookLoginResponse(503)

    transport, _, _, _ = build_transport(
        login=login,
        read=lambda token, path: MatchbookReadResponse(200),
    )
    results: list[object] = []
    errors: list[BaseException] = []

    first = run_in_thread(
        lambda: transport.read(path="/edge/rest/events"),
        results=results,
        errors=errors,
    )
    assert started.wait(timeout=1.0)
    second = run_in_thread(
        lambda: transport.read(path="/edge/rest/account/balance"),
        results=results,
        errors=errors,
    )
    with transport._condition:
        assert transport._login_waiters.get(1) == 1

    release.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert results == []
    assert calls == 1
    assert len(errors) == 2
    assert all(isinstance(error, MatchbookAuthenticationUnavailable) for error in errors)


def test_login_exception_does_not_leak_secret_text() -> None:
    def login() -> MatchbookLoginResponse:
        raise RuntimeError("password=hunter2 session-token=top-secret")

    transport, _, _, _ = build_transport(
        login=login,
        read=lambda token, path: MatchbookReadResponse(200),
    )

    with pytest.raises(MatchbookAuthenticationUnavailable) as exc_info:
        transport.read(path="/edge/rest/events")

    rendered = str(exc_info.value)
    assert "hunter2" not in rendered
    assert "top-secret" not in rendered


def test_logout_invalidates_generation_and_next_read_reauthenticates() -> None:
    login = LoginFactory()
    logged_out: list[str] = []

    def read(token: str, path: str) -> MatchbookReadResponse:
        return MatchbookReadResponse(200, token)

    def logout(token: str) -> int:
        logged_out.append(token)
        return 200

    transport, lifecycle, _, _ = build_transport(
        read=read,
        login=login,
        logout=logout,
    )
    assert transport.read(path="/edge/rest/events").payload == "token-1"

    transport.logout()

    assert logged_out == ["token-1"]
    assert transport.generation_id is None
    assert lifecycle.state is SessionState.EXPIRED

    assert transport.read(path="/edge/rest/events").payload == "token-2"
    assert login.calls == 2
    assert transport.generation_id == "gen-2"


def test_restart_never_restores_raw_token_or_active_generation() -> None:
    first_login = LoginFactory()
    first, original_lifecycle, _, _ = build_transport(
        read=lambda token, path: MatchbookReadResponse(200, token),
        login=first_login,
    )
    assert first.read(path="/edge/rest/events").payload == "token-1"
    snapshot = original_lifecycle.audit_snapshot()

    restored_lifecycle = MatchbookSessionLifecycle.from_audit_snapshot(snapshot)
    restored_login = LoginFactory()
    restored, _, _, _ = build_transport(
        lifecycle=restored_lifecycle,
        read=lambda token, path: MatchbookReadResponse(200, token),
        login=restored_login,
        generation_factory=GenerationFactory(start=1),
    )

    assert restored.generation_id is None
    assert restored_lifecycle.state is SessionState.RESTART_REAUTH_REQUIRED
    assert restored.read(path="/edge/rest/events").payload == "token-1"
    assert restored_login.calls == 1
    assert restored.generation_id == "gen-2"
    assert restored_lifecycle.state is SessionState.ACTIVE


def test_login_response_repr_never_contains_session_token() -> None:
    response = MatchbookLoginResponse(200, "super-secret-token")

    rendered = repr(response)

    assert "super-secret-token" not in rendered
    assert "session_token" not in rendered


def test_non_200_login_response_cannot_carry_token() -> None:
    with pytest.raises(ValueError):
        MatchbookLoginResponse(403, "must-not-survive")


@pytest.mark.parametrize(
    "bad_path",
    [
        "/edge/rest/v2/offers/123",
        "/edge/rest/v2/offers?x=1",
        "/edge/rest/v2/offers/",
        "/edge/rest/betting/write",
        "https://api.matchbook.com/edge/rest/events",
    ],
)
def test_read_only_allowlist_fails_closed_before_authentication(bad_path: str) -> None:
    login = LoginFactory()
    transport, _, _, _ = build_transport(
        read=lambda token, path: MatchbookReadResponse(200),
        login=login,
    )

    with pytest.raises(ValueError, match="read-only allowlist"):
        transport.read(path=bad_path)

    assert login.calls == 0


def test_surface_exposes_no_betting_write_method() -> None:
    transport, _, _, _ = build_transport(
        read=lambda token, path: MatchbookReadResponse(200),
    )

    assert not hasattr(transport, "write")
    assert not hasattr(transport, "submit")
    assert not hasattr(transport, "cancel")
    assert "/edge/rest/events" in MATCHBOOK_READ_ONLY_PATHS


def test_unconfigured_logout_is_fail_closed_without_changing_session() -> None:
    transport, lifecycle, _, login = build_transport(
        read=lambda token, path: MatchbookReadResponse(200, token),
    )
    assert transport.read(path="/edge/rest/events").payload == "token-1"

    with pytest.raises(MatchbookSessionTransportError, match="not configured"):
        transport.logout()

    assert login is not None and login.calls == 1
    assert transport.generation_id == "gen-1"
    assert lifecycle.state is SessionState.ACTIVE


def test_response_commit_clock_failure_drops_token_authority_and_rotates_on_retry() -> None:
    login = LoginFactory()

    class FailThirdClock:
        def __init__(self) -> None:
            self.calls = 0
            self.fail = True

        def __call__(self) -> int:
            self.calls += 1
            if self.fail and self.calls == 3:
                raise RuntimeError("clock unavailable")
            return 100 + self.calls

    clock = FailThirdClock()
    lifecycle = MatchbookSessionLifecycle()
    transport = MatchbookReadOnlySessionTransport(
        lifecycle=lifecycle,
        login=login,
        read=lambda token, path: MatchbookReadResponse(200, token),
        clock_ns=clock,
        generation_factory=GenerationFactory(),
    )

    with pytest.raises(MatchbookSessionTransportError, match="clock failed"):
        transport.read(path="/edge/rest/events")

    assert transport.generation_id is None
    assert lifecycle.state is SessionState.ACTIVE
    assert len(lifecycle._issued_read_tickets) == 1

    clock.fail = False
    assert transport.read(path="/edge/rest/events").payload == "token-2"
    assert login.calls == 2
    assert transport.generation_id == "gen-2"
    assert lifecycle.state is SessionState.ACTIVE
    assert lifecycle._issued_read_tickets == {}


def test_confirmed_logout_clock_failure_still_clears_local_token_authority() -> None:
    login = LoginFactory()
    logged_out: list[str] = []

    class FailFourthClock:
        def __init__(self) -> None:
            self.calls = 0
            self.fail = True

        def __call__(self) -> int:
            self.calls += 1
            if self.fail and self.calls == 4:
                raise RuntimeError("clock unavailable during logout")
            return 200 + self.calls

    clock = FailFourthClock()
    lifecycle = MatchbookSessionLifecycle()
    transport = MatchbookReadOnlySessionTransport(
        lifecycle=lifecycle,
        login=login,
        read=lambda token, path: MatchbookReadResponse(200, token),
        logout=lambda token: logged_out.append(token) or 200,
        clock_ns=clock,
        generation_factory=GenerationFactory(),
    )
    assert transport.read(path="/edge/rest/events").payload == "token-1"

    with pytest.raises(
        MatchbookSessionTransportError,
        match="could not be committed",
    ):
        transport.logout()

    assert logged_out == ["token-1"]
    assert transport.generation_id is None
    assert lifecycle.state is SessionState.ACTIVE

    clock.fail = False
    assert transport.read(path="/edge/rest/events").payload == "token-2"
    assert login.calls == 2
    assert transport.generation_id == "gen-2"
    assert lifecycle.state is SessionState.ACTIVE


def test_logout_while_read_is_inflight_fences_late_predecessor_response() -> None:
    login = LoginFactory()
    read_started = Event()
    release_read = Event()

    def read(token: str, path: str) -> MatchbookReadResponse:
        if path == "/edge/rest/account/positions":
            read_started.set()
            assert release_read.wait(timeout=2.0)
            return MatchbookReadResponse(200, "late-predecessor")
        return MatchbookReadResponse(200, "warm")

    transport, lifecycle, _, _ = build_transport(
        read=read,
        login=login,
        logout=lambda token: 200,
    )
    assert transport.read(path="/edge/rest/events").payload == "warm"

    results: list[object] = []
    errors: list[BaseException] = []
    inflight = run_in_thread(
        lambda: transport.read(path="/edge/rest/account/positions"),
        results=results,
        errors=errors,
    )
    assert read_started.wait(timeout=1.0)

    transport.logout()
    assert transport.generation_id is None
    assert lifecycle.state is SessionState.EXPIRED

    release_read.set()
    inflight.join(timeout=2.0)

    assert inflight.is_alive() is False
    assert results == []
    assert len(errors) == 1
    assert isinstance(errors[0], MatchbookStaleGenerationResponse)
