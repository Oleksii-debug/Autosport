from __future__ import annotations

import json
import socket
from dataclasses import replace
from hashlib import sha256
from threading import Event, Thread

import pytest

from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport import betfair_stream_transport as stream


TEST_APP_KEY = "TEST_APP_KEY_NOT_A_REAL_CREDENTIAL"
TEST_SESSION = "Bearer TEST_SESSION_NOT_A_REAL_CREDENTIAL"


class FakeSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False
        self.shutdown_how: int | None = None
        self.timeout: float | None = None
        self.recv_calls: list[int] = []

    def recv(self, size: int) -> bytes:
        self.recv_calls.append(size)
        if not self.chunks:
            return b""
        head = self.chunks.pop(0)
        if len(head) <= size:
            return head
        self.chunks.insert(0, head[size:])
        return head[:size]

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def shutdown(self, how: int) -> None:
        self.shutdown_how = how

    def close(self) -> None:
        self.closed = True


class Secrets:
    def __init__(
        self,
        *,
        epoch: int = 1,
        key_class: str = "LIVE",
        application_key: str = TEST_APP_KEY,
        session: str = TEST_SESSION,
        error: Exception | None = None,
    ) -> None:
        self.error = error
        self.calls = 0
        self.lease = stream.BetfairStreamCredentialLease(
            account_id="acct-1",
            app_identity_id="betfair-app-production-1",
            app_key_class=key_class,
            session_epoch=epoch,
            credentials=BetfairSessionCredentials(
                application_key,
                session,
            ),
        )

    def get_session_lease(
        self,
    ) -> stream.BetfairStreamCredentialLease:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.lease


