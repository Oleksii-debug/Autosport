from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal
from threading import Event, Thread

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
    def __init__(self, *, key_class: str = "LIVE") -> None:
        self.key_class = key_class

    def get_session_lease(self) -> stream.BetfairStreamCredentialLease:
        return stream.BetfairStreamCredentialLease(
            account_id="acct-1",
            app_identity_id="betfair-app-1",
            app_key_class=self.key_class,
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
    payload: dict[str, object] = {
        "op": "status",
        "id": request_id,
        "statusCode": "SUCCESS" if success else "FAILURE",
        "connectionClosed": False,
    }
    if not success:
        payload["errorCode"] = "INVALID_INPUT"
        payload["errorMessage"] = "invalid market subscription"
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"

def _mcm(
    *,
    request_id: int = 7,
    pt: int | None = None,
    runners: list[dict[str, object]] | None = None,
    conflate_ms: int = 0,
    betting_type: str = "ODDS",
    ladder_type: str | None = "CLASSIC",
) -> bytes:
    if pt is None:
        pt = time.time_ns() // 1_000_000
    runner_changes = runners or [{"id": 1, "hc": 0, "ltp": 2.0}]
    runner_definitions = [
        {
            "id": item["id"],
            "hc": item.get("hc", 0),
            "status": "ACTIVE",
        }
        for item in runner_changes
    ]
    market_definition: dict[str, object] = {
        "status": "OPEN",
        "bettingType": betting_type,
        "runners": runner_definitions,
    }
    if ladder_type is not None:
        market_definition["priceLadderDefinition"] = {"type": ladder_type}
    payload = {
        "op": "mcm",
        "id": request_id,
        "ct": "SUB_IMAGE",
        "initialClk": "i1",
        "clk": "c1",
        "pt": pt,
        "conflateMs": conflate_ms,
        "heartbeatMs": 5000,
        "mc": [
            {
                "id": "1.A",
                "img": True,
                "con": False,
                "marketDefinition": market_definition,
                "rc": runner_changes,
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"


def _delta_mcm(
    *,
    request_id: int = 7,
    pt: int | None = None,
    clk: str = "c2",
    price: float = 2.1,
) -> bytes:
    if pt is None:
        pt = time.time_ns() // 1_000_000
    payload = {
        "op": "mcm",
        "id": request_id,
        "clk": clk,
        "pt": pt,
        "mc": [
            {
                "id": "1.A",
                "img": False,
                "con": False,
                "marketDefinition": {
                    "status": "OPEN",
                    "bettingType": "ODDS",
                    "priceLadderDefinition": {"type": "CLASSIC"},
                },
                "rc": [{"id": 1, "hc": 0, "ltp": price}],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"


def _market_status_mcm(
    status: str,
    *,
    pt: int | None = None,
    clk: str = "c-status",
) -> bytes:
    if pt is None:
        pt = time.time_ns() // 1_000_000
    payload = {
        "op": "mcm",
        "id": 7,
        "clk": clk,
        "pt": pt,
        "mc": [
            {
                "id": "1.A",
                "img": False,
                "con": False,
                "marketDefinition": {
                    "status": status,
                    "bettingType": "ODDS",
                    "priceLadderDefinition": {"type": "CLASSIC"},
                },
                "rc": [],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"


def _runner_status_mcm(
    status: str,
    *,
    pt: int | None = None,
    clk: str = "c-runner",
) -> bytes:
    if pt is None:
        pt = time.time_ns() // 1_000_000
    payload = {
        "op": "mcm",
        "id": 7,
        "clk": clk,
        "pt": pt,
        "mc": [
            {
                "id": "1.A",
                "img": False,
                "con": False,
                "marketDefinition": {
                    "status": "OPEN",
                    "bettingType": "ODDS",
                    "priceLadderDefinition": {"type": "CLASSIC"},
                    "runners": [
                        {
                            "id": 1,
                            "hc": 0,
                            "status": status,
                        }
                    ],
                },
                "rc": [],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"


def _market_ladder_mcm(
    ladder_type: str,
    *,
    pt: int,
    clk: str,
) -> bytes:
    payload = {
        "op": "mcm",
        "id": 7,
        "clk": clk,
        "pt": pt,
        "mc": [
            {
                "id": "1.A",
                "img": False,
                "con": False,
                "marketDefinition": {
                    "status": "OPEN",
                    "bettingType": "ODDS",
                    "priceLadderDefinition": {"type": ladder_type},
                    "runners": [
                        {"id": 1, "hc": 0, "status": "ACTIVE"},
                    ],
                },
                "rc": [],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"


def _heartbeat_mcm(
    *,
    pt: int,
    clk: str,
    status: int | None = None,
) -> bytes:
    payload: dict[str, object] = {
        "op": "mcm",
        "id": 7,
        "ct": "HEARTBEAT",
        "clk": clk,
        "pt": pt,
    }
    if status is not None:
        payload["status"] = status
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"


def _transport(
    monkeypatch: pytest.MonkeyPatch,
    tail: bytes,
    *,
    key_class: str = "LIVE",
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
            app_key_class=key_class,
            session_epoch=1,
        ),
        secret_provider=Secrets(key_class=key_class),
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
        market_data_fields=("EX_LTP", "EX_MARKET_DEF"),
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


def _fresh_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    stream.BetfairStreamTlsTransport,
    BetfairAuthenticatedStreamFreshnessRuntime,
    BetfairAuthenticatedFreshnessDecision,
]:
    transport, _ = _transport(monkeypatch, _subscription_status() + _mcm())
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert decision.decision_eligible
    return transport, runtime, decision


def test_authenticated_subscription_to_freshness_is_product_issued_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        "marketDataFilter": {"fields": ["EX_LTP", "EX_MARKET_DEF"]},
        "marketFilter": {"marketIds": ["1.A"]},
        "op": "marketSubscription",
    }

    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    evidence = runtime.read_and_ingest()
    assert len(evidence) == 1
    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
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


def test_coalesced_buffered_second_frame_retains_original_socket_ingress_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    base_pt = time.time_ns() // 1_000_000
    first = _mcm(pt=base_pt)
    second = _delta_mcm(pt=base_pt + 1)
    ingress_ns = [1_000_000_000]

    def fake_monotonic_ns() -> int:
        return ingress_ns[0]

    monkeypatch.setattr(stream, "_MONOTONIC_NS", fake_monotonic_ns)
    monkeypatch.setattr(auth, "_MONOTONIC_NS", fake_monotonic_ns)
    monkeypatch.setattr(stream.time, "monotonic_ns", fake_monotonic_ns)

    transport, _ = _transport(
        monkeypatch,
        _subscription_status() + first + second,
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    first_evidence = runtime.read_and_ingest()
    assert len(first_evidence) == 1
    first_received_ns = runtime._transport_by_identity[_identity()][2]

    ingress_ns[0] += 15_000_000_000
    second_evidence = runtime.read_and_ingest()
    assert len(second_evidence) == 1
    second_received_ns = runtime._transport_by_identity[_identity()][2]

    assert first_received_ns == second_received_ns == 1_000_000_000

    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert (
        decision.verdict
        is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    )
    assert "consumer lag exceeds max_age_ms" in decision.reason
    assert not decision.decision_eligible


def test_authenticated_runtime_rejects_frame_minted_under_alternate_clock_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    transport, fake = _transport(monkeypatch, _subscription_status())
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    alternate_clock = lambda: 7_000_000_000
    monkeypatch.setattr(stream, "_MONOTONIC_NS", alternate_clock)
    monkeypatch.setattr(stream.time, "monotonic_ns", alternate_clock)
    fake.chunks.append(_mcm())

    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="receive clock authority mismatch",
    ):
        runtime.read_and_ingest()

    assert transport.is_authenticated is False
    assert transport.connection_id is None
    assert fake.closed is True


def test_authenticated_freshness_rejects_local_consumer_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    identity = _identity()
    transport, _ = _transport(
        monkeypatch,
        _subscription_status() + _mcm(),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    received_ns = runtime._transport_by_identity[identity][2]
    fake_monotonic_ns = lambda: received_ns + 10_001_000_000
    monkeypatch.setattr(auth, "_MONOTONIC_NS", fake_monotonic_ns)
    monkeypatch.setattr(auth.time, "monotonic_ns", fake_monotonic_ns)

    decision = runtime.evaluate(
        identity,
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )

    assert (
        decision.verdict
        is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    )
    assert "consumer lag exceeds max_age_ms" in decision.reason
    assert not decision.decision_eligible


def test_positive_decision_is_revoked_when_local_consumer_age_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    identity = _identity()
    transport, _ = _transport(
        monkeypatch,
        _subscription_status() + _mcm(),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    received_ns = runtime._transport_by_identity[identity][2]
    now_ns = [received_ns + 1_000_000]
    fake_monotonic_ns = lambda: now_ns[0]
    monkeypatch.setattr(auth, "_MONOTONIC_NS", fake_monotonic_ns)
    monkeypatch.setattr(auth.time, "monotonic_ns", fake_monotonic_ns)

    decision = runtime.evaluate(
        identity,
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert decision.decision_eligible

    now_ns[0] = received_ns + 10_001_000_000
    assert not decision.decision_eligible


def test_authenticated_freshness_rejects_monotonic_clock_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    identity = _identity()
    transport, _ = _transport(
        monkeypatch,
        _subscription_status() + _mcm(),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    received_ns = runtime._transport_by_identity[identity][2]
    fake_monotonic_ns = lambda: received_ns - 1
    monkeypatch.setattr(auth, "_MONOTONIC_NS", fake_monotonic_ns)
    monkeypatch.setattr(auth.time, "monotonic_ns", fake_monotonic_ns)

    decision = runtime.evaluate(
        identity,
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )

    assert (
        decision.verdict
        is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    )
    assert "monotonic clock regressed" in decision.reason
    assert not decision.decision_eligible


def test_caller_policy_mutation_cannot_extend_issued_freshness_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    identity = _identity()
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status() + _mcm(pt=publish_time_ms),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    caller_policy = BetfairStreamFreshnessPolicy(max_age_ms=10_000)
    decision = runtime.evaluate(identity, policy=caller_policy)
    assert decision.decision_eligible

    authority = auth._ISSUED_DECISIONS[decision]
    assert authority.policy is not caller_policy
    assert authority.policy.max_age_ms == 10_000

    # frozen=True is not an ownership boundary: a caller retaining the original
    # object can still mutate it directly. Twenty seconds later that forged wider
    # window would keep the old implementation current because it retained the
    # exact caller object by reference.
    object.__setattr__(caller_policy, "max_age_ms", 60_000)
    assert caller_policy.max_age_ms == 60_000
    assert authority.policy.max_age_ms == 10_000
    assert not runtime._decision_is_current(
        decision,
        identity,
        authority.policy,
        decision.evaluated_at_ms + 20_000,
    )


def test_policy_mutation_after_structural_evaluation_cannot_widen_retained_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    identity = _identity()
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status() + _mcm(pt=publish_time_ms),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    caller_policy = BetfairStreamFreshnessPolicy(max_age_ms=10_000)
    original_evaluate = runtime._freshness.evaluate

    def mutate_caller_after_structural_evaluation(
        identity_arg: BetfairQuoteIdentity,
        *,
        as_of_ms: int,
        policy: BetfairStreamFreshnessPolicy,
    ):
        result = original_evaluate(
            identity_arg,
            as_of_ms=as_of_ms,
            policy=policy,
        )
        object.__setattr__(caller_policy, "max_age_ms", 60_000)
        return result

    monkeypatch.setattr(
        runtime._freshness,
        "evaluate",
        mutate_caller_after_structural_evaluation,
    )

    decision = runtime.evaluate(identity, policy=caller_policy)
    assert (
        decision.verdict
        is BetfairAuthenticatedFreshnessVerdict.FRESH_AUTHENTICATED_PROVIDER_PUBLISH
    )
    authority = auth._ISSUED_DECISIONS[decision]
    assert caller_policy.max_age_ms == 60_000
    assert authority.policy is not caller_policy
    assert authority.policy.max_age_ms == 10_000
    assert not runtime._decision_is_current(
        decision,
        identity,
        authority.policy,
        decision.evaluated_at_ms + 20_000,
    )


def test_public_clock_rebinding_revokes_already_issued_positive_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    _transport_value, _runtime, decision = _fresh_decision(monkeypatch)
    monkeypatch.setattr(auth.time, "time_ns", lambda: 1_010_000_000)

    assert not decision.decision_eligible


def test_clock_rebinding_before_runtime_construction_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    transport, _ = _transport(monkeypatch, _subscription_status())
    subscription = _open(transport)
    monkeypatch.setattr(auth.time, "time_ns", lambda: 1_010_000_000)

    with pytest.raises(BetfairAuthenticatedStreamError, match="wall-clock dispatch changed"):
        BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)


def test_public_monotonic_clock_rebinding_revokes_positive_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    _transport_value, _runtime, decision = _fresh_decision(monkeypatch)
    monkeypatch.setattr(auth.time, "monotonic_ns", lambda: 1)

    assert not decision.decision_eligible


def test_monotonic_clock_rebinding_before_runtime_construction_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    transport, _ = _transport(monkeypatch, _subscription_status())
    subscription = _open(transport)
    monkeypatch.setattr(auth.time, "monotonic_ns", lambda: 1)

    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="monotonic-clock dispatch changed",
    ):
        BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)


def test_disconnect_revokes_already_issued_positive_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _runtime, decision = _fresh_decision(monkeypatch)

    transport.close()
    assert not decision.decision_eligible


def test_subscription_authority_is_bound_to_exact_transport_object_not_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _ = _transport(monkeypatch, _subscription_status())
    subscription = _open(first)
    second, _ = _transport(monkeypatch, b"")

    assert first is not second
    assert first.connection_id == second.connection_id == "conn-1"
    assert first._connection_generation == second._connection_generation == 1
    with pytest.raises(BetfairAuthenticatedStreamError, match="exact active transport object"):
        BetfairAuthenticatedStreamFreshnessRuntime(second, subscription)


def test_second_subscription_on_same_connection_is_rejected_before_network_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, fake = _transport(monkeypatch, _subscription_status())
    first = _open(transport)
    first.assert_issued()
    sent_before = list(fake.sent)

    with pytest.raises(BetfairAuthenticatedStreamError, match="already has an active"):
        _open(transport)

    assert fake.sent == sent_before
    assert transport.is_authenticated
    first.assert_issued()


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


@pytest.mark.parametrize(
    "extra",
    [
        {"connectionClosed": True},
        {"errorCode": "INVALID_INPUT"},
        {"errorMessage": "invalid market subscription"},
        {"error": True},
        {"error": None},
        {"error": 0},
        {"error": "INVALID_INPUT"},
    ],
)
def test_success_with_failure_indicators_never_issues_authority_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    extra: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "op": "status",
        "id": 7,
        "statusCode": "SUCCESS",
        "connectionClosed": False,
    }
    payload.update(extra)
    tail = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"
    transport, fake = _transport(monkeypatch, tail)

    with pytest.raises(BetfairAuthenticatedStreamError, match="not acknowledged SUCCESS"):
        _open(transport)

    assert fake.closed
    assert not transport.is_authenticated


@pytest.mark.parametrize(
    "connection_closed",
    [None, 0, 1, "", "false"],
)
def test_success_requires_exact_false_connection_closed(
    monkeypatch: pytest.MonkeyPatch,
    connection_closed: object,
) -> None:
    payload: dict[str, object] = {
        "op": "status",
        "id": 7,
        "statusCode": "SUCCESS",
        "connectionClosed": connection_closed,
    }
    tail = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"
    transport, fake = _transport(monkeypatch, tail)

    with pytest.raises(BetfairAuthenticatedStreamError, match="not acknowledged SUCCESS"):
        _open(transport)

    assert fake.closed
    assert not transport.is_authenticated


def test_success_without_connection_closed_issues_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, object] = {
        "op": "status",
        "id": 7,
        "statusCode": "SUCCESS",
    }
    tail = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"
    transport, fake = _transport(monkeypatch, tail)

    subscription = _open(transport)

    subscription.assert_issued()
    assert not fake.closed
    assert transport.is_authenticated


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
    transport, fake = _transport(
        monkeypatch,
        _subscription_status() + _mcm(request_id=6),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    with pytest.raises(ValueError, match="another subscription"):
        runtime.read_and_ingest()
    assert fake.closed
    assert not transport.is_authenticated
    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="no longer bound",
    ):
        runtime.evaluate(
            _identity(),
            policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
        )


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

    monkeypatch.setattr(auth, "_MAX_TRACKED_AUTHORITATIVE_QUOTES", 1)
    transport, fake = _transport(
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
    assert fake.closed
    assert not transport.is_authenticated
    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="no longer bound",
    ):
        runtime.evaluate(
            _identity(1),
            policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
        )


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


def test_post_subscription_non_market_frame_closes_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, fake = _transport(
        monkeypatch,
        _subscription_status()
        + _subscription_status(request_id=99),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="accepts only mcm frames",
    ):
        runtime.read_and_ingest()

    assert fake.closed
    assert not transport.is_authenticated
    with pytest.raises(BetfairAuthenticatedStreamError, match="not issued|no longer bound"):
        runtime.read_and_ingest()


def test_semantic_breach_revocation_blocks_concurrent_stale_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    publish_time_ms = time.time_ns() // 1_000_000
    transport, fake = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + _subscription_status(request_id=99),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    decode_entered = Event()
    release_decode = Event()
    evaluation_done = Event()
    read_errors: list[BaseException] = []
    evaluation_results: list[object] = []
    original_decode = auth._decode_exact_transport_frame

    def blocking_decode(frame):
        decode_entered.set()
        assert release_decode.wait(2.0)
        return original_decode(frame)

    monkeypatch.setattr(auth, "_decode_exact_transport_frame", blocking_decode)

    def read_invalid_frame() -> None:
        try:
            runtime.read_and_ingest()
        except BaseException as exc:
            read_errors.append(exc)

    def evaluate_old_evidence() -> None:
        try:
            evaluation_results.append(
                runtime.evaluate(
                    _identity(),
                    policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
                )
            )
        except BaseException as exc:
            evaluation_results.append(exc)
        finally:
            evaluation_done.set()

    reader = Thread(target=read_invalid_frame)
    reader.start()
    assert decode_entered.wait(2.0)

    evaluator = Thread(target=evaluate_old_evidence)
    evaluator.start()
    assert not evaluation_done.wait(0.05)

    release_decode.set()
    reader.join(2.0)
    evaluator.join(2.0)

    assert not reader.is_alive()
    assert not evaluator.is_alive()
    assert len(read_errors) == 1
    assert isinstance(read_errors[0], BetfairAuthenticatedStreamError)
    assert "accepts only mcm frames" in str(read_errors[0])
    assert fake.closed
    assert not transport.is_authenticated
    assert len(evaluation_results) == 1
    assert isinstance(evaluation_results[0], BetfairAuthenticatedStreamError)
    assert "no longer bound" in str(evaluation_results[0])


def test_out_of_band_frame_consumption_is_rejected_without_advancing_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, fake = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + _mcm(pt=publish_time_ms + 1),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert decision.decision_eligible
    sequence_before = transport._frame_sequence

    with pytest.raises(stream.BetfairStreamTransportError, match="owned by another runtime"):
        transport.read_authenticated_frame()

    assert transport._frame_sequence == sequence_before
    assert transport.is_authenticated
    assert not fake.closed
    assert decision.decision_eligible

    runtime.read_and_ingest()
    assert transport._frame_sequence == sequence_before + 1


def test_second_runtime_cannot_claim_same_authenticated_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _ = _transport(monkeypatch, _subscription_status())
    subscription = _open(transport)
    first = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    with pytest.raises(stream.BetfairStreamTransportError, match="reader is already owned"):
        BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    assert first.subscription is subscription
    assert transport.is_authenticated


def test_concurrent_evaluation_waits_for_valid_inflight_frame_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    publish_time_ms = time.time_ns() // 1_000_000
    transport, fake = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + _delta_mcm(pt=publish_time_ms + 1, price=2.2),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    old_decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert old_decision.decision_eligible

    decode_entered = Event()
    release_decode = Event()
    evaluation_done = Event()
    read_errors: list[BaseException] = []
    evaluation_results: list[object] = []
    original_decode = auth._decode_exact_transport_frame

    def blocking_decode(frame):
        decode_entered.set()
        assert release_decode.wait(2.0)
        return original_decode(frame)

    monkeypatch.setattr(auth, "_decode_exact_transport_frame", blocking_decode)

    def read_valid_frame() -> None:
        try:
            runtime.read_and_ingest()
        except BaseException as exc:
            read_errors.append(exc)

    def evaluate_current() -> None:
        try:
            evaluation_results.append(
                runtime.evaluate(
                    _identity(),
                    policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
                )
            )
        except BaseException as exc:
            evaluation_results.append(exc)
        finally:
            evaluation_done.set()

    reader = Thread(target=read_valid_frame)
    reader.start()
    assert decode_entered.wait(2.0)

    evaluator = Thread(target=evaluate_current)
    evaluator.start()
    assert not evaluation_done.wait(0.05)

    release_decode.set()
    reader.join(2.0)
    evaluator.join(2.0)

    assert not reader.is_alive()
    assert not evaluator.is_alive()
    assert read_errors == []
    assert len(evaluation_results) == 1
    current = evaluation_results[0]
    assert isinstance(current, BetfairAuthenticatedFreshnessDecision)
    assert current.decision_eligible
    assert current.evidence_id != old_decision.evidence_id
    assert not old_decision.decision_eligible
    assert transport.is_authenticated
    assert not fake.closed


def test_delayed_app_key_never_becomes_live_decision_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status() + _mcm(pt=publish_time_ms),
        key_class="DELAYED",
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )

    assert decision.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert "app key class is not LIVE" in decision.reason
    assert decision.evidence_id is not None
    assert not decision.decision_eligible
    assert transport.is_authenticated


def test_market_suspension_revokes_previously_fresh_quote_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + _market_status_mcm(
            "SUSPENDED",
            pt=publish_time_ms + 1,
            clk="c2",
        ),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert decision.decision_eligible

    runtime.read_and_ingest()

    assert not decision.decision_eligible
    suspended = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert suspended.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert "market status is not authoritatively OPEN" in suspended.reason
    assert not suspended.decision_eligible


def test_market_reopen_requires_quote_from_new_open_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + _market_status_mcm("SUSPENDED", pt=publish_time_ms + 1, clk="c2")
        + _market_status_mcm("OPEN", pt=publish_time_ms + 2, clk="c3")
        + _delta_mcm(pt=publish_time_ms + 3, clk="c4", price=2.2),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    runtime.read_and_ingest()
    runtime.read_and_ingest()

    reopened_without_quote = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert reopened_without_quote.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert not reopened_without_quote.decision_eligible

    runtime.read_and_ingest()
    refreshed = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert refreshed.decision_eligible


def test_runner_removal_revokes_previously_fresh_quote_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + _runner_status_mcm("REMOVED", pt=publish_time_ms + 1, clk="c2"),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert decision.decision_eligible

    runtime.read_and_ingest()

    assert not decision.decision_eligible
    removed = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert removed.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert "runner status is not authoritatively ACTIVE" in removed.reason
    assert not removed.decision_eligible


def test_runner_reactivation_requires_quote_from_new_active_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + _runner_status_mcm("REMOVED", pt=publish_time_ms + 1, clk="c2")
        + _runner_status_mcm("ACTIVE", pt=publish_time_ms + 2, clk="c3")
        + _delta_mcm(pt=publish_time_ms + 3, clk="c4", price=2.2),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    runtime.read_and_ingest()
    runtime.read_and_ingest()

    reactivated_without_quote = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert (
        reactivated_without_quote.verdict
        is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    )
    assert not reactivated_without_quote.decision_eligible

    runtime.read_and_ingest()
    refreshed = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert refreshed.decision_eligible


def test_live_decision_requires_market_definition_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status() + _mcm(pt=publish_time_ms),
    )
    subscription = open_authenticated_market_subscription(
        transport,
        provider_request_id=7,
        market_filter={"marketIds": ["1.A"]},
        market_data_fields=("EX_LTP",),
        ladder_levels=None,
        heartbeat_ms=5000,
        conflate_ms=0,
    )
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )

    assert decision.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert "EX_MARKET_DEF is required" in decision.reason
    assert not decision.decision_eligible


def test_duplicate_runner_definition_poison_closes_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
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
                "marketDefinition": {
                    "status": "OPEN",
                    "bettingType": "ODDS",
                    "priceLadderDefinition": {"type": "CLASSIC"},
                    "runners": [
                        {"id": 1, "hc": 0, "status": "ACTIVE"},
                        {"id": 1, "hc": 0, "status": "REMOVED"},
                    ],
                },
                "rc": [{"id": 1, "hc": 0, "ltp": 2.0}],
            }
        ],
    }
    frame = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"
    transport, fake = _transport(
        monkeypatch,
        _subscription_status() + frame,
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="duplicate runner identity",
    ):
        runtime.read_and_ingest()

    assert fake.closed
    assert not transport.is_authenticated


def test_market_authority_state_bound_overflow_closes_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    monkeypatch.setattr(auth, "_MAX_TRACKED_MARKET_AUTHORITY", 1)
    publish_time_ms = time.time_ns() // 1_000_000
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
                "id": market_id,
                "img": True,
                "con": False,
                "marketDefinition": {
                    "status": "OPEN",
                    "bettingType": "ODDS",
                    "priceLadderDefinition": {"type": "CLASSIC"},
                    "runners": [{"id": 1, "hc": 0, "status": "ACTIVE"}],
                },
                "rc": [{"id": 1, "hc": 0, "ltp": 2.0}],
            }
            for market_id in ("1.A", "1.B")
        ],
    }
    frame = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"
    transport, fake = _transport(
        monkeypatch,
        _subscription_status() + frame,
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="market-status authority map exceeded",
    ):
        runtime.read_and_ingest()

    assert fake.closed
    assert not transport.is_authenticated


def test_runner_authority_state_bound_overflow_closes_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autosport import betfair_authenticated_stream as auth

    monkeypatch.setattr(auth, "_MAX_TRACKED_RUNNER_AUTHORITY", 1)
    transport, fake = _transport(
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

    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="runner-status authority map exceeded",
    ):
        runtime.read_and_ingest()

    assert fake.closed
    assert not transport.is_authenticated


def test_live_key_with_provider_forced_delay_never_becomes_live_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(
            pt=publish_time_ms,
            conflate_ms=180_000,
        ),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=200_000),
    )

    assert decision.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert "conflation does not match" in decision.reason
    assert decision.evidence_id is not None
    assert not decision.decision_eligible
    assert transport.is_authenticated


def test_provider_conflation_change_revokes_existing_live_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    delayed_delta = {
        "op": "mcm",
        "id": 7,
        "clk": "c2",
        "pt": publish_time_ms + 1,
        "conflateMs": 180_000,
        "heartbeatMs": 5000,
        "mc": [
            {
                "id": "1.A",
                "img": False,
                "con": False,
                "marketDefinition": {
                    "status": "OPEN",
                    "bettingType": "ODDS",
                    "priceLadderDefinition": {"type": "CLASSIC"},
                    "runners": [
                        {"id": 1, "hc": 0, "status": "ACTIVE"},
                    ],
                },
                "rc": [{"id": 1, "hc": 0, "ltp": 2.2}],
            }
        ],
    }
    delayed_frame = (
        json.dumps(delayed_delta, separators=(",", ":")).encode("utf-8")
        + b"\r\n"
    )
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + delayed_frame,
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=200_000),
    )
    assert decision.decision_eligible

    runtime.read_and_ingest()

    assert not decision.decision_eligible
    delayed = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=200_000),
    )
    assert delayed.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert "conflation does not match" in delayed.reason
    assert not delayed.decision_eligible


