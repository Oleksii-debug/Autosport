from copy import copy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
import urllib.request as _urllib_request

import pytest

import autosport.betfair_session_origin as origin_subject
import autosport.betfair_standard_limit_admission as subject
from autosport.betfair_account_identity import (
    BetfairAuthenticatedAccountIdentity,
    build_betfair_authenticated_client,
    resolve_betfair_authenticated_account_identity,
)
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_session_origin import (
    BetfairAuthenticatedJurisdiction,
    BetfairLoginJurisdiction,
    BetfairNonInteractiveLoginSecrets,
    bind_betfair_authenticated_jurisdiction,
    cert_login_endpoint,
    login_betfair_noninteractive,
)
from autosport.betfair_standard_limit_admission import (
    BetfairStandardLimitAdmissionError,
    BetfairStandardLimitAdmissionState,
    REVIEW_AVAILABLE_FROM_UTC,
    REVIEW_EXPIRES_AT_UTC,
    assess_betfair_standard_limit_admission,
    is_authoritative_betfair_standard_limit_admission,
    require_authoritative_betfair_standard_limit_admission,
)

T = datetime(2026, 10, 3, 21, 0, tzinfo=timezone.utc)


def authoritative_context(monkeypatch, tmp_path, *, currency="GBP", jurisdiction=BetfairLoginJurisdiction.GLOBAL_COM):
    endpoint = cert_login_endpoint(jurisdiction)
    login_payload = b'{"sessionToken":"session-from-login","loginStatus":"SUCCESS"}'

    class LoginResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def geturl(self):
            return endpoint

        def read(self, limit):
            assert limit > len(login_payload)
            return login_payload

    def fake_login_open(self, request, data=None, timeout=0):
        assert data is None
        assert request.full_url == endpoint
        assert timeout > 0
        return LoginResponse()

    monkeypatch.setattr(
        origin_subject.ssl.SSLContext,
        "load_cert_chain",
        lambda self, certfile, keyfile=None, password=None: None,
    )
    monkeypatch.setattr(
        _urllib_request.OpenerDirector,
        "open",
        fake_login_open,
    )

    secrets = BetfairNonInteractiveLoginSecrets(
        "application-key",
        "user",
        "password",
        tmp_path / "client.crt",
        tmp_path / "client.key",
    )
    session = login_betfair_noninteractive(
        secrets,
        jurisdiction=jurisdiction,
    )

    class AccountResponse:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit):
            assert limit > len(self._payload)
            return self._payload

    class AccountOpener:
        def open(self, request, data=None, timeout=0):
            assert data is None
            assert timeout > 0
            rpc = json.loads(request.data.decode("utf-8"))
            assert rpc["method"] == "AccountAPING/v1.0/getAccountDetails"
            payload = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": rpc["id"],
                    "result": {
                        "currencyCode": currency,
                        "localeCode": "en",
                        "region": "GBR",
                        "timezone": "Europe/London",
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return AccountResponse(payload)

    monkeypatch.setattr(_urllib_request, "_opener", AccountOpener())

    client = build_betfair_authenticated_client(session.credentials)
    identity = resolve_betfair_authenticated_account_identity(client)
    bound = bind_betfair_authenticated_jurisdiction(
        session.origin,
        identity,
        client=client,
    )
    return client, identity, bound


def assess(client, identity, bound, *, size="1", price="2", side="BACK", as_of=T, target=None):
    return assess_betfair_standard_limit_admission(
        client=client,
        account_identity=identity,
        authenticated_jurisdiction=bound,
        side=side,
        size=Decimal(size),
        price=Decimal(price),
        as_of=as_of,
        bet_target_type=target,
    )


def test_standard_gbp_minimum_is_authoritative_candidate(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    result = assess(client, identity, bound, size="1", price="1.01")

    assert result.state is BetfairStandardLimitAdmissionState.ADMISSION_CANDIDATE
    assert result.reason_code == "GBP_STANDARD_MINIMUM_SATISFIED"
    assert result.gross_payout == Decimal("1.01")
    assert result.admission_candidate is True
    assert is_authoritative_betfair_standard_limit_admission(
        result, client=client
    )
    assert require_authoritative_betfair_standard_limit_admission(
        result, client=client
    ) is result
    assert result.provider_acceptance_proven is False
    assert result.market_price_admissibility_proven is False
    assert result.liquidity_reserved is False
    assert result.execution_authorized is False
    assert result.real_money_execution is False


def test_lower_minimum_exact_payout_boundary_is_authoritative(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    result = assess(client, identity, bound, size="0.10", price="100")

    assert result.state is BetfairStandardLimitAdmissionState.ADMISSION_CANDIDATE
    assert result.reason_code == "GBP_LOWER_MINIMUM_PAYOUT_SATISFIED"
    assert result.gross_payout == Decimal("10.00")
    assert is_authoritative_betfair_standard_limit_admission(
        result, client=client
    )


def test_below_both_reviewed_thresholds_is_rejected_not_authoritative(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    result = assess(client, identity, bound, size="0.10", price="99.99")

    assert result.state is BetfairStandardLimitAdmissionState.REJECTED_REVIEWED_CONSTRAINT
    assert result.reason_code == "GBP_SIZE_AND_PAYOUT_BELOW_REVIEWED_MINIMUMS"
    assert not is_authoritative_betfair_standard_limit_admission(
        result, client=client
    )


@pytest.mark.parametrize(
    ("as_of", "reason"),
    [
        (
            datetime(2026, 10, 3, 20, 29, 59, tzinfo=timezone.utc),
            "RULE_REVIEW_NOT_CAUSALLY_AVAILABLE",
        ),
        (
            REVIEW_EXPIRES_AT_UTC,
            "RULE_REVIEW_EXPIRED",
        ),
    ],
)
def test_review_interval_fails_closed(monkeypatch, tmp_path, as_of, reason):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    result = assess(
        client, identity, bound, size="1", price="2", as_of=as_of
    )
    assert result.state is BetfairStandardLimitAdmissionState.UNKNOWN_UNPROVEN
    assert result.reason_code == reason
    assert not is_authoritative_betfair_standard_limit_admission(
        result, client=client
    )


def test_non_gbp_has_no_current_reviewed_generation(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(
        monkeypatch, tmp_path, currency="EUR"
    )
    result = assess(client, identity, bound, size="2", price="2")
    assert result.state is BetfairStandardLimitAdmissionState.UNKNOWN_UNPROVEN
    assert result.reason_code == "CURRENCY_HAS_NO_CURRENT_REVIEWED_GENERATION"


@pytest.mark.parametrize(
    "jurisdiction",
    [
        BetfairLoginJurisdiction.ITALY,
        BetfairLoginJurisdiction.SPAIN,
        BetfairLoginJurisdiction.ROMANIA,
        BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND,
    ],
)
def test_non_global_cert_route_cannot_borrow_global_gbp_review(monkeypatch, tmp_path, jurisdiction):
    client, identity, bound = authoritative_context(
        monkeypatch,
        tmp_path,
        currency="GBP",
        jurisdiction=jurisdiction,
    )
    result = assess(client, identity, bound, size="0.10", price="100")
    assert result.state is BetfairStandardLimitAdmissionState.UNKNOWN_UNPROVEN
    assert result.reason_code == "JURISDICTION_OUTSIDE_REVIEWED_GLOBAL_CERT_SCOPE"
    assert not is_authoritative_betfair_standard_limit_admission(
        result, client=client
    )


@pytest.mark.parametrize("target", ["PAYOUT", "BACKERS_PROFIT"])
def test_target_sizing_never_uses_standard_size_review(monkeypatch, tmp_path, target):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    result = assess(
        client,
        identity,
        bound,
        size="0.01",
        price="1000",
        target=target,
    )
    assert result.state is BetfairStandardLimitAdmissionState.UNKNOWN_UNPROVEN
    assert result.reason_code == "TARGET_SIZING_OUTSIDE_STANDARD_SIZE_CONTRACT"


def test_forged_public_identity_and_jurisdiction_cannot_mint_candidate(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    forged_identity = BetfairAuthenticatedAccountIdentity(
        venue_id=identity.venue_id,
        mode=identity.mode,
        identity_scope=identity.identity_scope,
        session_context_id=identity.session_context_id,
        currency_code=identity.currency_code,
        account_details_sha256=identity.account_details_sha256,
        observed_at=identity.observed_at,
    )
    forged_bound = BetfairAuthenticatedJurisdiction(
        venue_id=bound.venue_id,
        jurisdiction=bound.jurisdiction,
        session_context_id=bound.session_context_id,
        account_identity_id=forged_identity.identity_id,
        session_origin_id=bound.session_origin_id,
    )
    result = assess(client, forged_identity, forged_bound, size="1", price="2")
    assert result.state is BetfairStandardLimitAdmissionState.UNKNOWN_UNPROVEN
    assert result.reason_code == "UPSTREAM_SESSION_AUTHORITY_UNPROVEN"


def test_copy_of_authoritative_result_cannot_copy_authority(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    result = assess(client, identity, bound)
    copied = copy(result)
    assert copied == result
    assert copied is not result
    assert is_authoritative_betfair_standard_limit_admission(result, client=client)
    assert not is_authoritative_betfair_standard_limit_admission(copied, client=client)


def test_result_mutation_invalidates_authority(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    result = assess(client, identity, bound)
    assert is_authoritative_betfair_standard_limit_admission(result, client=client)
    object.__setattr__(result, "price", Decimal("999"))
    assert not is_authoritative_betfair_standard_limit_admission(result, client=client)


def test_session_rotation_revokes_admission_authority(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    result = assess(client, identity, bound)
    assert is_authoritative_betfair_standard_limit_admission(result, client=client)

    client._credentials = BetfairSessionCredentials(
        "application-key",
        "rotated-session",
    )
    assert not is_authoritative_betfair_standard_limit_admission(
        result, client=client
    )


def test_public_review_constant_rebind_cannot_change_captured_rule(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    monkeypatch.setattr(subject, "GBP_MIN_BET_SIZE", Decimal("999"))
    monkeypatch.setattr(subject, "GBP_MIN_BET_PAYOUT", Decimal("999"))
    result = assess(client, identity, bound, size="1", price="1.01")
    assert result.reason_code == "GBP_STANDARD_MINIMUM_SATISFIED"
    assert is_authoritative_betfair_standard_limit_admission(
        result, client=client
    )


def test_semantic_digest_and_generation_are_stable_across_equal_decisions(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    first = assess(client, identity, bound)
    second = assess(client, identity, bound)
    assert first.review_generation_id == second.review_generation_id
    assert first.review_semantic_sha256 == second.review_semantic_sha256
    assert len(first.review_semantic_sha256) == 64
    assert first.review_available_from == REVIEW_AVAILABLE_FROM_UTC
    assert first.review_expires_at == REVIEW_EXPIRES_AT_UTC


def test_arithmetic_is_exact_under_low_ambient_precision(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    with localcontext() as context:
        context.prec = 2
        result = assess(client, identity, bound, size="0.1000000001", price="99.9999999")
    assert result.gross_payout == Decimal("9.99999999999999999")
    assert result.state is BetfairStandardLimitAdmissionState.REJECTED_REVIEWED_CONSTRAINT


@pytest.mark.parametrize(
    "bad",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("0"),
        Decimal("-1"),
        Decimal("1e19"),
        Decimal("1e-19"),
        Decimal("9" * 65),
    ],
)
def test_size_shape_is_strict_and_bounded(bad):
    with pytest.raises(BetfairStandardLimitAdmissionError):
        subject._positive_decimal(bad, "size")


def test_float_ingress_is_rejected_before_authority_work(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    with pytest.raises(BetfairStandardLimitAdmissionError, match="exact positive"):
        assess_betfair_standard_limit_admission(
            client=client,
            account_identity=identity,
            authenticated_jurisdiction=bound,
            side="BACK",
            size=1.0,  # type: ignore[arg-type]
            price=Decimal("2"),
            as_of=T,
        )


def test_naive_decision_time_is_rejected(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)
    with pytest.raises(BetfairStandardLimitAdmissionError, match="timezone-aware"):
        assess(
            client,
            identity,
            bound,
            as_of=datetime(2026, 10, 3, 21, 0),
        )


def test_module_helper_rebind_cannot_change_captured_canonical_arithmetic(monkeypatch, tmp_path):
    client, identity, bound = authoritative_context(monkeypatch, tmp_path)

    monkeypatch.setattr(
        subject,
        "_exact_multiply",
        lambda left, right: Decimal("999999"),
    )
    monkeypatch.setattr(
        subject,
        "_positive_decimal",
        lambda value, field, **kwargs: Decimal("999999"),
    )

    result = assess(client, identity, bound, size="0.10", price="100")
    assert result.gross_payout == Decimal("10.00")
    assert result.reason_code == "GBP_LOWER_MINIMUM_PAYOUT_SATISFIED"
    assert is_authoritative_betfair_standard_limit_admission(
        result, client=client
    )