class Sink:
    def __init__(
        self,
        *,
        mismatch: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.frames: list[stream.BetfairStreamRawFrame] = []
        self.mismatch = mismatch
        self.error = error

    def persist(
        self,
        frame: stream.BetfairStreamRawFrame,
    ) -> stream.BetfairStreamPersistenceReceipt:
        self.frames.append(frame)
        if self.error is not None:
            raise self.error
        receipt = stream.BetfairStreamPersistenceReceipt.for_frame(
            frame
        )
        if self.mismatch == "digest":
            receipt = replace(
                receipt,
                payload_sha256="0" * 64,
            )
        elif self.mismatch == "sequence":
            receipt = replace(
                receipt,
                frame_sequence=receipt.frame_sequence + 1,
            )
        return receipt


def identity(
    *,
    epoch: int = 1,
    key_class: str = "LIVE",
) -> stream.BetfairStreamSessionIdentity:
    return stream.BetfairStreamSessionIdentity(
        account_id="acct-1",
        app_identity_id="betfair-app-production-1",
        app_key_class=key_class,
        session_epoch=epoch,
    )


def connection_line() -> bytes:
    return b'{"op":"connection","connectionId":"conn-1"}\r\n'


def status_line(*, ok: bool = True) -> bytes:
    if ok:
        return (
            b'{"op":"status","id":1,"statusCode":"SUCCESS",'
            b'"connectionClosed":false}\r\n'
        )
    return (
        b'{"op":"status","id":1,"statusCode":"FAILURE",'
        b'"errorMessage":"TEST_SESSION_NOT_A_REAL_CREDENTIAL"}\r\n'
    )


def connected_socket(*, tail: bytes = b"") -> FakeSocket:
    return FakeSocket(
        [
            connection_line(),
            status_line() + tail,
        ]
    )


def make_transport(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeSocket,
    *,
    secrets: Secrets | None = None,
    ident: stream.BetfairStreamSessionIdentity | None = None,
    max_frame_bytes: int = 4 * 1024 * 1024,
) -> stream.BetfairStreamTlsTransport:
    monkeypatch.setattr(
        stream,
        "_open_verified_tls_socket",
        lambda timeout, _cancel=None: fake,
    )
    return stream.BetfairStreamTlsTransport(
        identity=ident or identity(),
        secret_provider=secrets or Secrets(),
        max_frame_bytes=max_frame_bytes,
    )


def test_verified_tls_uses_canonical_endpoint_hostname_verification_and_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    raw = FakeSocket([])
    tls = FakeSocket([])

    def create_connection(address, timeout):
        captured["address"] = address
        captured["timeout"] = timeout
        return raw

    class Context:
        def wrap_socket(
            self,
            candidate,
            *,
            server_hostname,
        ):
            captured["candidate"] = candidate
            captured["server_hostname"] = server_hostname
            return tls

    monkeypatch.setattr(
        stream.socket,
        "create_connection",
        create_connection,
    )
    monkeypatch.setattr(
        stream.ssl,
        "create_default_context",
        lambda: Context(),
    )

    assert stream._open_verified_tls_socket(3.5) is tls
    assert captured == {
        "address": ("stream-api.betfair.com", 443),
        "timeout": 3.5,
        "candidate": raw,
        "server_hostname": "stream-api.betfair.com",
    }
    assert tls.timeout == 3.5


@pytest.mark.parametrize(
    "timeout",
    [0, 0.099, 60.001, True, float("inf")],
)
def test_timeout_is_strictly_bounded(timeout: object) -> None:
    with pytest.raises(ValueError):
        stream.BetfairStreamTlsTransport(
            identity=identity(),
            secret_provider=Secrets(),
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_authentication_preserves_bearer_session_verbatim_without_write_or_live_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket()
    secrets = Secrets(
        application_key="TEST-APP-X",
        session="Bearer TEST-VENDOR-TOKEN",
    )
    transport = make_transport(
        monkeypatch,
        fake,
        secrets=secrets,
    )

    assert transport.connect() == "conn-1"
    assert transport.is_authenticated is True
    assert transport.grants_provider_write_authority is False
    assert transport.grants_live_decision_authority is False
    assert transport.requires_resubscription_after_connect is True
    assert secrets.calls == 1

    assert len(fake.sent) == 1
    assert fake.sent[0].endswith(b"\r\n")
    assert json.loads(fake.sent[0][:-2]) == {
        "appKey": "TEST-APP-X",
        "id": 1,
        "op": "authentication",
        "session": "Bearer TEST-VENDOR-TOKEN",
    }


def test_delayed_session_authentication_never_becomes_live_decision_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket()
    transport = make_transport(
        monkeypatch,
        fake,
        secrets=Secrets(key_class="DELAYED"),
        ident=identity(key_class="DELAYED"),
    )
    transport.connect()

    assert transport.identity.app_key_class == "DELAYED"
    assert (
        transport.identity.grants_live_decision_authority
        is False
    )
    assert transport.grants_live_decision_authority is False


def test_session_epoch_rotation_mismatch_fails_before_socket_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def forbidden(_timeout, _cancel=None):
        nonlocal calls
        calls += 1
        raise AssertionError(
            "network must not open for mismatched credential epoch"
        )

    monkeypatch.setattr(
        stream,
        "_open_verified_tls_socket",
        forbidden,
    )
    transport = stream.BetfairStreamTlsTransport(
        identity=identity(epoch=1),
        secret_provider=Secrets(epoch=2),
    )

    with pytest.raises(
        stream.BetfairStreamAuthenticationError,
        match="epoch",
    ):
        transport.connect()
    assert calls == 0


def test_new_session_epoch_changes_origin_identity() -> None:
    assert (
        identity(epoch=1).identity_sha256
        != identity(epoch=2).identity_sha256
    )


def test_secret_lease_repr_and_transport_errors_do_not_echo_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_app = "TEST-APP-DO-NOT-ECHO"
    secret_session = "Bearer TEST-SESSION-DO-NOT-ECHO"
    secrets = Secrets(
        application_key=secret_app,
        session=secret_session,
    )
    fake = FakeSocket(
        [
            connection_line(),
            (
                '{"op":"status","id":1,"statusCode":"FAILURE",'
                '"errorMessage":"TEST-APP-DO-NOT-ECHO '
                'Bearer TEST-SESSION-DO-NOT-ECHO"}\r\n'
            ).encode(),
        ]
    )
    transport = make_transport(
        monkeypatch,
        fake,
        secrets=secrets,
    )

    with pytest.raises(
        stream.BetfairStreamAuthenticationError
    ) as exc:
        transport.connect()

    exposed = " ".join(
        (
            repr(secrets.lease),
            repr(transport),
            str(exc.value),
        )
    )
    assert secret_app not in exposed
    assert secret_session not in exposed
    assert fake.closed is True


def test_secret_provider_exception_is_redacted_and_enters_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        stream.time,
        "monotonic",
        lambda: now[0],
    )
    secret_text = "C:/private/cert-and-password-secret.p12"
    secrets = Secrets(error=RuntimeError(secret_text))
    transport = stream.BetfairStreamTlsTransport(
        identity=identity(),
        secret_provider=secrets,
    )

    with pytest.raises(
        stream.BetfairStreamAuthenticationError
    ) as exc:
        transport.connect()
    assert secret_text not in str(exc.value)
    with pytest.raises(stream.BetfairStreamBackoffError):
        transport.connect()
    assert secrets.calls == 1


def test_repeated_auth_failure_obeys_exponential_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]
    opens: list[float] = []
    monkeypatch.setattr(
        stream.time,
        "monotonic",
        lambda: now[0],
    )

    def opener(_timeout, _cancel=None):
        opens.append(now[0])
        return FakeSocket(
            [
                connection_line(),
                status_line(ok=False),
            ]
        )

    monkeypatch.setattr(
        stream,
        "_open_verified_tls_socket",
        opener,
    )
    secrets = Secrets()
    transport = stream.BetfairStreamTlsTransport(
        identity=identity(),
        secret_provider=secrets,
    )

    with pytest.raises(
        stream.BetfairStreamAuthenticationError
    ):
        transport.connect()
    assert opens == [10.0]
    with pytest.raises(stream.BetfairStreamBackoffError):
        transport.connect()
    assert opens == [10.0]

    now[0] = 11.0
    with pytest.raises(
        stream.BetfairStreamAuthenticationError
    ):
        transport.connect()
    assert opens == [10.0, 11.0]
    now[0] = 12.9
    with pytest.raises(stream.BetfairStreamBackoffError):
        transport.connect()
    now[0] = 13.0
    with pytest.raises(
        stream.BetfairStreamAuthenticationError
    ):
        transport.connect()
    assert opens == [10.0, 11.0, 13.0]