def test_provider_503_revokes_live_authority_without_disconnect_until_new_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, fake = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + _heartbeat_mcm(
            pt=publish_time_ms + 1,
            clk="hb1",
            status=503,
        )
        + _heartbeat_mcm(
            pt=publish_time_ms + 2,
            clk="hb2",
        )
        + _delta_mcm(
            pt=publish_time_ms + 3,
            clk="c3",
            price=2.2,
        ),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    initial = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert initial.decision_eligible

    runtime.read_and_ingest()
    assert not initial.decision_eligible
    after_503 = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert after_503.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert not after_503.decision_eligible
    assert transport.is_authenticated
    assert not fake.closed

    runtime.read_and_ingest()
    after_healthy_heartbeat = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert (
        after_healthy_heartbeat.verdict
        is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    )
    assert not after_healthy_heartbeat.decision_eligible
    assert transport.is_authenticated
    assert not fake.closed

    runtime.read_and_ingest()
    recovered = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert recovered.decision_eligible
    assert recovered.evidence_id != initial.evidence_id
    assert transport.is_authenticated
    assert not fake.closed


def test_mutating_delayed_identity_to_live_cannot_escalate_authenticated_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status() + _mcm(pt=publish_time_ms),
        key_class="DELAYED",
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    object.__setattr__(transport.identity, "app_key_class", "LIVE")
    assert transport.identity.app_key_class == "LIVE"
    assert transport.authenticated_app_key_class is None

    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=200_000),
    )
    assert decision.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert "authenticated Betfair app key class is not LIVE" in decision.reason
    assert not decision.decision_eligible


