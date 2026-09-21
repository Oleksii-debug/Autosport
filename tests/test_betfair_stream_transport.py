from __future__ import annotations

import json
import socket
from hashlib import sha256

import pytest

from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport import betfair_stream_transport as stream


class FakeSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False
        self.shutdown_how: int | None = None
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

    def shutdown(self, how: int) -> None:
        self.shutdown_how = how

    def close(self) -> None:
        self.closed = True


class Secrets:
    def __init__(
        self,
        application_key: str = "APP-SECRET",
        session: str = "Bearer SESSION-SECRET",
    ) -> None:
        self.credentials = BetfairSessionCredentials(application_key, session)
        self.identities: list[stream.BetfairStreamSessionIdentity] = []

    def get_session_credentials(
        self, identity: stream.BetfairStreamSessionIdentity
    ) -> BetfairSessionCredentials:
        self.identities.append(identity)
        return self.credentials


def identity(
    *, epoch: int = 1, key_class: str = "LIVE"
) -> stream.BetfairStreamSessionIdentity:
    return stream.BetfairStreamSessionIdentity(
        account_id="acct-1",
        app_identity_id="betfair-app-production-1",
        app_key_class=key_class,
        session_epoch=epoch,
    )


def connected_socket(*, tail: bytes = b"") -> FakeSocket:
    return FakeSocket(
        [
            b'{"op":"connection","connectionId":"conn-1"}\r\n',
            b'{"op":"status","id":1,"statusCode":"SUCCESS","connectionClosed":false}'
            b"\r\n" + tail,
        ]
    )


def make_transport(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeSocket,
    *,
    secrets: Secrets | None = None,
    max_chunk_bytes: int = 65536,
) -> stream.BetfairStreamTlsTransport:
    monkeypatch.setattr(stream, "_open_verified_tls_socket", lambda timeout: fake)
    return stream.BetfairStreamTlsTransport(
        identity=identity(),
        secret_provider=secrets or Secrets(),
        max_chunk_bytes=max_chunk_bytes,
    )


