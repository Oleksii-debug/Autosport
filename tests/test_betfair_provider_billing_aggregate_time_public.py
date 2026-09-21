from __future__ import annotations

from hashlib import sha256
import json

import pytest

from autosport.betfair_account_readonly import BetfairEvidence, BetfairReadOnlyError
from autosport.betfair_provider_billing_inputs import (
    BetfairAccountStatementPageObservation,
    BetfairDeveloperAppEntitlementObservation,
    BetfairProviderBillingInputsObservation,
)


def _canonical_sha256(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _public_components():
    entitlement = BetfairDeveloperAppEntitlementObservation(
        venue_id="betfair",
        app_id=1,
        app_name="autosport",
        version_id=1,
        version="1.0",
        delay_data=False,
        subscription_required=False,
        owner_managed=True,
        active=True,
        vendor_id=None,
        provider_owner="provider-owner",
        observed_at="2026-09-21T01:00:00Z",
        source_projection_sha256="1" * 64,
    )
    request_scope_sha256 = _canonical_sha256(
        {
            "method": "AccountAPING/v1.0/getAccountStatement",
            "params": {"fromRecord": 0, "recordCount": 100},
        }
    )
    statement = BetfairAccountStatementPageObservation(
        venue_id="betfair",
        currency_code="GBP",
        from_record=0,
        record_count=100,
        statement_from=None,
        statement_to=None,
        request_scope_sha256=request_scope_sha256,
        items=(),
        more_available=False,
        account_details_evidence=BetfairEvidence(
            "2026-09-21T01:00:01Z", "3" * 64
        ),
        statement_evidence=BetfairEvidence(
            "2026-09-21T01:00:02Z", "4" * 64
        ),
    )
    return entitlement, statement


@pytest.mark.parametrize(
    "reconstructed_observed_at",
    [
        "2026-09-21T01:00:01Z",
        "2026-09-21T01:00:03Z",
    ],
    ids=["earlier", "later"],
)
def test_public_dto_reconstruction_cannot_substitute_aggregate_observed_at(
    reconstructed_observed_at: str,
) -> None:
    entitlement, statement = _public_components()

    with pytest.raises(
        BetfairReadOnlyError,
        match="observed_at must equal latest component observation",
    ):
        BetfairProviderBillingInputsObservation(
            entitlement=entitlement,
            statement=statement,
            observed_at=reconstructed_observed_at,
            evidence_sha256="0" * 64,
        )