from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_commission_applicability import (
    BetfairCommissionApplicabilityError,
    assess_betfair_commission_applicability,
    validate_betfair_commission_applicability_assessment,
)


_FIXED_NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


class _Transport:
    def __init__(self) -> None:
        self._responses = [
            _response(
                {
                    "currencyCode": "GBP",
                    "localeCode": "en",
                    "region": "GBR",
                    "timezone": "Europe/London",
                    "discountRate": 12.5,
                    "pointsBalance": 42,
                },
                1,
            ),
            _response(
                [
                    {
                        "marketId": "1.234",
                        "description": {
                            "marketBaseRate": 5.0,
                            "discountAllowed": True,
                            "regulator": "MR_INT",
                        },
                    }
                ],
                2,
            ),
        ]

    def post(self, _url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        assert headers
        assert body
        assert timeout_seconds > 0
        if not self._responses:
            raise AssertionError("unexpected provider call")
        return self._responses.pop(0)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _client() -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=_Transport(),
        clock=lambda: _FIXED_NOW,
        venue_id="betfair-exchange",
        account_id="account-123",
    )


def _recomputed_assessment_id(assessment) -> str:
    payload = assessment.to_dict()
    payload.pop("assessment_id")
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_assessment_checksum_is_integrity_not_product_issuance_authority() -> None:
    assessment = assess_betfair_commission_applicability(
        _client(),
        market_id="1.234",
    )
    assert validate_betfair_commission_applicability_assessment(assessment) is assessment

    # frozen=True and a public checksum are not authenticity boundaries against direct
    # object mutation. A caller can coherently relabel a negative value and recompute its
    # checksum; validation therefore deliberately promises only canonical negative shape
    # and checksum consistency, never provider-read provenance or product issuance.
    object.__setattr__(assessment, "market_id", "1.caller-relabeled")
    object.__setattr__(assessment, "fee_input_sha256", "f" * 64)
    object.__setattr__(assessment, "assessment_id", _recomputed_assessment_id(assessment))

    assert validate_betfair_commission_applicability_assessment(assessment) is assessment
    assert assessment.prospective_commission_amount_authorized is False
    assert assessment.complete_execution_fee_cost_authorized is False
    assert assessment.provider_write_authorized is False
    assert assessment.real_money_execution_authorized is False

    # Structural self-consistency can never be used to turn the negative prerequisite
    # into positive economic or execution authority, even if the caller recomputes the
    # checksum after mutation.
    object.__setattr__(assessment, "prospective_commission_amount_authorized", True)
    object.__setattr__(assessment, "assessment_id", _recomputed_assessment_id(assessment))
    with pytest.raises(
        BetfairCommissionApplicabilityError,
        match="exceeds the canonical fail-closed applicability boundary",
    ):
        validate_betfair_commission_applicability_assessment(assessment)
