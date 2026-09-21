from autosport import betfair_stream_transport as stream
from autosport.betfair_account_readonly import BetfairSessionCredentials

import pytest


class _FakeSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False

    def recv(self, size: int) -> bytes:
        if not self.chunks:
            return b""
        head = self.chunks.pop(0)
        if len(head) <= size:
            return head
        self.chunks.insert(0, head[size:])
        return head[:size]

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def settimeout(self, _value: float) -> None:
        pass

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _Secrets:
    def __init__(self, lease: stream.BetfairStreamCredentialLease) -> None:
        self.lease = lease

    def get_session_lease(self) -> stream.BetfairStreamCredentialLease:
        return self.lease


class _NoOpSink:
    def persist(
        self,
        frame: stream.BetfairStreamRawFrame,
    ) -> stream.BetfairStreamPersistenceReceipt:
        # Deliberately perform no persistence whatsoever. Returning an exact-shape
        # public DTO must not be sufficient to mint durable/persisted authority.
        return stream.BetfairStreamPersistenceReceipt.for_frame(frame)


def _identity() -> stream.BetfairStreamSessionIdentity:
    return stream.BetfairStreamSessionIdentity(
        account_id="acct-A",
        app_identity_id="app-A",
        app_key_class="LIVE",
        session_epoch=1,
    )


def _lease(
    *,
    application_key: str,
    session_token: str,
) -> stream.BetfairStreamCredentialLease:
    return stream.BetfairStreamCredentialLease(
        account_id="acct-A",
        app_identity_id="app-A",
        app_key_class="LIVE",
        session_epoch=1,
        credentials=BetfairSessionCredentials(application_key, session_token),
    )


def _handshake(*, tail: bytes = b"") -> _FakeSocket:
    return _FakeSocket(
        [
            b'{"op":"connection","connectionId":"conn-1"}\r\n',
            (
                b'{"op":"status","id":1,"statusCode":"SUCCESS",'
                b'"connectionClosed":false}\r\n' + tail
            ),
        ]
    )


def test_caller_metadata_cannot_attest_different_authenticated_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-metadata plus B credentials must not mint identity-A provenance."""

    try:
        lease = _lease(
            application_key="APP-B-NOT-A-REAL-KEY",
            session_token="TOKEN-B-NOT-A-REAL-SESSION",
        )
    except (TypeError, ValueError):
        # A future origin-bound lease constructor may make arbitrary caller
        # construction structurally impossible, which closes this falsifier.
        return

    fake = _handshake()
    monkeypatch.setattr(
        stream,
        "_open_verified_tls_socket",
        lambda _timeout: fake,
    )
    transport = stream.BetfairStreamTlsTransport(
        identity=_identity(),
        secret_provider=_Secrets(lease),
    )

    with pytest.raises(stream.BetfairStreamTransportError):
        transport.connect()


def test_noop_sink_cannot_mint_durable_persisted_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-mintable receipt is not proof that frame bytes were durable."""

    try:
        lease = _lease(
            application_key="APP-A-NOT-A-REAL-KEY",
            session_token="TOKEN-A-NOT-A-REAL-SESSION",
        )
    except (TypeError, ValueError):
        return

    fake = _handshake(tail=b'{"op":"mcm","id":7}\r\n')
    monkeypatch.setattr(
        stream,
        "_open_verified_tls_socket",
        lambda _timeout: fake,
    )
    transport = stream.BetfairStreamTlsTransport(
        identity=_identity(),
        secret_provider=_Secrets(lease),
    )
    transport.connect()

    reader = getattr(transport, "read_persisted_frame", None)
    if reader is None:
        # Narrowing this layer back to authenticated transport-origin framing and
        # moving durability authority elsewhere is an explicitly safe repair.
        return

    with pytest.raises(stream.BetfairStreamTransportError):
        reader(_NoOpSink())
