from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from autosport import betfair_stream_transport as stream
from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.betfair_authenticated_stream import (
    BetfairAuthenticatedFreshnessDecision,
    BetfairAuthenticatedFreshnessVerdict,
    BetfairAuthenticatedMarketSubscription,
    BetfairAuthenticatedStreamError,
    BetfairAuthenticatedStreamFreshnessRuntime,
    open_authenticated_market_subscription,
)
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


def _subscription_status(*, request_id: int = 7, success: bool = True) -> bytes:
    status = "SUCCESS" if success else "FAILURE"
    error = "false" if success else '"INVALID_INPUT"'
    return (
        f'{{"op":"status","id":{request_id},"statusCode":"{status}",'
        f'"error":{error}}}\r\n'
    ).encode("utf-8")


def _mcm(
    *,
    request_id: int = 7,
    pt: int = 1000,
    runners: list[dict[str, object]] | None = None,
) -> bytes:
    payload = {
        "op": "mcm",
        "id": request_id,
        "ct": "SUB_IMAGE",
        "initialClk": "i1",
        "clk": "c1",
        "pt": pt,
        "conflateMs": 0,
        "heartbeatMs": 5000,
        "mc": [
            {
                "id": "1.A",
                "img": True,
                "con": False,
                "rc": runners
                or [{"id": 1, "hc": 0, "ltp": 2.0}],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"


def _transport(
    monkeypatch: pytest.MonkeyPatch,
    tail: bytes,
) -> tuple[stream.BetfairStreamTlsTransport, FakeSocket]:
    fake = FakeSocket([_connection(), _auth_status() + tail])
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


def _open(
    transport: stream.BetfairStreamTlsTransport,
) -> BetfairAuthenticatedMarketSubscription:
    return open_authenticated_market_subscription(
        transport,
        provider_request_id=7,
        market_filter={"marketIds": ["1.A"]},
        market_data_fields=("EX_LTP",),
        ladder_levels=None,
        heartbeat_ms=5000,
        conflate_ms=0,
    )


def _identity(selection_id: int = 1) -> BetfairQuoteIdentity:
    return BetfairQuoteIdentity(
        BETFAIR_STREAM_SOURCE_ID,
        "1.A",
        selection_id,
        Decimal("0"),
        BetfairQuoteSide.LAST_TRADED,
        None,
    )


def test_authenticated_subscription_to_freshness_is_product_issued_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    monkeypatch.setattr(auth.time, "time_ns", lambda: 1_010_000_000)
    transport, fake = _transport(
        monkeypatch,
        _subscription_status() + _mcm(),
    )

    subscription = _open(transport)
    subscription.assert_issued()
    sent = json.loads(fake.sent[-1].decode("utf-8"))
    assert sent == {
        "conflateMs": 0,
        "heartbeatMs": 5000,
        "id": 7,
        "marketDataFilter": {"fields": ["EX_LTP"]},
        "marketFilter": {"marketIds": ["1.A"]},
        "op": "marketSubscription",
    }

    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    evidence = runtime.read_and_ingest()
    assert len(evidence) == 1
    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=20),
    )
    assert (
        decision.verdict
        is BetfairAuthenticatedFreshnessVerdict.FRESH_AUTHENTICATED_PROVIDER_PUBLISH
    )
    assert decision.decision_eligible
    assert decision.evidence_id == evidence[0].evidence_id
    assert not decision.grants_provider_write_authority
    assert not decision.grants_execution_authority
    assert not decision.real_money_authorized
    assert not subscription.grants_provider_write_authority
    assert not subscription.grants_execution_authority
    assert not subscription.real_money_authorized


def test_subscription_requires_exact_provider_success_id_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, fake = _transport(monkeypatch, _subscription_status(request_id=8))
    with pytest.raises(BetfairAuthenticatedStreamError, match="id mismatch"):
        _open(transport)
    assert fake.closed
    assert not transport.is_authenticated


def test_subscription_provider_failure_never_issues_authority_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, fake = _transport(monkeypatch, _subscription_status(success=False))
    with pytest.raises(BetfairAuthenticatedStreamError, match="not acknowledged SUCCESS"):
        _open(transport)
    assert fake.closed
    assert not transport.is_authenticated


def test_prior_post_auth_frame_prevents_subscription_relabeling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, fake = _transport(
        monkeypatch,
        _mcm(request_id=99) + _subscription_status(),
    )
    prior = transport.read_authenticated_frame()
    prior.assert_transport_issued()
    with pytest.raises(BetfairAuthenticatedStreamError, match="not the first post-auth frame"):
        _open(transport)
    assert fake.closed


def test_old_subscription_message_is_rejected_before_freshness_state_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    monkeypatch.setattr(auth.time, "time_ns", lambda: 1_010_000_000)
    transport, _ = _transport(
        monkeypatch,
        _subscription_status() + _mcm(request_id=6),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    with pytest.raises(ValueError, match="another subscription"):
        runtime.read_and_ingest()
    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=20),
    )
    assert decision.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert not decision.decision_eligible


