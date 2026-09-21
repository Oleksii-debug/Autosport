from __future__ import annotations

import json
from threading import Event, Thread

import pytest

from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport import betfair_stream_transport as stream


TEST_APP_KEY = "TEST_APP_KEY_NOT_A_REAL_CREDENTIAL"
TEST_SESSION = "TEST_SESSION_NOT_A_REAL_CREDENTIAL"


class _StaticSecretProvider:
    def __init__(self) -> None:
        self._lease = stream.BetfairStreamCredentialLease(
            account_id="account-1",
            app_identity_id="app-1",
            app_key_class="LIVE",
            session_epoch=1,
            credentials=BetfairSessionCredentials(
                application_key=TEST_APP_KEY,
                session_token=TEST_SESSION,
            ),
        )

    def get_session_lease(self) -> stream.BetfairStreamCredentialLease:
        return self._lease


class _HandshakeSocket:
    def __init__(self) -> None:
        self._chunks = [
            json.dumps(
                {"op": "connection", "connectionId": "connection-1"},
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\r\n",
            json.dumps(
                {
                    "op": "status",
                    "id": 1,
                    "statusCode": "SUCCESS",
                    "connectionId": "connection-1",
                    "connectionClosed": False,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\r\n",
        ]
        self.sent: list[bytes] = []
        self.closed = False

    def recv(self, _size: int) -> bytes:
        if self.closed:
            raise OSError("closed")
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def sendall(self, data: bytes) -> None:
        if self.closed:
            raise OSError("closed")
        self.sent.append(bytes(data))

    def settimeout(self, _value: float) -> None:
        return None

    def shutdown(self, _how: int) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True


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


def test_close_cancels_blocked_connect_and_prevents_late_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator close must fence an in-flight connect, not merely a published socket."""

    open_entered = Event()
    allow_open_to_return = Event()
    connect_done = Event()
    socket = _HandshakeSocket()
    outcome: dict[str, object] = {}

    def blocked_open(*args: object, **kwargs: object) -> _HandshakeSocket:
        open_entered.set()
        cancellation = next(
            (
                value
                for value in (*args, *kwargs.values())
                if hasattr(value, "is_set") and hasattr(value, "wait")
            ),
            None,
        )
        while not allow_open_to_return.wait(timeout=0.01):
            if cancellation is not None and cancellation.is_set():
                raise OSError("connect cancelled")
        return socket

    monkeypatch.setattr(stream, "_open_verified_tls_socket", blocked_open)
    transport = _transport()

    def run_connect() -> None:
        try:
            outcome["connection_id"] = transport.connect()
        except Exception as exc:  # expected for a cancellation-safe implementation
            outcome["error"] = exc
        finally:
            connect_done.set()

    worker = Thread(target=run_connect, name="betfair-connect-test", daemon=True)
    worker.start()
    assert open_entered.wait(timeout=1.0), "connect did not enter the network-open phase"

    transport.close()

    # A cancellation-safe transport must not leave operator STOP waiting for the full
    # configured network timeout. The cleanup release below prevents a broken
    # implementation from leaking the daemon worker into later tests.
    cancelled_promptly = connect_done.wait(timeout=0.25)
    allow_open_to_return.set()
    worker.join(timeout=1.0)

    assert cancelled_promptly, (
        "close()/STOP did not cancel the in-flight TCP/TLS connect promptly"
    )
    assert not worker.is_alive(), "connect worker remained live after test cleanup"
    assert "connection_id" not in outcome, (
        "an in-flight connect authenticated successfully after close()/STOP"
    )
    assert "error" in outcome, "cancelled connect did not terminate fail-closed"
    assert transport.is_authenticated is False
    assert transport.connection_id is None