def test_success_resets_failure_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [10.0]
    sockets = iter(
        [
            FakeSocket(
                [
                    connection_line(),
                    status_line(ok=False),
                ]
            ),
            connected_socket(),
        ]
    )
    monkeypatch.setattr(
        stream.time,
        "monotonic",
        lambda: now[0],
    )
    monkeypatch.setattr(
        stream,
        "_open_verified_tls_socket",
        lambda _timeout, _cancel=None: next(sockets),
    )
    transport = stream.BetfairStreamTlsTransport(
        identity=identity(),
        secret_provider=Secrets(),
    )

    with pytest.raises(
        stream.BetfairStreamAuthenticationError
    ):
        transport.connect()
    now[0] = 11.0
    assert transport.connect() == "conn-1"
    transport.close()
    assert transport._next_connect_monotonic == 0.0


def test_duplicate_handshake_keys_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSocket(
        [
            b'{"op":"connection","op":"status",'
            b'"connectionId":"conn-1"}\r\n'
        ]
    )
    transport = make_transport(monkeypatch, fake)

    with pytest.raises(stream.BetfairStreamProtocolError):
        transport.connect()
    assert fake.sent == []
    assert fake.closed is True


def test_auth_status_must_match_request_and_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSocket(
        [
            connection_line(),
            (
                b'{"op":"status","id":1,'
                b'"statusCode":"SUCCESS",'
                b'"connectionId":"conn-2"}\r\n'
            ),
        ]
    )
    transport = make_transport(monkeypatch, fake)

    with pytest.raises(
        stream.BetfairStreamProtocolError,
        match="connection id mismatch",
    ):
        transport.connect()
    assert fake.closed is True


def test_auth_failure_publishes_no_persisted_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSocket(
        [
            connection_line(),
            status_line(ok=False) + b'{"op":"mcm"}\r\n',
        ]
    )
    transport = make_transport(monkeypatch, fake)
    sink = Sink()

    with pytest.raises(
        stream.BetfairStreamAuthenticationError
    ):
        transport.connect()
    with pytest.raises(stream.BetfairStreamTransportError):
        transport.read_authenticated_frame()
    assert sink.frames == []


def test_fragmented_crlf_frame_is_reassembled_exactly_with_authenticated_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = b'{"op":"mcm","clk":"next"}\r\n'
    fake = connected_socket(tail=frame[:7])
    fake.chunks.extend([frame[7:-1], frame[-1:]])
    transport = make_transport(monkeypatch, fake)
    transport.connect()

    issued = transport.read_authenticated_frame()

    assert issued.payload == frame
    assert issued.payload_sha256 == sha256(frame).hexdigest()
    assert issued.connection_id == "conn-1"
    assert issued.connection_generation == 1
    assert issued.frame_sequence == 1
    assert issued.configured_identity_provider_attested is False
    assert issued.durable_persistence_verified is False
    issued.assert_transport_issued()

def test_coalesced_frames_are_split_without_byte_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = b'{"op":"mcm","clk":"a"}\r\n'
    second = b'{"op":"mcm","clk":"b"}\r\n'
    fake = connected_socket(tail=first + second)
    transport = make_transport(monkeypatch, fake)
    transport.connect()

    one = transport.read_authenticated_frame()
    two = transport.read_authenticated_frame()

    assert [one.payload, two.payload] == [first, second]
    assert [one.frame_sequence, two.frame_sequence] == [1, 2]
    assert one.connection_generation == two.connection_generation == 1

