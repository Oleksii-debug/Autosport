from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json

import pytest

from autosport import _betfair_authenticated_stream_filter_snapshot as filter_guard
from autosport import betfair_authenticated_stream as auth
from autosport import betfair_stream_transport as stream
from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.betfair_stream_codec import (
    BETFAIR_STREAM_SOURCE_ID,
    BetfairQuoteIdentity,
    BetfairQuoteSide,
)
from autosport.betfair_stream_publish_freshness import BetfairStreamFreshnessPolicy


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


def _mcm(*, publish_time_ms: int = 1000) -> bytes:
    payload = {
        "op": "mcm",
        "id": 7,
        "ct": "SUB_IMAGE",
        "initialClk": "i1",
        "clk": "c1",
        "pt": publish_time_ms,
        "conflateMs": 0,
        "heartbeatMs": 5000,
        "mc": [
            {
                "id": "1.A",
                "img": True,
                "con": False,
                "rc": [{"id": 1, "hc": 0, "ltp": 2.0}],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"


def _identity() -> BetfairQuoteIdentity:
    return BetfairQuoteIdentity(
        BETFAIR_STREAM_SOURCE_ID,
        "1.A",
        1,
        Decimal("0"),
        BetfairQuoteSide.LAST_TRADED,
        None,
    )


def _transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tail: bytes = b"",
) -> tuple[stream.BetfairStreamTlsTransport, FakeSocket]:
    fake = FakeSocket(
        [_connection(), _auth_status() + _subscription_status() + tail]
    )
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


def _open_subscription(
    transport: stream.BetfairStreamTlsTransport,
) -> auth.BetfairAuthenticatedMarketSubscription:
    return auth.open_authenticated_market_subscription(
        transport,
        provider_request_id=7,
        market_filter={"marketIds": ["1.A"]},
        market_data_fields=("EX_LTP",),
        ladder_levels=None,
        heartbeat_ms=5000,
        conflate_ms=0,
    )


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


@pytest.mark.parametrize("bypass", ["original_alias", "wrapped_alias"])
def test_direct_pre_guard_aliases_cannot_bypass_intrinsic_filter_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    bypass: str,
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
    original_canonicalizer = auth._canonical_json_bytes
    mutated = False

    def mutate_after_issuer_snapshot(value):
        nonlocal mutated
        payload = original_canonicalizer(value)
        if value is caller_filter and not mutated:
            mutated = True
            caller_filter["marketIds"][0] = "9.MUTATED"
            caller_filter["marketTypeCodes"].append("OVER_UNDER_25")
        return payload

    monkeypatch.setattr(auth, "_canonical_json_bytes", mutate_after_issuer_snapshot)
    if bypass == "original_alias":
        issuer = filter_guard._ORIGINAL_OPEN
    else:
        issuer = auth.open_authenticated_market_subscription.__wrapped__

    subscription = issuer(
        transport,
        provider_request_id=7,
        market_filter=caller_filter,
        market_data_fields=("EX_LTP",),
        ladder_levels=None,
        heartbeat_ms=5000,
        conflate_ms=0,
    )

    assert mutated
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


def test_time_ns_rebind_before_runtime_admission_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _fake = _transport(monkeypatch)
    subscription = _open_subscription(transport)

    monkeypatch.setattr(auth.time, "time_ns", lambda: 1_010_000_000)

    with pytest.raises(
        auth.BetfairAuthenticatedStreamError,
        match="product wall-clock dispatch changed",
    ):
        auth.BetfairAuthenticatedStreamFreshnessRuntime(
            transport,
            subscription,
        )


def test_time_ns_rebind_after_runtime_admission_fails_closed_on_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _fake = _transport(monkeypatch, tail=_mcm())
    subscription = _open_subscription(transport)
    runtime = auth.BetfairAuthenticatedStreamFreshnessRuntime(
        transport,
        subscription,
    )

    monkeypatch.setattr(auth.time, "time_ns", lambda: 1_010_000_000)

    with pytest.raises(
        auth.BetfairAuthenticatedStreamError,
        match="product wall-clock dispatch changed",
    ):
        runtime.read_and_ingest()


def test_time_ns_rebind_after_positive_decision_revokes_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = auth.time.time_ns() // 1_000_000
    transport, _fake = _transport(
        monkeypatch,
        tail=_mcm(publish_time_ms=publish_time_ms),
    )
    subscription = _open_subscription(transport)
    runtime = auth.BetfairAuthenticatedStreamFreshnessRuntime(
        transport,
        subscription,
    )
    runtime.read_and_ingest()
    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=60_000),
    )
    assert (
        decision.verdict
        is auth.BetfairAuthenticatedFreshnessVerdict.FRESH_AUTHENTICATED_PROVIDER_PUBLISH
    )
    assert decision.decision_eligible

    monkeypatch.setattr(auth.time, "time_ns", lambda: 1_010_000_000)

    assert not decision.decision_eligible
