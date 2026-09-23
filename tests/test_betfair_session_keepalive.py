from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

import autosport.betfair_session_keepalive as subject
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_session_keepalive import (
    BetfairApiRouteResolutionStatus if False else BetfairKeepAliveResponse,
)
from autosport.betfair_session_keepalive import (
    BetfairSessionKeepAliveError,
    BetfairSessionKeepAliveObservation,
    is_authoritative_betfair_session_keepalive,
    keep_alive_betfair_session,
    keepalive_endpoint,
    parse_keepalive_response,
    validate_keepalive_success_response,
)
from autosport.betfair_session_origin import (
    BetfairAuthenticatedJurisdiction,
    BetfairLoginJurisdiction,
)


UTC = timezone.utc


@pytest.mark.parametrize(
    ("jurisdiction", "endpoint"),
    (
        (
            BetfairLoginJurisdiction.GLOBAL_COM,
            "https://identitysso.betfair.com/api/keepAlive",
        ),
        (
            BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND,
            "https://identitysso.betfair.com.au/api/keepAlive",
        ),
        (
            BetfairLoginJurisdiction.ITALY,
            "https://identitysso.betfair.it/api/keepAlive",
        ),
        (
            BetfairLoginJurisdiction.SPAIN,
            "https://identitysso.betfair.es/api/keepAlive",
        ),
        (
            BetfairLoginJurisdiction.ROMANIA,
            "https://identitysso.betfair.ro/api/keepAlive",
        ),
    ),
)
def test_documented_keepalive_endpoint_table_is_explicit(jurisdiction, endpoint):
    assert keepalive_endpoint(jurisdiction) == endpoint


def test_non_enum_jurisdiction_cannot_suffix_route_keepalive():
    with pytest.raises(
        BetfairSessionKeepAliveError,
        match="exact BetfairLoginJurisdiction",
    ):
        keepalive_endpoint("SPAIN")  # type: ignore[arg-type]


def test_success_response_is_strict_and_redacts_token_and_product():
    token = "SESSION-SECRET"
    product = "APPLICATION-SECRET"
    payload = json.dumps(
        {
            "token": token,
            "product": product,
            "status": "SUCCESS",
            "error": "",
        },
        separators=(",", ":"),
    ).encode("utf-8")

    response = parse_keepalive_response(payload)

    assert response.token == token
    assert response.product == product
    assert response.status == "SUCCESS"
    assert response.error == ""
    assert response.response_sha256 == sha256(payload).hexdigest()
    assert token not in repr(response)
    assert product not in repr(response)
    assert repr(response).count("<redacted>") == 2


@pytest.mark.parametrize(
    "payload",
    (
        b'{"token":"t","product":"p","status":"SUCCESS","error":"","extra":1}',
        b'{"token":"t","product":"p","status":"SUCCESS"}',
        b'["t","p","SUCCESS",""]',
        b'{"token":"t","token":"other","product":"p","status":"SUCCESS","error":""}',
        b'{"token":"t","product":"p","status":NaN,"error":""}',
    ),
)
def test_response_schema_is_fail_closed(payload):
    with pytest.raises(BetfairSessionKeepAliveError):
        parse_keepalive_response(payload)