def test_verified_tls_opens_only_canonical_stream_endpoint(
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
        def wrap_socket(self, candidate, *, server_hostname):
            captured["candidate"] = candidate
            captured["server_hostname"] = server_hostname
            return tls

    monkeypatch.setattr(stream.socket, "create_connection", create_connection)
    monkeypatch.setattr(stream.ssl, "create_default_context", lambda: Context())

    assert stream._open_verified_tls_socket(3.5) is tls
    assert captured == {
        "address": ("stream-api.betfair.com", 443),
        "timeout": 3.5,
        "candidate": raw,
        "server_hostname": "stream-api.betfair.com",
    }


def test_authentication_preserves_bearer_session_verbatim_and_never_grants_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket()
    secrets = Secrets(application_key="KEY-X", session="Bearer token-with-prefix")
    transport = make_transport(monkeypatch, fake, secrets=secrets)

    assert transport.connect() == "conn-1"
    assert transport.is_authenticated is True
    assert transport.grants_provider_write_authority is False
    assert transport.identity.grants_provider_write_authority is False
    assert secrets.identities == [transport.identity]

    assert len(fake.sent) == 1
    assert fake.sent[0].endswith(b"\r\n")
    auth = json.loads(fake.sent[0][:-2])
    assert auth == {
        "appKey": "KEY-X",
        "id": 1,
        "op": "authentication",
        "session": "Bearer token-with-prefix",
    }


def test_secrets_are_absent_from_transport_repr_identity_and_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_app = "APPKEY-DO-NOT-LEAK"
    secret_session = "Bearer SESSION-DO-NOT-LEAK"
    fake = FakeSocket(
        [
            b'{"op":"connection","connectionId":"conn-1"}\r\n',
            (
                '{"op":"status","id":1,"statusCode":"FAILURE",'
                '"errorMessage":"APPKEY-DO-NOT-LEAK Bearer SESSION-DO-NOT-LEAK"}\r\n'
            ).encode(),
        ]
    )
    transport = make_transport(
        monkeypatch, fake, secrets=Secrets(secret_app, secret_session)
    )

    with pytest.raises(stream.BetfairStreamAuthenticationError) as exc:
        transport.connect()

    exposed = " ".join((repr(transport), repr(transport.identity), str(exc.value)))
    assert secret_app not in exposed
    assert secret_session not in exposed
    assert fake.closed is True


def test_duplicate_handshake_keys_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSocket(
        [b'{"op":"connection","op":"status","connectionId":"conn-1"}\r\n']
    )
    transport = make_transport(monkeypatch, fake)

    with pytest.raises(stream.BetfairStreamProtocolError):
        transport.connect()

    assert fake.sent == []
    assert fake.closed is True


def test_status_must_match_authentication_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSocket(
        [
            b'{"op":"connection","connectionId":"conn-1"}\r\n',
            b'{"op":"status","id":2,"statusCode":"SUCCESS"}\r\n',
        ]
    )
    transport = make_transport(monkeypatch, fake)

    with pytest.raises(stream.BetfairStreamProtocolError, match="id mismatch"):
        transport.connect()

    assert fake.closed is True


def test_connection_id_cannot_change_during_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSocket(
        [
            b'{"op":"connection","connectionId":"conn-1"}\r\n',
            b'{"op":"status","id":1,"statusCode":"SUCCESS","connectionId":"conn-2"}\r\n',
        ]
    )
    transport = make_transport(monkeypatch, fake)

    with pytest.raises(stream.BetfairStreamProtocolError, match="connection id mismatch"):
        transport.connect()

    assert fake.closed is True


def test_stream_bytes_buffered_with_auth_status_are_not_lost_and_are_persisted_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"op":"mcm","clk":"next"}\r\n'
    fake = connected_socket(tail=payload)
    transport = make_transport(monkeypatch, fake)
    transport.connect()
    persisted: list[stream.BetfairStreamRawChunk] = []

    chunk = transport.read_persisted_chunk(persisted.append)

    assert persisted == [chunk]
    assert chunk.payload == payload
    assert chunk.payload_sha256 == sha256(payload).hexdigest()
    assert chunk.chunk_sequence == 1
    assert chunk.connection_id == "conn-1"
    assert payload.decode() not in repr(chunk)


def test_raw_ingress_is_bounded_and_sequences_only_after_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket()
    transport = make_transport(monkeypatch, fake, max_chunk_bytes=4)
    transport.connect()
    fake.chunks.extend([b"abcdefgh", b"ijkl"])
    persisted: list[stream.BetfairStreamRawChunk] = []

    first = transport.read_persisted_chunk(persisted.append)
    second = transport.read_persisted_chunk(persisted.append)
    third = transport.read_persisted_chunk(persisted.append)

    assert [item.payload for item in (first, second, third)] == [
        b"abcd",
        b"efgh",
        b"ijkl",
    ]
    assert [item.chunk_sequence for item in persisted] == [1, 2, 3]
    assert all(size <= 4096 for size in fake.recv_calls[:2])
    assert fake.recv_calls[-3:] == [4, 4, 4]


def test_persistence_failure_closes_connection_and_does_not_echo_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_payload = b"provider-account-private-payload"
    fake = connected_socket(tail=secret_payload)
    transport = make_transport(monkeypatch, fake)
    transport.connect()

    def fail(_chunk):
        raise RuntimeError(secret_payload.decode())

    with pytest.raises(stream.BetfairStreamPersistenceError) as exc:
        transport.read_persisted_chunk(fail)

    assert secret_payload.decode() not in str(exc.value)
    assert fake.closed is True
    assert transport.is_authenticated is False


