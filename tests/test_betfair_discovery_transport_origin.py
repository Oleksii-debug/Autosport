from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import urllib.request as _urllib_request

import pytest

from autosport.betfair_account_identity import build_betfair_authenticated_client
from autosport.betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BETTING_JSON_RPC_ENDPOINT,
    BetfairSessionCredentials,
)
from autosport.betfair_multisport_catalog import (
    BetfairEventType,
    BetfairMarketType,
    build_list_event_types_request,
    build_list_market_types_request,
    parse_event_types_result,
    parse_market_types_result,
)
from autosport.betfair_discovery_provenance import (
    BetfairDiscoveryProvenanceError,
    BetfairDiscoveryVisibilityScope,
    build_betfair_discovery_acquisition_evidence,
)
import autosport.betfair_discovery_transport_origin as origin


FIXED_NOW = datetime(2026, 9, 29, 15, 40, tzinfo=timezone.utc)


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        assert limit >= len(self._payload)
        return self._payload


class _DiscoveryOpener:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def open(self, request, data=None, timeout: float = 0):
        assert data is None
        assert timeout > 0
        body = json.loads(request.data.decode("utf-8"))
        headers = {key.lower(): value for key, value in request.header_items()}
        assert headers["x-application"] == "app-key"
        assert headers["x-authentication"] == "session-token"
        self.calls.append(
            {
                "url": request.full_url,
                "method": body["method"],
                "params": body["params"],
                "id": body["id"],
            }
        )
        if body["method"] == "AccountAPING/v1.0/getAccountDetails":
            assert request.full_url == ACCOUNT_JSON_RPC_ENDPOINT
            result = {
                "currencyCode": "EUR",
                "localeCode": "en",
                "region": "GBR",
                "timezone": "Europe/London",
            }
        elif body["method"] == "SportsAPING/v1.0/listEventTypes":
            assert request.full_url == BETTING_JSON_RPC_ENDPOINT
            result = [
                {
                    "eventType": {"id": "1", "name": "Soccer"},
                    "marketCount": 2,
                }
            ]
        elif body["method"] == "SportsAPING/v1.0/listMarketTypes":
            assert request.full_url == BETTING_JSON_RPC_ENDPOINT
            assert body["params"]["filter"] == {"eventTypeIds": ["1"]}
            result = [{"marketType": "MATCH_ODDS", "marketCount": 2}]
        else:
            raise AssertionError(f"unexpected method {body['method']!r}")
        raw = json.dumps(
            {"jsonrpc": "2.0", "id": body["id"], "result": result},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return _Response(raw)


def _client(monkeypatch: pytest.MonkeyPatch):
    opener = _DiscoveryOpener()

    def fake_do_open(_self, _http_class, request, **_kwargs):
        response = opener.open(
            request,
            timeout=getattr(request, "timeout", 0),
        )
        response.code = 200
        response.msg = "OK"
        return response

    monkeypatch.setattr(
        _urllib_request.AbstractHTTPHandler,
        "do_open",
        fake_do_open,
    )
    client = build_betfair_authenticated_client(
        BetfairSessionCredentials("app-key", "session-token"),
        account_label="caller-label-must-not-be-authority",
    )
    return client, opener


def _live_evidence(monkeypatch: pytest.MonkeyPatch):
    client, opener = _client(monkeypatch)
    event_acquisition = origin.acquire_authenticated_betfair_discovery(
        client,
        build_list_event_types_request(),
    )
    event_types = parse_event_types_result(event_acquisition.result)
    market_acquisition = origin.acquire_authenticated_betfair_discovery(
        client,
        build_list_market_types_request(event_type_ids=("1",)),
    )
    market_types = parse_market_types_result(market_acquisition.result)

    evidence = build_betfair_discovery_acquisition_evidence(
        discovery_run_id="run-auth-origin",
        visibility_scope=BetfairDiscoveryVisibilityScope(
            account_scope_ref=event_acquisition.account_identity.session_context_id,
            application_scope_ref="product-k07-authenticated-application-context",
            key_class="LIVE",
            jurisdiction="UK",
        ),
        event_type_request=event_acquisition.exchange.request,
        event_type_raw_response=event_acquisition.exchange.raw_response,
        event_type_observed_at=event_acquisition.exchange.observed_at,
        event_types=event_types,
        selected_event_type_id="1",
        market_type_request=market_acquisition.exchange.request,
        market_type_raw_response=market_acquisition.exchange.raw_response,
        market_type_observed_at=market_acquisition.exchange.observed_at,
        market_types=market_types,
        max_age_seconds=60,
    )
    return client, opener, evidence, event_acquisition, market_acquisition


def test_caller_supplied_acquisition_does_not_mint_authenticated_transport_origin():
    from datetime import timedelta
    from autosport.betfair_discovery_provenance import (
        build_betfair_discovery_acquisition_evidence,
    )

    t0 = FIXED_NOW
    evidence = build_betfair_discovery_acquisition_evidence(
        discovery_run_id="caller-only",
        visibility_scope=BetfairDiscoveryVisibilityScope(
            account_scope_ref="account-A",
            application_scope_ref="application-A",
            key_class="LIVE",
            jurisdiction="UK",
        ),
        event_type_request=build_list_event_types_request(),
        event_type_raw_response=b"event-types",
        event_type_observed_at=t0,
        event_types=(BetfairEventType("1", "Soccer", 1),),
        selected_event_type_id="1",
        market_type_request=build_list_market_types_request(event_type_ids=("1",)),
        market_type_raw_response=b"market-types",
        market_type_observed_at=t0 + timedelta(seconds=1),
        market_types=(BetfairMarketType("MATCH_ODDS", 1),),
        max_age_seconds=60,
    )

    assessment = origin.assess_betfair_discovery_transport_origin(evidence)

    assert assessment.grants_authenticated_transport_origin_authority is False
    assert assessment.reason == "NO_AUTHENTICATED_TRANSPORT_RECEIPTS"


def test_process_global_urllib_opener_cannot_mint_authenticated_origin(monkeypatch):
    client, canonical_io = _client(monkeypatch)
    hostile_opener = _DiscoveryOpener()
    monkeypatch.setattr(_urllib_request, "_opener", hostile_opener)

    acquisition = origin.acquire_authenticated_betfair_discovery(
        client,
        build_list_event_types_request(),
    )

    assert acquisition.receipt.raw_response_sha256 == (
        acquisition.exchange.raw_response_sha256
    )
    assert origin.is_authoritative_betfair_discovery_transport_receipt(
        acquisition.receipt
    )
    assert hostile_opener.calls == []
    assert [call["method"] for call in canonical_io.calls] == [
        "AccountAPING/v1.0/getAccountDetails",
        "SportsAPING/v1.0/listEventTypes",
    ]


def test_product_owned_result_resolver_redecodes_exact_authenticated_bytes(monkeypatch):
    client, _opener = _client(monkeypatch)
    acquisition = origin.acquire_authenticated_betfair_discovery(
        client,
        build_list_event_types_request(),
    )

    result = origin.resolve_authoritative_betfair_authenticated_discovery_result(
        acquisition
    )

    assert result == acquisition.result
    assert result == [
        {
            "eventType": {"id": "1", "name": "Soccer"},
            "marketCount": 2,
        }
    ]


def test_product_owned_result_resolver_rejects_post_acquisition_raw_mutation(monkeypatch):
    client, _opener = _client(monkeypatch)
    acquisition = origin.acquire_authenticated_betfair_discovery(
        client,
        build_list_event_types_request(),
    )
    forged = json.dumps(
        {"jsonrpc": "2.0", "id": 2, "result": []},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    object.__setattr__(acquisition.exchange, "raw_response", forged)

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="changed after authenticated acquisition",
    ):
        origin.resolve_authoritative_betfair_authenticated_discovery_result(
            acquisition
        )


def test_product_owned_result_resolver_rejects_result_or_decoder_rebinding(monkeypatch):
    client, _opener = _client(monkeypatch)
    acquisition = origin.acquire_authenticated_betfair_discovery(
        client,
        build_list_event_types_request(),
    )
    object.__setattr__(acquisition, "result", [])

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="result changed after acquisition",
    ):
        origin.resolve_authoritative_betfair_authenticated_discovery_result(
            acquisition
        )

    client2, _opener2 = _client(monkeypatch)
    acquisition2 = origin.acquire_authenticated_betfair_discovery(
        client2,
        build_list_event_types_request(),
    )
    monkeypatch.setattr(
        origin._readonly,
        "_decode_json",
        lambda _payload: {"jsonrpc": "2.0", "result": []},
    )
    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="not canonical",
    ):
        origin.resolve_authoritative_betfair_authenticated_discovery_result(
            acquisition2
        )