def test_reconnect_invalidates_process_local_subscription_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _ = _transport(monkeypatch, _subscription_status())
    subscription = _open(transport)
    transport._connection_generation += 1

    with pytest.raises(BetfairAuthenticatedStreamError, match="no longer bound"):
        BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)


def test_direct_subscription_copy_cannot_mint_runtime_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _ = _transport(monkeypatch, _subscription_status())
    issued = _open(transport)
    forged = replace(issued)

    with pytest.raises(BetfairAuthenticatedStreamError, match="not issued"):
        forged.assert_issued()
    with pytest.raises(BetfairAuthenticatedStreamError, match="not issued"):
        BetfairAuthenticatedStreamFreshnessRuntime(transport, forged)


def test_direct_positive_decision_dto_is_not_decision_eligible() -> None:
    forged = BetfairAuthenticatedFreshnessDecision(
        verdict=(
            BetfairAuthenticatedFreshnessVerdict.FRESH_AUTHENTICATED_PROVIDER_PUBLISH
        ),
        reason="looks fresh",
        evidence_id="a" * 64,
        subscription_id="b" * 64,
        transport_frame_sha256="c" * 64,
        evaluated_at_ms=1000,
    )
    assert not forged.decision_eligible


def test_credential_like_market_filter_is_rejected_before_network_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, fake = _transport(monkeypatch, _subscription_status())
    sent_before = list(fake.sent)

    with pytest.raises(ValueError, match="credential-like"):
        open_authenticated_market_subscription(
            transport,
            provider_request_id=7,
            market_filter={"marketIds": ["1.A"], "sessionToken": "must-not-appear"},
            market_data_fields=("EX_LTP",),
            ladder_levels=None,
            heartbeat_ms=5000,
            conflate_ms=0,
        )
    assert fake.sent == sent_before


def test_market_filter_resource_bound_fails_before_network_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, fake = _transport(monkeypatch, _subscription_status())
    sent_before = list(fake.sent)

    with pytest.raises(ValueError, match="too many items"):
        open_authenticated_market_subscription(
            transport,
            provider_request_id=7,
            market_filter={"marketIds": [str(i) for i in range(1025)]},
            market_data_fields=("EX_LTP",),
            ladder_levels=None,
            heartbeat_ms=5000,
            conflate_ms=0,
        )
    assert fake.sent == sent_before


def test_transport_origin_tracking_is_bounded_and_overflow_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    monkeypatch.setattr(auth.time, "time_ns", lambda: 1_010_000_000)
    monkeypatch.setattr(auth, "_MAX_TRACKED_AUTHORITATIVE_QUOTES", 1)
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(
            runners=[
                {"id": 1, "hc": 0, "ltp": 2.0},
                {"id": 2, "hc": 0, "ltp": 2.1},
            ]
        ),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    with pytest.raises(BetfairAuthenticatedStreamError, match="exceeded its bound"):
        runtime.read_and_ingest()
    decision = runtime.evaluate(
        _identity(1),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=20),
    )
    assert decision.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert not decision.decision_eligible


def test_noncanonical_transport_subclass_cannot_issue_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ShadowTransport(stream.BetfairStreamTlsTransport):
        pass

    shadow = ShadowTransport(
        identity=stream.BetfairStreamSessionIdentity(
            account_id="acct-1",
            app_identity_id="betfair-app-1",
            app_key_class="LIVE",
            session_epoch=1,
        ),
        secret_provider=Secrets(),
    )
    with pytest.raises(TypeError, match="canonical"):
        open_authenticated_market_subscription(
            shadow,
            provider_request_id=7,
            market_filter={"marketIds": ["1.A"]},
            market_data_fields=("EX_LTP",),
            ladder_levels=None,
            heartbeat_ms=5000,
            conflate_ms=0,
        )