def test_network_receive_failure_is_generic_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken(FakeSocket):
        def recv(self, size: int) -> bytes:
            if self.sent:
                raise OSError("socket failure with secret-ish provider text")
            return super().recv(size)

    fake = Broken(
        [
            b'{"op":"connection","connectionId":"conn-1"}\r\n',
            b'{"op":"status","id":1,"statusCode":"SUCCESS"}\r\n',
        ]
    )
    transport = make_transport(monkeypatch, fake)

    with pytest.raises(stream.BetfairStreamTransportError) as exc:
        transport.connect()

    assert "secret-ish" not in str(exc.value)
    assert fake.closed is True


def test_empty_post_auth_read_is_disconnect_not_empty_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket()
    transport = make_transport(monkeypatch, fake)
    transport.connect()

    with pytest.raises(stream.BetfairStreamTransportError, match="closed"):
        transport.read_persisted_chunk(lambda _chunk: None)

    assert transport.is_authenticated is False


def test_session_identity_digest_is_non_secret_and_epoch_bound() -> None:
    first = identity(epoch=1)
    second = identity(epoch=2)
    delayed = identity(epoch=1, key_class="DELAYED")

    assert first.identity_sha256 != second.identity_sha256
    assert first.identity_sha256 != delayed.identity_sha256
    assert len(first.identity_sha256) == 64
    assert "APP-SECRET" not in first.identity_sha256
    assert "SESSION-SECRET" not in first.identity_sha256


@pytest.mark.parametrize("epoch", [0, -1, True, 1.5])
def test_session_epoch_is_strictly_positive_integer(epoch: object) -> None:
    with pytest.raises(ValueError):
        stream.BetfairStreamSessionIdentity(
            account_id="acct",
            app_identity_id="app",
            app_key_class="LIVE",
            session_epoch=epoch,  # type: ignore[arg-type]
        )


def test_delayed_key_identity_is_allowed_but_not_promoted_to_write_authority() -> None:
    delayed = identity(key_class="DELAYED")
    assert delayed.app_key_class == "DELAYED"
    assert delayed.grants_provider_write_authority is False


def test_transport_has_no_betting_write_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = make_transport(monkeypatch, connected_socket())
    for forbidden in (
        "place_orders",
        "place_action",
        "cancel_orders",
        "replace_orders",
        "update_orders",
    ):
        assert not hasattr(transport, forbidden)


def test_close_is_idempotent_and_clears_authenticated_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = connected_socket()
    transport = make_transport(monkeypatch, fake)
    transport.connect()

    transport.close()
    transport.close()

    assert fake.shutdown_how == socket.SHUT_RDWR
    assert fake.closed is True
    assert transport.is_authenticated is False
    assert transport.connection_id is None


def test_reconnect_requires_explicit_new_connect_and_preserves_session_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_socket = connected_socket()
    second_socket = connected_socket()
    sockets = iter([first_socket, second_socket])
    monkeypatch.setattr(
        stream, "_open_verified_tls_socket", lambda timeout: next(sockets)
    )
    transport = stream.BetfairStreamTlsTransport(
        identity=identity(epoch=7),
        secret_provider=Secrets(),
    )

    transport.connect()
    with pytest.raises(stream.BetfairStreamTransportError, match="already connected"):
        transport.connect()
    transport.close()
    transport.connect()

    assert transport.identity.session_epoch == 7
    assert second_socket.sent


def test_oversized_handshake_without_crlf_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeSocket([b"x" * (stream._HANDSHAKE_MAX_BYTES + 1)])
    transport = make_transport(monkeypatch, fake)

    with pytest.raises(stream.BetfairStreamProtocolError, match="size limit"):
        transport.connect()

    assert fake.closed is True


def test_public_protocol_declares_tls_socket_and_no_write_authority() -> None:
    assert stream.PUBLIC_PROTOCOL == {
        "host": "stream-api.betfair.com",
        "port": 443,
        "transport": "TLS_SOCKET_CRLF_JSON",
        "market_write_authority": False,
        "betting_write_authority": False,
    }