def test_oversized_no_newline_frame_fails_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket(tail=b"x" * 5)
    transport = make_transport(
        monkeypatch,
        fake,
        max_frame_bytes=4,
    )
    transport.connect()
    sink = Sink()

    with pytest.raises(
        stream.BetfairStreamProtocolError,
        match="size limit",
    ):
        transport.read_authenticated_frame()
    assert sink.frames == []
    assert fake.closed is True


def test_oversized_terminated_frame_fails_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket(tail=b"abcde\r\n")
    transport = make_transport(
        monkeypatch,
        fake,
        max_frame_bytes=4,
    )
    transport.connect()
    sink = Sink()

    with pytest.raises(
        stream.BetfairStreamProtocolError,
        match="size limit",
    ):
        transport.read_authenticated_frame()
    assert sink.frames == []


def test_disconnect_mid_frame_discards_partial_bytes_before_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_socket = connected_socket(
        tail=b'{"op":"mcm"'
    )
    second_socket = connected_socket(
        tail=b'{"op":"mcm","clk":"fresh"}\r\n'
    )
    sockets = iter(
        [
            first_socket,
            second_socket,
        ]
    )
    monkeypatch.setattr(
        stream,
        "_open_verified_tls_socket",
        lambda _timeout, _cancel=None: next(sockets),
    )
    transport = stream.BetfairStreamTlsTransport(
        identity=identity(),
        secret_provider=Secrets(),
    )
    sink = Sink()

    transport.connect()
    with pytest.raises(
        stream.BetfairStreamProtocolError,
        match="truncated",
    ):
        transport.read_authenticated_frame()
    assert sink.frames == []

    transport.connect()
    frame = transport.read_authenticated_frame()
    assert (
        frame.payload
        == b'{"op":"mcm","clk":"fresh"}\r\n'
    )


def test_bare_lf_delimiter_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket(
        tail=b'{"op":"mcm"}\n'
    )
    transport = make_transport(monkeypatch, fake)
    transport.connect()

    with pytest.raises(
        stream.BetfairStreamProtocolError,
        match="non-CRLF",
    ):
        transport.read_authenticated_frame()
    assert fake.closed is True


def test_transport_refuses_caller_mintable_durable_persistence_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = b'{"private":"TEST-PRIVATE-PAYLOAD"}\r\n'
    fake = connected_socket(tail=frame)
    transport = make_transport(monkeypatch, fake)
    transport.connect()
    sink = Sink(error=RuntimeError(frame.decode()))

    with pytest.raises(
        stream.BetfairStreamPersistenceError,
        match="does not issue durable persistence authority",
    ):
        transport.read_persisted_frame(sink)

    assert sink.frames == []
    assert transport.is_authenticated is True

@pytest.mark.parametrize(
    "mismatch",
    ["digest", "sequence"],
)
def test_mismatched_persistence_receipt_never_issues_authoritative_frame(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    assert mismatch in {"digest", "sequence"}
    fake = connected_socket(tail=b'{"op":"mcm"}\r\n')
    transport = make_transport(monkeypatch, fake)
    transport.connect()
    sink = Sink(mismatch=mismatch)

    with pytest.raises(
        stream.BetfairStreamPersistenceError,
        match="does not issue durable persistence authority",
    ):
        transport.read_persisted_frame(sink)
    assert sink.frames == []

def test_directly_constructed_or_replaced_authenticated_frame_is_not_transport_issued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"op":"mcm"}\r\n'
    fake = connected_socket(tail=payload)
    transport = make_transport(monkeypatch, fake)
    transport.connect()
    issued = transport.read_authenticated_frame()
    issued.assert_transport_issued()

    forged = stream.BetfairStreamAuthenticatedFrame(
        issued.connection_id,
        issued.connection_generation,
        issued.frame_sequence,
        issued.payload,
        issued.payload_sha256,
        issued.received_monotonic_ns,
    )
    with pytest.raises(
        stream.BetfairStreamAuthenticationError,
        match="not issued",
    ):
        forged.assert_transport_issued()

    replaced = replace(
        issued,
        frame_sequence=issued.frame_sequence + 1,
    )
    with pytest.raises(
        stream.BetfairStreamAuthenticationError,
        match="not issued",
    ):
        replaced.assert_transport_issued()

