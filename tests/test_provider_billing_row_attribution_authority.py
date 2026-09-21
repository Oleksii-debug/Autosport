from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_provider_billing_inputs import read_betfair_provider_billing_inputs
from autosport.provider_billing_row_attribution import (
    ProviderBillingRowAttributionEvidence,
    resolve_provider_billing_row_attribution,
)
from autosport.provider_billing_row_attribution_authority import (
    ProviderBillingRowAuthorityError,
    VerifiedProviderBillingRowAuthority,
    resolve_verified_provider_billing_row_attribution,
    validate_verified_provider_billing_row_authority,
    verify_provider_billing_row_attribution,
)


class _Clock:
    def __init__(self) -> None:
        self.tick = 0

    def __call__(self) -> datetime:
        self.tick += 1
        return datetime(2026, 9, 21, 5, 0, self.tick, tzinfo=timezone.utc)


class _Transport:
    def __init__(self, application_key: str) -> None:
        self.application_key = application_key

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
            result = [{
                "appId": 41,
                "appName": "autosport",
                "appVersions": [{
                    "owner": "provider-owner-A",
                    "versionId": 7,
                    "version": "1.0",
                    "applicationKey": self.application_key,
                    "delayData": False,
                    "subscriptionRequired": False,
                    "ownerManaged": False,
                    "active": True,
                    "vendorId": "vendor-3",
                }],
            }]
        elif method == "AccountAPING/v1.0/getAccountStatement":
            result = {
                "accountStatement": [{
                    "refId": "billing-ref-1",
                    "itemDate": "2026-09-20T09:00:00Z",
                    "amount": -499,
                    "balance": 1501,
                    "itemClass": "UNKNOWN",
                    "itemClassData": {"source": "provider"},
                }],
                "moreAvailable": False,
            }
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _source():
    application_key = "live-key-123"
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials(application_key, "session-secret"),
        transport=_Transport(application_key),
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


def _caller_modified_evidence(
    evidence: ProviderBillingRowAttributionEvidence,
) -> ProviderBillingRowAttributionEvidence:
    provider_owner = "caller-selected-owner"
    payload = {
        "schema": "autosport.provider_billing_row_attribution",
        "schema_version": 1,
        "venue_id": evidence.venue_id,
        "provider_owner": provider_owner,
        "app_id": evidence.app_id,
        "app_version_id": evidence.app_version_id,
        "currency_code": evidence.currency_code,
        "source_observed_at": evidence.source_observed_at,
        "source_evidence_sha256": evidence.source_evidence_sha256,
        "statement_request_scope_sha256": evidence.statement_request_scope_sha256,
        "statement_source_payload_sha256": evidence.statement_source_payload_sha256,
        "statement_more_available": evidence.statement_more_available,
        "row": {
            "ref_id": evidence.row_ref_id,
            "item_date": evidence.row_item_date,
            "amount": str(evidence.row_amount),
            "amount_sign": evidence.row_amount_sign,
            "item_class": evidence.row_item_class,
            "item_class_data_sha256": evidence.row_item_class_data_sha256,
        },
        "attribution_state": "UNPROVEN",
        "missing_authorities": list(evidence.missing_authorities),
    }
    digest = sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return ProviderBillingRowAttributionEvidence(
        venue_id=evidence.venue_id,
        provider_owner=provider_owner,
        app_id=evidence.app_id,
        app_version_id=evidence.app_version_id,
        currency_code=evidence.currency_code,
        source_observed_at=evidence.source_observed_at,
        source_evidence_sha256=evidence.source_evidence_sha256,
        statement_request_scope_sha256=evidence.statement_request_scope_sha256,
        statement_source_payload_sha256=evidence.statement_source_payload_sha256,
        statement_more_available=evidence.statement_more_available,
        row_ref_id=evidence.row_ref_id,
        row_item_date=evidence.row_item_date,
        row_amount=evidence.row_amount,
        row_amount_sign=evidence.row_amount_sign,
        row_item_class=evidence.row_item_class,
        row_item_class_data_sha256=evidence.row_item_class_data_sha256,
        attribution_state="UNPROVEN",
        missing_authorities=evidence.missing_authorities,
        evidence_sha256=digest,
    )