def test_product_owned_authenticated_discovery_mints_exact_live_origin(monkeypatch):
    _client_obj, opener, evidence, event_acq, market_acq = _live_evidence(monkeypatch)

    assessment = origin.assess_betfair_discovery_transport_origin(
        evidence,
        receipts=(event_acq.receipt, market_acq.receipt),
    )

    assert assessment.grants_authenticated_transport_origin_authority is True
    assert assessment.reason == "AUTHENTICATED_TRANSPORT_ORIGIN_PROVEN"
    assert assessment.bound_exchange_count == assessment.exchange_count == 2
    assert all(
        origin.is_authoritative_betfair_discovery_transport_receipt(receipt)
        for receipt in (event_acq.receipt, market_acq.receipt)
    )
    assert [call["method"] for call in opener.calls] == [
        "AccountAPING/v1.0/getAccountDetails",
        "SportsAPING/v1.0/listEventTypes",
        "AccountAPING/v1.0/getAccountDetails",
        "SportsAPING/v1.0/listMarketTypes",
    ]
    assert event_acq.receipt.transport_authority_ref == (
        market_acq.receipt.transport_authority_ref
    )
    assert event_acq.receipt.raw_response_sha256 == (
        event_acq.exchange.raw_response_sha256
    )


def test_authenticated_origin_rejects_relabelled_account_scope(monkeypatch):
    _client_obj, _opener, evidence, event_acq, market_acq = _live_evidence(
        monkeypatch
    )
    relabelled = replace(
        evidence,
        visibility_scope=replace(
            evidence.visibility_scope,
            account_scope_ref="betfair-session-context:" + ("0" * 64),
        ),
    )

    assessment = origin.assess_betfair_discovery_transport_origin(
        relabelled,
        receipts=(event_acq.receipt, market_acq.receipt),
    )

    assert assessment.grants_authenticated_transport_origin_authority is False
    assert (
        assessment.reason
        == "AUTHENTICATED_TRANSPORT_ACCOUNT_SCOPE_MISMATCH"
    )
    assert assessment.bound_exchange_count == 0
    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="AUTHENTICATED_TRANSPORT_ACCOUNT_SCOPE_MISMATCH",
    ):
        origin.require_betfair_authenticated_transport_origin(
            relabelled,
            receipts=(event_acq.receipt, market_acq.receipt),
        )