def test_reconnect_without_reauthentication_cannot_read_or_requalify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket(
        tail=b'{"op":"mcm"}\r\n'
    )
    transport = make_transport(monkeypatch, fake)
    transport.connect()
    transport.close()

    with pytest.raises(
        stream.BetfairStreamTransportError,
        match="not authenticated",
    ):
        transport.read_authenticated_frame()
    assert (
        transport.requires_resubscription_after_connect
        is True
    )


def test_no_betting_write_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = make_transport(
        monkeypatch,
        connected_socket(),
    )
    for forbidden in (
        "place_orders",
        "place_action",
        "cancel_orders",
        "replace_orders",
        "update_orders",
        "login",
        "keep_alive",
        "logout",
    ):
        assert not hasattr(transport, forbidden)


def test_close_is_idempotent_and_clears_authenticated_and_partial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket(tail=b"partial")
    transport = make_transport(monkeypatch, fake)
    transport.connect()

    transport.close()
    transport.close()

    assert fake.shutdown_how == socket.SHUT_RDWR
    assert fake.closed is True
    assert transport.is_authenticated is False
    assert transport.connection_id is None
    assert transport._receive_buffer == bytearray()


def test_public_protocol_declares_fail_closed_boundaries() -> None:
    assert (
        stream.PUBLIC_PROTOCOL["host"]
        == "stream-api.betfair.com"
    )
    assert stream.PUBLIC_PROTOCOL["port"] == 443
    assert (
        stream.PUBLIC_PROTOCOL["transport"]
        == "TLS_SOCKET_CRLF_JSON"
    )
    assert (
        stream.PUBLIC_PROTOCOL[
            "requires_authentication_before_subscription"
        ]
        is True
    )
    assert (
        stream.PUBLIC_PROTOCOL[
            "requires_resubscription_after_reconnect"
        ]
        is True
    )
    assert (
        stream.PUBLIC_PROTOCOL[
            "minimum_reconnect_backoff_seconds"
        ]
        == 1.0
    )
    assert (
        stream.PUBLIC_PROTOCOL[
            "maximum_reconnect_backoff_seconds"
        ]
        == 60.0
    )
    assert (
        stream.PUBLIC_PROTOCOL["market_write_authority"]
        is False
    )
    assert (
        stream.PUBLIC_PROTOCOL["betting_write_authority"]
        is False
    )
    assert (
        stream.PUBLIC_PROTOCOL["live_decision_authority"]
        is False
    )

def test_configured_identity_is_not_promoted_to_provider_attested_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket(tail=b'{"op":"mcm","id":7}\r\n')
    transport = make_transport(monkeypatch, fake)
    transport.connect()

    frame = transport.read_authenticated_frame()

    assert transport.identity.provider_attested is False
    assert frame.configured_identity_provider_attested is False
    assert not hasattr(frame, "session_identity_sha256")
    assert not hasattr(frame, "account_id")
    assert not hasattr(frame, "app_identity_id")
    frame.assert_transport_issued()


def test_close_cancels_blocked_connect_and_prevents_late_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_entered = Event()
    allow_open_to_return = Event()
    connect_done = Event()
    socket_candidate = connected_socket()
    outcome: dict[str, object] = {}

    def blocked_open(*args: object, **kwargs: object) -> FakeSocket:
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
        return socket_candidate

    monkeypatch.setattr(stream, "_open_verified_tls_socket", blocked_open)
    transport = stream.BetfairStreamTlsTransport(
        identity=identity(),
        secret_provider=Secrets(),
        timeout_seconds=0.1,
    )

    def run_connect() -> None:
        try:
            outcome["connection_id"] = transport.connect()
        except Exception as exc:
            outcome["error"] = exc
        finally:
            connect_done.set()

    worker = Thread(target=run_connect, name="betfair-connect-test", daemon=True)
    worker.start()
    assert open_entered.wait(timeout=1.0)

    transport.close()

    cancelled_promptly = connect_done.wait(timeout=0.25)
    allow_open_to_return.set()
    worker.join(timeout=1.0)

    assert cancelled_promptly
    assert not worker.is_alive()
    assert "connection_id" not in outcome
    assert isinstance(outcome.get("error"), stream.BetfairStreamTransportError)
    assert transport.is_authenticated is False
    assert transport.connection_id is None
    assert transport._next_connect_monotonic == 0.0


def test_public_protocol_exposes_narrow_origin_authority_only() -> None:
    assert stream.PUBLIC_PROTOCOL["configured_identity_provider_attested"] is False
    assert stream.PUBLIC_PROTOCOL["durable_persistence_authority"] is False
    assert stream.PUBLIC_PROTOCOL["authenticated_transport_origin_authority"] is True
    assert stream.PUBLIC_PROTOCOL["connect_cancellation_supported"] is True