def test_mutating_live_identity_after_positive_decision_revokes_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, _runtime, decision = _fresh_decision(monkeypatch)
    assert transport.authenticated_app_key_class == "LIVE"
    assert decision.decision_eligible

    object.__setattr__(transport.identity, "app_key_class", "DELAYED")

    assert transport.authenticated_app_key_class is None
    assert not decision.decision_eligible


def test_suspended_market_drops_active_authority_state_for_endurance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + _market_status_mcm(
            "SUSPENDED",
            pt=publish_time_ms + 1,
            clk="c2",
        ),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    assert runtime._market_status_by_id == {"1.A": "OPEN"}
    assert runtime._runner_status_by_key

    runtime.read_and_ingest()

    assert "1.A" not in runtime._market_status_by_id
    assert "1.A" not in runtime._market_open_sequence
    assert all(key[0] != "1.A" for key in runtime._runner_status_by_key)
    assert all(key[0] != "1.A" for key in runtime._runner_active_sequence)


def test_removed_runner_drops_active_runner_authority_state_for_endurance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + _runner_status_mcm(
            "REMOVED",
            pt=publish_time_ms + 1,
            clk="c2",
        ),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    runner_key = ("1.A", 1, Decimal("0"))
    assert runtime._runner_status_by_key[runner_key] == "ACTIVE"

    runtime.read_and_ingest()

    assert runner_key not in runtime._runner_status_by_key
    assert runner_key not in runtime._runner_active_sequence


