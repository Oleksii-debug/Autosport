from __future__ import annotations

from hashlib import sha256
import json

import pytest

from autosport import _betfair_authenticated_stream_filter_snapshot as filter_guard
from autosport import betfair_authenticated_stream as auth
from autosport import betfair_stream_transport as stream
from autosport.betfair_account_readonly import BetfairSessionCredentials


class FakeSocket:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout: float | None = None

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

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class Secrets:
    def get_session_lease(self) -> stream.BetfairStreamCredentialLease:
        return stream.BetfairStreamCredentialLease(
            account_id="acct-1",
            app_identity_id="betfair-app-1",
            app_key_class="LIVE",
            session_epoch=1,
            credentials=BetfairSessionCredentials(
                "TEST_APP_KEY_NOT_A_REAL_CREDENTIAL",
                "Bearer TEST_SESSION_NOT_A_REAL_CREDENTIAL",
            ),
        )


def _connection() -> bytes:
    return b'{"op":"connection","connectionId":"conn-1"}\r\n'


def _auth_status() -> bytes:
    return (
        b'{"op":"status","id":1,"statusCode":"SUCCESS",'
        b'"connectionClosed":false}\r\n'
    )


def _subscription_status() -> bytes:
    return (
        b'{"op":"status","id":7,"statusCode":"SUCCESS",'
        b'"error":false}\r\n'
    )


def _transport(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[stream.BetfairStreamTlsTransport, FakeSocket]:
    fake = FakeSocket([_connection(), _auth_status() + _subscription_status()])
    monkeypatch.setattr(
        stream,
        "_open_verified_tls_socket",
        lambda _timeout, _cancel=None: fake,
    )
    transport = stream.BetfairStreamTlsTransport(
        identity=stream.BetfairStreamSessionIdentity(
            account_id="acct-1",
            app_identity_id="betfair-app-1",
            app_key_class="LIVE",
            session_epoch=1,
        ),
        secret_provider=Secrets(),
    )
    assert transport.connect() == "conn-1"
    return transport, fake


def test_caller_mutation_after_snapshot_cannot_change_sent_filter_or_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, fake = _transport(monkeypatch)
    caller_filter = {
        "marketIds": ["1.A"],
        "marketTypeCodes": ["MATCH_ODDS"],
    }
    before = json.dumps(
        caller_filter,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    original_canonicalizer = filter_guard._CANONICAL_JSON_BYTES
    mutated = False

    def mutate_after_first_snapshot(value):
        nonlocal mutated
        payload = original_canonicalizer(value)
        if value is caller_filter and not mutated:
            mutated = True
            caller_filter["marketIds"][0] = "9.MUTATED"
            caller_filter["marketTypeCodes"].append("OVER_UNDER_25")
        return payload

    monkeypatch.setattr(
        filter_guard,
        "_CANONICAL_JSON_BYTES",
        mutate_after_first_snapshot,
    )

    subscription = auth.open_authenticated_market_subscription(
        transport,
        provider_request_id=7,
        market_filter=caller_filter,
        market_data_fields=("EX_LTP",),
        ladder_levels=None,
        heartbeat_ms=5000,
        conflate_ms=0,
    )

    assert mutated
    assert caller_filter == {
        "marketIds": ["9.MUTATED"],
        "marketTypeCodes": ["MATCH_ODDS", "OVER_UNDER_25"],
    }
    sent = json.loads(fake.sent[-1].decode("utf-8"))
    assert sent["marketFilter"] == {
        "marketIds": ["1.A"],
        "marketTypeCodes": ["MATCH_ODDS"],
    }
    assert subscription.market_filter_sha256 == sha256(before).hexdigest()
    subscription.assert_issued()


def test_snapshot_guard_is_installed_on_public_subscription_issuer() -> None:
    assert getattr(
        auth.open_authenticated_market_subscription,
        "_autosport_market_filter_snapshot_guard",
        False,
    )
