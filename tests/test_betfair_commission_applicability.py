from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

import autosport.betfair_commission_applicability as applicability_module

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_commission_applicability import (
    BetfairCommissionApplicabilityAssessment,
    BetfairCommissionApplicabilityError,
    BetfairCommissionApplicabilityReason,
    BetfairCommissionApplicabilityStatus,
    assess_betfair_commission_applicability,
    require_product_betfair_commission_applicability,
)


FIXED_NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected provider call")
        return self.responses.pop(0)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _account_details(*, region: str = "GBR", discount_rate: float = 12.5):
    return {
        "currencyCode": "GBP",
        "localeCode": "en",
        "region": region,
        "timezone": "Europe/London",
        "discountRate": discount_rate,
        "pointsBalance": 42,
        "firstName": "NeverPersist",
        "lastName": "NeverPersist",
    }


def _market_description(*, market_base_rate: float = 5.0, discount_allowed: bool = True):
    return [
        {
            "marketId": "1.234",
            "description": {
                "marketBaseRate": market_base_rate,
                "discountAllowed": discount_allowed,
                "regulator": "MR_INT",
            },
        }
    ]


def _client(*, account=None, market=None):
    transport = FakeTransport(
        [
            _response(_account_details() if account is None else account, 1),
            _response(_market_description() if market is None else market, 2),
        ]
    )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
        venue_id="betfair-exchange",
        account_id="account-123",
    )
    return client, transport


def test_authenticated_fee_inputs_do_not_mint_prospective_commission_authority():
    client, transport = _client()

    assessment = assess_betfair_commission_applicability(client, market_id="1.234")
    assert require_product_betfair_commission_applicability(assessment) is assessment

    assert len(transport.calls) == 2
    assert assessment.status is BetfairCommissionApplicabilityStatus.UNPROVEN
    assert assessment.reasons == (
        BetfairCommissionApplicabilityReason.ACCOUNT_TARIFF_REGIME_UNPROVEN,
        BetfairCommissionApplicabilityReason.EVENT_REGIME_UNPROVEN,
        BetfairCommissionApplicabilityReason.ADDITIONAL_ACCOUNT_CHARGES_UNPROVEN,
        BetfairCommissionApplicabilityReason.TERMINAL_NET_WINNINGS_UNAVAILABLE,
    )
    assert assessment.prospective_commission_amount_authorized is False
    assert assessment.complete_execution_fee_cost_authorized is False
    assert assessment.provider_write_authorized is False
    assert assessment.real_money_execution_authorized is False


def test_rewards_region_and_discount_allowed_are_not_treated_as_tariff_proof():
    client, _ = _client(
        account=_account_details(region="GBR", discount_rate=20.0),
        market=_market_description(market_base_rate=5.0, discount_allowed=True),
    )

    assessment = assess_betfair_commission_applicability(client, market_id="1.234")

    assert assessment.region == "GBR"
    assert assessment.status is BetfairCommissionApplicabilityStatus.UNPROVEN
    assert (
        BetfairCommissionApplicabilityReason.ACCOUNT_TARIFF_REGIME_UNPROVEN
        in assessment.reasons
    )
    assert assessment.prospective_commission_amount_authorized is False


def test_zero_base_rate_does_not_launder_unknown_additional_charges_into_zero_cost():
    client, _ = _client(
        market=_market_description(market_base_rate=0.0, discount_allowed=False),
    )

    assessment = assess_betfair_commission_applicability(client, market_id="1.234")

    assert assessment.status is BetfairCommissionApplicabilityStatus.UNPROVEN
    assert (
        BetfairCommissionApplicabilityReason.ADDITIONAL_ACCOUNT_CHARGES_UNPROVEN
        in assessment.reasons
    )
    assert assessment.complete_execution_fee_cost_authorized is False


def test_exact_provider_payload_changes_change_bound_fee_input_identity():
    client_a, _ = _client(account=_account_details(discount_rate=10.0))
    first = assess_betfair_commission_applicability(client_a, market_id="1.234")

    client_b, _ = _client(account=_account_details(discount_rate=11.0))
    second = assess_betfair_commission_applicability(client_b, market_id="1.234")

    assert first.fee_input_sha256 != second.fee_input_sha256
    assert first.account_source_payload_sha256 != second.account_source_payload_sha256
    assert first.assessment_id != second.assessment_id


def test_assessment_projection_never_persists_credentials_or_account_personal_fields():
    client, _ = _client()
    assessment = assess_betfair_commission_applicability(client, market_id="1.234")

    rendered = json.dumps(assessment.to_dict(), sort_keys=True)

    assert "app-secret" not in rendered
    assert "session-secret" not in rendered
    assert "NeverPersist" not in rendered
    assert assessment.account_id == "account-123"


def test_public_constructor_cannot_mint_assessment():
    with pytest.raises(
        BetfairCommissionApplicabilityError,
        match="product-issued",
    ):
        BetfairCommissionApplicabilityAssessment()


def test_object_new_forgery_cannot_cross_product_issuance_boundary():
    forged = object.__new__(BetfairCommissionApplicabilityAssessment)

    with pytest.raises(
        BetfairCommissionApplicabilityError,
        match="not product-issued",
    ):
        require_product_betfair_commission_applicability(forged)


def test_malformed_provider_input_fails_before_assessment_issuance():
    account = _account_details()
    del account["discountRate"]
    client, transport = _client(account=account)

    with pytest.raises(BetfairReadOnlyError, match="discount_rate_percent is missing"):
        assess_betfair_commission_applicability(client, market_id="1.234")

    assert len(transport.calls) == 1


def test_public_assessment_root_keeps_canonical_reader_and_issuer_after_module_rebinding(
    monkeypatch,
):
    client, transport = _client()

    def forbidden_reader(*_args, **_kwargs):
        raise AssertionError("mutable module reader must not become assessment authority")

    def forbidden_issuer(*_args, **_kwargs):
        raise AssertionError("mutable module issuer must not become assessment authority")

    monkeypatch.setattr(
        applicability_module,
        "read_betfair_execution_fee_inputs",
        forbidden_reader,
    )
    monkeypatch.setattr(
        applicability_module,
        "_issue_assessment",
        forbidden_issuer,
    )

    assessment = assess_betfair_commission_applicability(client, market_id="1.234")

    assert len(transport.calls) == 2
    assert assessment.status is BetfairCommissionApplicabilityStatus.UNPROVEN
    assert require_product_betfair_commission_applicability(assessment) is assessment
    assert assessment.prospective_commission_amount_authorized is False
    assert assessment.complete_execution_fee_cost_authorized is False
