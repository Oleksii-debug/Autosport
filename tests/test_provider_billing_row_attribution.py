from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

import autosport.provider_billing_row_attribution as attribution_module
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_provider_billing_inputs import (
    read_betfair_provider_billing_inputs,
)
from autosport.provider_billing_row_attribution import (
    ProviderBillingAttributionError,
    ProviderBillingRowAttributionEvidence,
    resolve_provider_billing_row_attribution,
)


class _Clock:
    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return datetime(2026, 9, 21, 4, 45, self._tick, tzinfo=timezone.utc)


class _Transport:
    def __init__(
        self,
        application_key: str,
        *,
        owner: str = "provider-owner-A",
        amount: int = -499,
        more_available: bool = False,
        duplicate_ref: bool = False,
    ) -> None:
        self.application_key = application_key
        self.owner = owner
        self.amount = amount
        self.more_available = more_available
        self.duplicate_ref = duplicate_ref

    def post(
        self,
        _url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        assert headers["X-Application"] == self.application_key
        assert timeout_seconds > 0
        request = json.loads(body)
        method = request["method"]
        if method == "AccountAPING/v1.0/getAccountDetails":
            result: object = {"currencyCode": "GBP"}
        elif method == "AccountAPING/v1.0/getDeveloperAppKeys":
            result = [
                {
                    "appId": 41,
                    "appName": "autosport",
                    "appVersions": [
                        {
                            "owner": self.owner,
                            "versionId": 7,
                            "version": "1.0",
                            "applicationKey": self.application_key,
                            "delayData": False,
                            "subscriptionRequired": False,
                            "ownerManaged": False,
                            "active": True,
                            "vendorId": "vendor-3",
                        }
                    ],
                }
            ]
        elif method == "AccountAPING/v1.0/getAccountStatement":
            row = {
                "refId": "billing-ref-1",
                "itemDate": "2026-09-20T09:00:00Z",
                "amount": self.amount,
                "balance": 1501,
                "itemClass": "UNKNOWN",
                "itemClassData": {"source": "provider"},
            }
            rows = [row]
            if self.duplicate_ref:
                rows.append({**row, "amount": self.amount - 1})
            result = {
                "accountStatement": rows,
                "moreAvailable": self.more_available,
            }
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _source(
    *,
    owner: str = "provider-owner-A",
    amount: int = -499,
    more_available: bool = False,
    duplicate_ref: bool = False,
):
    application_key = "live-key-123"
    transport = _Transport(
        application_key,
        owner=owner,
        amount=amount,
        more_available=more_available,
        duplicate_ref=duplicate_ref,
    )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials(application_key, "session-secret"),
        transport=transport,
        clock=_Clock(),
        venue_id="betfair",
        account_id="caller-label-is-not-authority",
    )
    return read_betfair_provider_billing_inputs(
        client,
        record_count=10,
        statement_from="2026-09-01T00:00:00Z",
        statement_to="2026-09-21T00:00:00Z",
    )


def test_binds_exact_authenticated_row_but_keeps_allocation_unproven() -> None:
    source = _source()

    evidence = resolve_provider_billing_row_attribution(source, "billing-ref-1")

    assert type(evidence) is ProviderBillingRowAttributionEvidence
    assert evidence.venue_id == "betfair"
    assert evidence.provider_owner == "provider-owner-A"
    assert evidence.app_id == 41
    assert evidence.app_version_id == 7
    assert evidence.currency_code == "GBP"
    assert evidence.row_ref_id == "billing-ref-1"
    assert evidence.row_amount == Decimal("-499")
    assert evidence.row_amount_sign == "NEGATIVE"
    assert evidence.row_item_class == "UNKNOWN"
    assert evidence.attribution_state == "UNPROVEN"
    assert evidence.missing_authorities == (
        "AUTOSPORT_ACTIVITY_NUMERATOR",
        "PROVIDER_WINDOW_TOTAL_DENOMINATOR",
        "COST_APPLICABILITY",
        "ALLOCATION_RULE",
    )
    assert evidence.source_evidence_sha256 == source.evidence_sha256
    assert evidence.statement_request_scope_sha256 == (
        source.statement.request_scope_sha256
    )
    assert evidence.statement_source_payload_sha256 == (
        source.statement.statement_evidence.source_payload_sha256
    )
    assert not hasattr(evidence, "allocated_amount")
    assert not hasattr(evidence, "allocation_fraction")
    assert not hasattr(evidence, "cost_class")
    assert not hasattr(evidence, "known_zero")


def test_row_or_provider_owner_drift_changes_attribution_identity() -> None:
    base = resolve_provider_billing_row_attribution(_source(), "billing-ref-1")
    amount_drift = resolve_provider_billing_row_attribution(
        _source(amount=-500), "billing-ref-1"
    )
    owner_drift = resolve_provider_billing_row_attribution(
        _source(owner="provider-owner-B"), "billing-ref-1"
    )

    assert base.evidence_sha256 != amount_drift.evidence_sha256
    assert base.evidence_sha256 != owner_drift.evidence_sha256
    assert amount_drift.row_amount == Decimal("-500")
    assert owner_drift.provider_owner == "provider-owner-B"


def test_partial_statement_page_remains_explicitly_unproven() -> None:
    evidence = resolve_provider_billing_row_attribution(
        _source(more_available=True), "billing-ref-1"
    )

    assert evidence.statement_more_available is True
    assert evidence.attribution_state == "UNPROVEN"
    assert "PROVIDER_WINDOW_TOTAL_DENOMINATOR" in evidence.missing_authorities


def test_missing_or_duplicate_provider_ref_id_fails_closed() -> None:
    source = _source()
    with pytest.raises(ProviderBillingAttributionError, match="exactly one statement ref_id"):
        resolve_provider_billing_row_attribution(source, "missing-ref")

    duplicate = _source(duplicate_ref=True)
    with pytest.raises(ProviderBillingAttributionError, match="exactly one statement ref_id"):
        resolve_provider_billing_row_attribution(duplicate, "billing-ref-1")


def test_zero_or_positive_provider_amount_does_not_mint_known_zero_cost() -> None:
    zero = resolve_provider_billing_row_attribution(
        _source(amount=0), "billing-ref-1"
    )
    positive = resolve_provider_billing_row_attribution(
        _source(amount=12), "billing-ref-1"
    )

    assert zero.row_amount_sign == "ZERO"
    assert positive.row_amount_sign == "POSITIVE"
    assert zero.attribution_state == positive.attribution_state == "UNPROVEN"
    assert not hasattr(zero, "known_zero")


def test_resolver_keeps_original_source_type_authority_after_global_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    monkeypatch.setattr(
        attribution_module,
        "BetfairProviderBillingInputsObservation",
        object,
    )

    evidence = resolve_provider_billing_row_attribution(source, "billing-ref-1")
    assert evidence.provider_owner == "provider-owner-A"
    assert evidence.attribution_state == "UNPROVEN"


def test_public_evidence_rejects_non_timestamp_observation_fields() -> None:
    evidence = resolve_provider_billing_row_attribution(_source(), "billing-ref-1")

    with pytest.raises(
        ProviderBillingAttributionError,
        match="source_observed_at must be timezone-aware ISO-8601",
    ):
        replace(evidence, source_observed_at="not-a-time")

    with pytest.raises(
        ProviderBillingAttributionError,
        match="row_item_date must be timezone-aware ISO-8601",
    ):
        replace(evidence, row_item_date="2026-09-20T09:00:00")