def test_captured_requirement_ignores_rebound_public_assessment(monkeypatch):
    _client_obj, _opener, evidence, _event_acq, _market_acq = _live_evidence(
        monkeypatch
    )
    captured_requirement = origin.require_betfair_authenticated_transport_origin
    monkeypatch.setattr(
        origin,
        "assess_betfair_discovery_transport_origin",
        lambda *args, **kwargs: origin.BetfairDiscoveryTransportOriginAssessment(
            True,
            "FORGED",
            0,
            0,
            (),
        ),
    )

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="NO_AUTHENTICATED_TRANSPORT_RECEIPTS",
    ):
        captured_requirement(evidence)


def test_captured_assessment_ignores_rebound_public_receipt_verifier(monkeypatch):
    _client_obj, _opener, evidence, event_acq, market_acq = _live_evidence(
        monkeypatch
    )
    rebuilt_event = origin.BetfairDiscoveryTransportOriginReceipt._issue(
        transport_authority_ref=event_acq.receipt.transport_authority_ref,
        method=event_acq.receipt.method,
        request_sha256=event_acq.receipt.request_sha256,
        raw_response_sha256=event_acq.receipt.raw_response_sha256,
        observed_at_utc=event_acq.receipt.observed_at_utc,
    )
    rebuilt_market = origin.BetfairDiscoveryTransportOriginReceipt._issue(
        transport_authority_ref=market_acq.receipt.transport_authority_ref,
        method=market_acq.receipt.method,
        request_sha256=market_acq.receipt.request_sha256,
        raw_response_sha256=market_acq.receipt.raw_response_sha256,
        observed_at_utc=market_acq.receipt.observed_at_utc,
    )
    captured_assessment = origin.assess_betfair_discovery_transport_origin
    monkeypatch.setattr(
        origin,
        "is_authoritative_betfair_discovery_transport_receipt",
        lambda receipt: True,
    )

    assessment = captured_assessment(
        evidence,
        receipts=(rebuilt_event, rebuilt_market),
    )

    assert assessment.grants_authenticated_transport_origin_authority is False
    assert assessment.reason == "UNISSUED_OR_STALE_AUTHENTICATED_TRANSPORT_RECEIPT"