def test_verifier_issues_witness_only_for_exact_source_reresolution() -> None:
    source = _source()
    evidence = resolve_provider_billing_row_attribution(source, "billing-ref-1")

    authority = verify_provider_billing_row_attribution(
        source, evidence, "billing-ref-1"
    )

    assert type(authority) is VerifiedProviderBillingRowAuthority
    assert authority.source_evidence_sha256 == source.evidence_sha256
    assert authority.row_ref_id == "billing-ref-1"
    assert authority.evidence_sha256 == evidence.evidence_sha256
    assert validate_verified_provider_billing_row_authority(authority) is authority


def test_caller_modified_exact_type_cannot_pass_product_verifier() -> None:
    source = _source()
    genuine = resolve_provider_billing_row_attribution(source, "billing-ref-1")
    modified = _caller_modified_evidence(genuine)

    assert type(modified) is ProviderBillingRowAttributionEvidence
    assert modified.provider_owner == "caller-selected-owner"
    with pytest.raises(
        ProviderBillingRowAuthorityError,
        match="does not match canonical source re-resolution",
    ):
        verify_provider_billing_row_attribution(source, modified, "billing-ref-1")


def test_rebound_evidence_equality_cannot_bypass_field_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    genuine = resolve_provider_billing_row_attribution(source, "billing-ref-1")
    modified = _caller_modified_evidence(genuine)
    monkeypatch.setattr(
        ProviderBillingRowAttributionEvidence,
        "__eq__",
        lambda _self, _other: True,
    )

    with pytest.raises(
        ProviderBillingRowAuthorityError,
        match="does not match canonical source re-resolution",
    ):
        verify_provider_billing_row_attribution(source, modified, "billing-ref-1")


def test_authority_witness_constructor_is_not_caller_mintable() -> None:
    source = _source()
    evidence = resolve_provider_billing_row_attribution(source, "billing-ref-1")

    with pytest.raises(
        ProviderBillingRowAuthorityError,
        match="must be product-issued",
    ):
        VerifiedProviderBillingRowAuthority(
            source_evidence_sha256=source.evidence_sha256,
            row_ref_id=evidence.row_ref_id,
            evidence_sha256=evidence.evidence_sha256,
        )


def test_object_new_forged_exact_witness_is_not_registered_authority() -> None:
    source = _source()
    evidence, genuine = resolve_verified_provider_billing_row_attribution(
        source, "billing-ref-1"
    )
    forged = object.__new__(VerifiedProviderBillingRowAuthority)
    object.__setattr__(
        forged, "source_evidence_sha256", genuine.source_evidence_sha256
    )
    object.__setattr__(forged, "row_ref_id", genuine.row_ref_id)
    object.__setattr__(forged, "evidence_sha256", evidence.evidence_sha256)

    assert type(forged) is VerifiedProviderBillingRowAuthority
    with pytest.raises(
        ProviderBillingRowAuthorityError,
        match="must be product-issued and registered",
    ):
        validate_verified_provider_billing_row_authority(forged)


def test_registered_witness_field_tamper_is_rejected() -> None:
    source = _source()
    _evidence, authority = resolve_verified_provider_billing_row_attribution(
        source, "billing-ref-1"
    )
    object.__setattr__(authority, "row_ref_id", "caller-rebound-ref")

    with pytest.raises(
        ProviderBillingRowAuthorityError,
        match="no longer matches issued identity",
    ):
        validate_verified_provider_billing_row_authority(authority)


def test_resolve_verified_returns_causally_bound_pair() -> None:
    source = _source()

    evidence, authority = resolve_verified_provider_billing_row_attribution(
        source, "billing-ref-1"
    )

    assert authority.source_evidence_sha256 == evidence.source_evidence_sha256
    assert authority.row_ref_id == evidence.row_ref_id
    assert authority.evidence_sha256 == evidence.evidence_sha256
    assert evidence.attribution_state == "UNPROVEN"
    assert validate_verified_provider_billing_row_authority(authority) is authority


def test_verified_witness_cannot_expose_cost_or_allocation_authority() -> None:
    names = {field.name for field in fields(VerifiedProviderBillingRowAuthority)}

    assert names == {"source_evidence_sha256", "row_ref_id", "evidence_sha256"}
    assert names.isdisjoint({
        "row_amount",
        "allocated_amount",
        "allocation_fraction",
        "cost_class",
        "known_amount",
        "known_zero",
        "not_applicable",
        "complete",
    })