def test_line_market_never_becomes_odds_live_decision_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
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
                "marketDefinition": {
                    "status": "OPEN",
                    "bettingType": "LINE",
                    "priceLadderDefinition": {"type": "LINE_RANGE"},
                    "lineMinUnit": 10.0,
                    "lineMaxUnit": 20.0,
                    "lineInterval": 0.5,
                    "runners": [
                        {"id": 1, "hc": 0, "status": "ACTIVE"},
                    ],
                },
                "rc": [{"id": 1, "hc": 0, "ltp": 10.5}],
            }
        ],
    }
    frame = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"
    transport, _ = _transport(
        monkeypatch,
        _subscription_status() + frame,
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )

    assert decision.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert "betting type is not supported" in decision.reason
    assert not decision.decision_eligible


def test_missing_market_betting_type_poison_closes_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
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
                "marketDefinition": {
                    "status": "OPEN",
                    "runners": [
                        {"id": 1, "hc": 0, "status": "ACTIVE"},
                    ],
                },
                "rc": [{"id": 1, "hc": 0, "ltp": 2.0}],
            }
        ],
    }
    frame = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"
    transport, fake = _transport(
        monkeypatch,
        _subscription_status() + frame,
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="bettingType is unsupported",
    ):
        runtime.read_and_ingest()

    assert fake.closed
    assert not transport.is_authenticated


