from __future__ import annotations

from threading import Event, Lock, Thread

import pytest

from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport import betfair_stream_transport as stream


class _StaticSecretProvider:
    def __init__(self) -> None:
        self._lease = stream.BetfairStreamCredentialLease(
            account_id="account-1",
            app_identity_id="app-1",
            app_key_class="LIVE",
            session_epoch=1,
            credentials=BetfairSessionCredentials(
                application_key="TEST_APP_KEY_NOT_A_REAL_CREDENTIAL",
                session_token="TEST_SESSION_NOT_A_REAL_CREDENTIAL",
            ),
        )

    def get_session_lease(self) -> stream.BetfairStreamCredentialLease:
        return self._lease


class _FailingSocket:
    def recv(self, _size: int) -> bytes:
        raise OSError("synthetic handshake failure")

    def sendall(self, _data: bytes) -> None:
        return None

    def settimeout(self, _value: float) -> None:
        return None

    def shutdown(self, _how: int) -> None:
        return None

    def close(self) -> None:
        return None


def _transport() -> stream.BetfairStreamTlsTransport:
    return stream.BetfairStreamTlsTransport(
        identity=stream.BetfairStreamSessionIdentity(
            account_id="account-1",
            app_identity_id="app-1",
            app_key_class="LIVE",
            session_epoch=1,
        ),
        secret_provider=_StaticSecretProvider(),
        timeout_seconds=0.1,
    )


def test_concurrent_connect_is_single_flight_at_network_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One transport instance must never launch parallel provider login attempts."""

    lock = Lock()
    release_open = Event()
    first_open_entered = Event()
    second_open_entered = Event()
    first_thread_started = Event()
    second_thread_started = Event()
    open_calls = 0

    def blocked_open(*_args: object, **_kwargs: object) -> _FailingSocket:
        nonlocal open_calls
        with lock:
            open_calls += 1
            call_number = open_calls
        if call_number == 1:
            first_open_entered.set()
        elif call_number == 2:
            second_open_entered.set()
        if not release_open.wait(timeout=2.0):
            raise TimeoutError("test opener was not released")
        return _FailingSocket()

    monkeypatch.setattr(stream, "_open_verified_tls_socket", blocked_open)
    transport = _transport()
    outcomes: list[object] = []

    def run_connect(started: Event) -> None:
        started.set()
        try:
            outcomes.append(transport.connect())
        except Exception as exc:
            outcomes.append(exc)

    first = Thread(
        target=run_connect,
        args=(first_thread_started,),
        name="betfair-connect-first",
        daemon=True,
    )
    second = Thread(
        target=run_connect,
        args=(second_thread_started,),
        name="betfair-connect-second",
        daemon=True,
    )

    first.start()
    assert first_thread_started.wait(timeout=1.0)
    assert first_open_entered.wait(timeout=1.0)

    second.start()
    assert second_thread_started.wait(timeout=1.0)

    parallel_second_open = second_open_entered.wait(timeout=0.75)

    release_open.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)

    assert parallel_second_open is False, (
        "concurrent connect() calls entered two simultaneous TCP/TLS opens; "
        "provider authentication must be single-flight per transport instance"
    )
    assert not first.is_alive()
    assert not second.is_alive()
    assert len(outcomes) == 2