@pytest.mark.parametrize(
    "error",
    ("INPUT_VALIDATION_ERROR", "INTERNAL_ERROR", "NO_SESSION"),
)
def test_documented_failure_responses_never_validate_as_success(error):
    response = parse_keepalive_response(
        json.dumps(
            {
                "token": "",
                "product": "",
                "status": "FAIL",
                "error": error,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )

    with pytest.raises(
        BetfairSessionKeepAliveError,
        match="not successful",
    ):
        validate_keepalive_success_response(
            response,
            credentials=BetfairSessionCredentials("app", "session"),
        )


def test_success_must_echo_exact_session_token_and_application_key():
    credentials = BetfairSessionCredentials("expected-app", "expected-token")

    wrong_token = parse_keepalive_response(
        b'{"token":"other-token","product":"expected-app","status":"SUCCESS","error":""}'
    )
    with pytest.raises(
        BetfairSessionKeepAliveError,
        match="token does not match",
    ):
        validate_keepalive_success_response(
            wrong_token,
            credentials=credentials,
        )

    wrong_product = parse_keepalive_response(
        b'{"token":"expected-token","product":"other-app","status":"SUCCESS","error":""}'
    )
    with pytest.raises(
        BetfairSessionKeepAliveError,
        match="product does not match",
    ):
        validate_keepalive_success_response(
            wrong_product,
            credentials=credentials,
        )

    exact = parse_keepalive_response(
        b'{"token":"expected-token","product":"expected-app","status":"SUCCESS","error":""}'
    )
    assert (
        validate_keepalive_success_response(
            exact,
            credentials=credentials,
        )
        is exact
    )


def _forged_jurisdiction(
    jurisdiction: BetfairLoginJurisdiction = BetfairLoginJurisdiction.GLOBAL_COM,
) -> BetfairAuthenticatedJurisdiction:
    return BetfairAuthenticatedJurisdiction(
        venue_id="betfair",
        jurisdiction=jurisdiction,
        session_context_id="betfair-session-context:" + "a" * 64,
        account_identity_id="b" * 64,
        session_origin_id="c" * 64,
    )


def test_caller_constructed_observation_is_never_authoritative():
    value = BetfairSessionKeepAliveObservation(
        venue_id="betfair",
        session_context_id="betfair-session-context:" + "a" * 64,
        jurisdiction=BetfairLoginJurisdiction.GLOBAL_COM,
        endpoint=keepalive_endpoint(BetfairLoginJurisdiction.GLOBAL_COM),
        observed_at=datetime(2026, 9, 24, tzinfo=UTC),
        response_sha256="d" * 64,
    )
    copied = replace(value)

    assert value.observation_id == copied.observation_id
    assert not is_authoritative_betfair_session_keepalive(value)
    assert not is_authoritative_betfair_session_keepalive(copied)
    assert value.provider_write_authorized is False
    assert value.execution_authorized is False
    assert value.funds_authorized is False
    assert value.settlement_authorized is False
    assert value.durable_restart_authority is False


def test_forged_parent_jurisdiction_is_rejected_before_network(monkeypatch):
    def forbidden_transport(*args, **kwargs):
        raise AssertionError("network must not be reached for forged authority")

    monkeypatch.setattr(
        subject.UrllibBetfairKeepAliveTransport,
        "post_keep_alive",
        forbidden_transport,
    )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app", "session")
    )

    with pytest.raises(
        BetfairSessionKeepAliveError,
        match="authenticated jurisdiction authority",
    ):
        keep_alive_betfair_session(
            _forged_jurisdiction(),
            client=client,
        )


def test_public_issuer_has_no_transport_or_clock_injection_seam():
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app", "session")
    )
    forged = _forged_jurisdiction()

    with pytest.raises(TypeError):
        keep_alive_betfair_session(
            forged,
            client=client,
            transport=object(),  # type: ignore[call-arg]
        )
    with pytest.raises(TypeError):
        keep_alive_betfair_session(
            forged,
            client=client,
            clock=lambda: datetime.now(UTC),  # type: ignore[call-arg]
        )


def test_observation_endpoint_cannot_be_relabelled_to_other_jurisdiction():
    with pytest.raises(
        BetfairSessionKeepAliveError,
        match="does not match jurisdiction",
    ):
        BetfairSessionKeepAliveObservation(
            venue_id="betfair",
            session_context_id="betfair-session-context:" + "a" * 64,
            jurisdiction=BetfairLoginJurisdiction.SPAIN,
            endpoint=keepalive_endpoint(BetfairLoginJurisdiction.GLOBAL_COM),
            observed_at=datetime(2026, 9, 24, tzinfo=UTC),
            response_sha256="d" * 64,
        )


def test_module_exposes_no_issuance_registry_or_factory_hook():
    for name in (
        "_ISSUED_KEEPALIVE",
        "_PROCESS_HMAC_KEY",
        "_remember_keepalive",
        "_build_keepalive_authority_runtime",
    ):
        assert not hasattr(subject, name), name


def test_response_type_is_memory_only_and_reconstruction_is_not_authority():
    parsed = BetfairKeepAliveResponse(
        token="token-secret",
        product="product-secret",
        status="SUCCESS",
        error="",
        response_sha256="e" * 64,
    )
    reconstructed = replace(parsed)

    assert reconstructed == parsed
    assert "token-secret" not in repr(reconstructed)
    assert "product-secret" not in repr(reconstructed)
    assert not hasattr(parsed, "execution_authorized")


def test_invalid_success_shape_and_undocumented_failure_fail_closed():
    with pytest.raises(
        BetfairSessionKeepAliveError,
        match="empty error",
    ):
        BetfairKeepAliveResponse(
            token="token",
            product="app",
            status="SUCCESS",
            error="NO_SESSION",
            response_sha256="f" * 64,
        )
    with pytest.raises(
        BetfairSessionKeepAliveError,
        match="undocumented",
    ):
        BetfairKeepAliveResponse(
            token="",
            product="",
            status="FAIL",
            error="SOMETHING_NEW",
            response_sha256="f" * 64,
        )