@pytest.mark.parametrize("betting_type", ["LINE", "RANGE"])
def test_non_odds_price_models_do_not_gain_live_decision_authority(
    monkeypatch: pytest.MonkeyPatch,
    betting_type: str,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
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
                "marketDefinition": {
                    "status": "OPEN",
                    "bettingType": betting_type,
                    "priceLadderDefinition": {"type": "LINE_RANGE"},
                    "runners": [
                        {"id": 1, "hc": 0, "status": "ACTIVE"},
                    ],
                },
                "rc": [{"id": 1, "hc": 0, "ltp": 10.5}],
            }
        ],
    }
    frame = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"
    transport, fake = _transport(
        monkeypatch,
        _subscription_status() + frame,
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )

    assert decision.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert "betting type is not supported" in decision.reason
    assert not decision.decision_eligible
    assert transport.is_authenticated
    assert not fake.closed


def test_duplicate_sub_image_market_keeps_unique_highest_provider_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000

    def market_copy(version: int, price: float) -> dict[str, object]:
        return {
            "id": "1.A",
            "img": True,
            "con": False,
            "marketDefinition": {
                "status": "OPEN",
                "bettingType": "ODDS",
                "priceLadderDefinition": {"type": "CLASSIC"},
                "version": version,
                "runners": [
                    {"id": 1, "hc": 0, "status": "ACTIVE"},
                ],
            },
            "rc": [{"id": 1, "hc": 0, "ltp": price}],
        }

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
            market_copy(10, 2.0),
            market_copy(11, 2.2),
        ],
    }
    frame = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"
    transport, fake = _transport(
        monkeypatch,
        _subscription_status() + frame,
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    evidence = runtime.read_and_ingest()

    assert len(evidence) == 1
    assert evidence[0].quote.price == Decimal("2.2")
    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert decision.decision_eligible
    assert transport.is_authenticated
    assert not fake.closed


def test_duplicate_sub_image_equal_version_is_ambiguous_and_closes_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000

    def market_copy(price: float) -> dict[str, object]:
        return {
            "id": "1.A",
            "img": True,
            "con": False,
            "marketDefinition": {
                "status": "OPEN",
                "bettingType": "ODDS",
                "priceLadderDefinition": {"type": "CLASSIC"},
                "version": 10,
                "runners": [
                    {"id": 1, "hc": 0, "status": "ACTIVE"},
                ],
            },
            "rc": [{"id": 1, "hc": 0, "ltp": price}],
        }

    payload = {
        "op": "mcm",
        "id": 7,
        "ct": "SUB_IMAGE",
        "initialClk": "i1",
        "clk": "c1",
        "pt": publish_time_ms,
        "conflateMs": 0,
        "heartbeatMs": 5000,
        "mc": [market_copy(2.0), market_copy(2.2)],
    }
    frame = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\r\n"
    transport, fake = _transport(
        monkeypatch,
        _subscription_status() + frame,
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)

    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="ambiguous equal version",
    ):
        runtime.read_and_ingest()

    assert fake.closed
    assert not transport.is_authenticated