def test_authenticated_origin_rejects_post_issuance_raw_response_mutation(monkeypatch):
    _client_obj, _opener, evidence, event_acq, market_acq = _live_evidence(
        monkeypatch
    )
    object.__setattr__(
        evidence.event_type_exchange,
        "raw_response",
        b'{"jsonrpc":"2.0","id":999,"result":[]}',
    )

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="changed after authenticated acquisition",
    ):
        origin.assess_betfair_discovery_transport_origin(
            evidence,
            receipts=(event_acq.receipt, market_acq.receipt),
        )


def test_authenticated_origin_rejects_post_issuance_request_mutation(monkeypatch):
    _client_obj, _opener, evidence, event_acq, market_acq = _live_evidence(
        monkeypatch
    )
    object.__setattr__(
        evidence.event_type_exchange,
        "request",
        build_list_market_types_request(event_type_ids=("1",)),
    )

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="changed after authenticated acquisition",
    ):
        origin.assess_betfair_discovery_transport_origin(
            evidence,
            receipts=(event_acq.receipt, market_acq.receipt),
        )


def test_authenticated_origin_rejects_post_issuance_observed_at_mutation(monkeypatch):
    _client_obj, _opener, evidence, event_acq, market_acq = _live_evidence(
        monkeypatch
    )
    object.__setattr__(
        evidence.event_type_exchange,
        "observed_at",
        evidence.event_type_exchange.observed_at + timedelta(microseconds=1),
    )

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="changed after authenticated acquisition",
    ):
        origin.assess_betfair_discovery_transport_origin(
            evidence,
            receipts=(event_acq.receipt, market_acq.receipt),
        )


def test_receipt_constructor_and_matching_copy_cannot_mint_origin(monkeypatch):
    _client_obj, _opener, _evidence, event_acq, _market_acq = _live_evidence(
        monkeypatch
    )

    with pytest.raises(TypeError, match="product-issued"):
        origin.BetfairDiscoveryTransportOriginReceipt()

    try:
        copied = copy.copy(event_acq.receipt)
    except TypeError:
        copied = None
    if copied is not None:
        assert copied == event_acq.receipt
        assert copied is not event_acq.receipt
        assert not origin.is_authoritative_betfair_discovery_transport_receipt(copied)

    rebuilt = origin.BetfairDiscoveryTransportOriginReceipt._issue(
        transport_authority_ref=event_acq.receipt.transport_authority_ref,
        method=event_acq.receipt.method,
        request_sha256=event_acq.receipt.request_sha256,
        raw_response_sha256=event_acq.receipt.raw_response_sha256,
        observed_at_utc=event_acq.receipt.observed_at_utc,
    )
    assert rebuilt == event_acq.receipt
    assert not origin.is_authoritative_betfair_discovery_transport_receipt(rebuilt)


def test_authenticated_origin_revokes_when_client_session_context_mutates(monkeypatch):
    client, _opener, evidence, event_acq, market_acq = _live_evidence(monkeypatch)

    object.__setattr__(client._credentials, "session_token", "rotated-session")

    assert not origin.is_authoritative_betfair_discovery_transport_receipt(
        event_acq.receipt
    )
    assessment = origin.assess_betfair_discovery_transport_origin(
        evidence,
        receipts=(event_acq.receipt, market_acq.receipt),
    )
    assert assessment.grants_authenticated_transport_origin_authority is False
    assert assessment.reason == "UNISSUED_OR_STALE_AUTHENTICATED_TRANSPORT_RECEIPT"


def test_authenticated_origin_requirement_accepts_only_exact_live_receipts(monkeypatch):
    _client_obj, _opener, evidence, event_acq, market_acq = _live_evidence(
        monkeypatch
    )

    origin.require_betfair_authenticated_transport_origin(
        evidence,
        receipts=(event_acq.receipt, market_acq.receipt),
    )

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="authenticated Betfair transport origin is not proven",
    ):
        origin.require_betfair_authenticated_transport_origin(
            evidence,
            receipts=(event_acq.receipt,),
        )


def test_receipt_payload_mutation_revokes_exact_object_authority(monkeypatch):
    _client_obj, _opener, _evidence, event_acq, _market_acq = _live_evidence(
        monkeypatch
    )

    object.__setattr__(event_acq.receipt, "request_sha256", "0" * 64)

    assert not origin.is_authoritative_betfair_discovery_transport_receipt(
        event_acq.receipt
    )