def test_duplicate_market_ids_in_delta_are_not_silently_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    duplicate_delta = {
        "op": "mcm",
        "id": 7,
        "clk": "c2",
        "pt": publish_time_ms + 1,
        "mc": [
            {
                "id": "1.A",
                "img": False,
                "con": False,
                "rc": [{"id": 1, "hc": 0, "ltp": 2.1}],
            },
            {
                "id": "1.A",
                "img": False,
                "con": False,
                "rc": [{"id": 1, "hc": 0, "ltp": 2.2}],
            },
        ],
    }
    delta_frame = (
        json.dumps(duplicate_delta, separators=(",", ":")).encode("utf-8")
        + b"\r\n"
    )
    transport, fake = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(pt=publish_time_ms)
        + delta_frame,
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    with pytest.raises(
        BetfairAuthenticatedStreamError,
        match="only recoverable in SUB_IMAGE",
    ):
        runtime.read_and_ingest()

    assert fake.closed
    assert not transport.is_authenticated


def test_classic_price_ladder_valid_tick_remains_live_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(
            pt=publish_time_ms,
            runners=[{"id": 1, "hc": 0, "ltp": 2.02}],
            ladder_type="CLASSIC",
        ),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )

    assert decision.decision_eligible


def test_classic_price_ladder_invalid_tick_is_not_live_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, fake = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(
            pt=publish_time_ms,
            runners=[{"id": 1, "hc": 0, "ltp": 2.01}],
            ladder_type="CLASSIC",
        ),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )

    assert decision.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert "invalid for the authoritative price ladder" in decision.reason
    assert not decision.decision_eligible
    assert transport.is_authenticated
    assert not fake.closed


def test_finest_price_ladder_accepts_cent_tick_rejected_by_classic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(
            pt=publish_time_ms,
            runners=[{"id": 1, "hc": 0, "ltp": 2.01}],
            ladder_type="FINEST",
        ),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )

    assert decision.decision_eligible


def test_missing_price_ladder_definition_never_becomes_live_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, fake = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(
            pt=publish_time_ms,
            ladder_type=None,
        ),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()

    decision = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )

    assert decision.verdict is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    assert "price ladder is not authoritative" in decision.reason
    assert not decision.decision_eligible
    assert transport.is_authenticated
    assert not fake.closed


def test_price_ladder_change_revokes_old_quote_until_new_price_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish_time_ms = time.time_ns() // 1_000_000
    transport, _ = _transport(
        monkeypatch,
        _subscription_status()
        + _mcm(
            pt=publish_time_ms,
            runners=[{"id": 1, "hc": 0, "ltp": 2.0}],
            ladder_type="CLASSIC",
        )
        + _market_ladder_mcm(
            "FINEST",
            pt=publish_time_ms + 1,
            clk="c2",
        )
        + _delta_mcm(
            pt=publish_time_ms + 2,
            clk="c3",
            price=2.01,
        ),
    )
    subscription = _open(transport)
    runtime = BetfairAuthenticatedStreamFreshnessRuntime(transport, subscription)
    runtime.read_and_ingest()
    initial = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert initial.decision_eligible

    runtime.read_and_ingest()

    assert not initial.decision_eligible
    after_ladder_change = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert (
        after_ladder_change.verdict
        is BetfairAuthenticatedFreshnessVerdict.NOT_AUTHORIZED
    )
    assert "predates the current Betfair market-price semantics" in (
        after_ladder_change.reason
    )
    assert not after_ladder_change.decision_eligible

    runtime.read_and_ingest()
    refreshed = runtime.evaluate(
        _identity(),
        policy=BetfairStreamFreshnessPolicy(max_age_ms=10_000),
    )
    assert refreshed.decision_eligible
    assert refreshed.evidence_id != initial.evidence_id
