from datetime import datetime, timezone

import pytest

import autosport.betdaq_account_readonly as betdaq_account_module
from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqCredentials,
)
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationKind,
    bind_bookmaker_integration,
)
from autosport.bookmaker_capability_lifecycle import (
    BetdaqAuthenticatedCapabilityIssuance,
    CapabilityEvidence,
    issue_betdaq_authenticated_capability_evidence,
)
from autosport.provider_capability_evidence_matrix import (
    ProviderCapabilityEvidenceMatrixError,
    ProviderCapabilityTruthGrade,
    build_provider_capability_evidence_matrix,
    issue_betdaq_authenticated_read_evidence,
    issue_provider_capability_evidence,
)


_BETDAQ_NS = "http://www.GlobalBettingExchange.com/ExternalAPI/"
_BETDAQ_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


class _BetdaqHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self.payload


def _product_issued_betdaq_balance(
    monkeypatch,
    *,
    committed_at: str = "2026-09-21T10:01:00+00:00",
    review_due_at: str = "2026-09-21T11:01:00+00:00",
) -> BetdaqAuthenticatedCapabilityIssuance:
    payload = (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{_BETDAQ_SOAP}" xmlns="{_BETDAQ_NS}">'
        f"<soap:Body><GetAccountBalancesResponse>"
        f'<GetAccountBalancesResult Currency="EUR" Balance="120.02" '
        f'Exposure="-20.01" AvailableFunds="100.01" Credit="0">'
        f'<ReturnStatus Code="0" Description="fixture-status" CallId="fixture-call" />'
        f"</GetAccountBalancesResult></GetAccountBalancesResponse>"
        f"</soap:Body></soap:Envelope>"
    ).encode()

    def opener(request, *, timeout):
        return _BetdaqHttpResponse(payload)

    monkeypatch.setattr(betdaq_account_module, "urlopen", opener)
    client = BetdaqAccountReadOnlyClient(
        BetdaqCredentials("alice", "secret-pass", "app-id"),
        clock=lambda: datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc),
    )
    return issue_betdaq_authenticated_capability_evidence(
        client,
        BookmakerCapability.BALANCE_READ,
        committed_at=committed_at,
        review_due_at=review_due_at,
    )


def test_caller_metadata_cannot_mint_current_read_authority() -> None:
    profile = BookmakerCapabilityProfile(
        venue_id="provider-a",
        account_id="account-a",
        adapter_id="official-api",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at="2026-09-22T00:00:00+00:00",
        source_ref="profile-test",
        source_payload_sha256="a" * 64,
    )
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-22T00:01:00+00:00",
        source_ref="integration-test",
        source_payload_sha256="b" * 64,
    )

    for grade in (
        ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN,
        ProviderCapabilityTruthGrade.OBSERVED_OPERATIONAL,
    ):
        with pytest.raises(
            ProviderCapabilityEvidenceMatrixError,
            match="sealed upstream provider verification",
        ):
            issue_provider_capability_evidence(
                capability=BookmakerCapability.BALANCE_READ,
                profile_state=BookmakerCapabilityState.SUPPORTED,
                grade=grade,
                profile_id=profile.profile_id,
                integration_evidence_id=integration.evidence_id,
                environment="production",
                application_mode="live-key-readonly",
                observed_at="2026-09-22T00:02:00+00:00",
                expires_at="2026-09-22T00:04:00+00:00",
                evidence_ref="caller://claims/current-balance-read",
                evidence_sha256="c" * 64,
                endpoint_operation="get-account-funds",
            )


def test_product_issued_betdaq_lifecycle_can_mint_bounded_authenticated_matrix_fact(
    monkeypatch,
) -> None:
    issuance = _product_issued_betdaq_balance(monkeypatch)
    integration = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:01:30+00:00",
        source_ref="betdaq-secure-api",
        source_payload_sha256="d" * 64,
    )

    fact = issue_betdaq_authenticated_read_evidence(issuance, integration)
    assert fact.grade is ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN
    assert fact.capability is BookmakerCapability.BALANCE_READ
    assert fact.environment == "production"
    assert fact.application_mode == "betdaq-authenticated-readonly"
    assert fact.observed_at == issuance.evidence.observed_at
    assert fact.expires_at == "2026-09-21T11:00:00+00:00"
    assert fact.evidence_sha256 == issuance.evidence.evidence_id

    matrix = build_provider_capability_evidence_matrix(
        issuance.profile,
        integration,
        environment="production",
        application_mode="betdaq-authenticated-readonly",
        matrix_version=1,
        as_of="2026-09-21T11:00:00+00:00",
        matrix_ref="betdaq-authenticated-lifecycle-composition",
        evidence=(fact,),
    )
    accepted = frozenset(
        {ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN}
    )
    assert matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=accepted,
        at_time="2026-09-21T10:59:59+00:00",
    )
    assert not matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=accepted,
        at_time="2026-09-21T11:00:00+00:00",
    )


def test_copied_lifecycle_payload_cannot_mint_authenticated_matrix_fact(
    monkeypatch,
) -> None:
    issuance = _product_issued_betdaq_balance(monkeypatch)
    integration = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:01:30+00:00",
        source_ref="betdaq-secure-api",
        source_payload_sha256="d" * 64,
    )
    copied = CapabilityEvidence(
        **{
            field: getattr(issuance.evidence, field)
            for field in issuance.evidence.__dataclass_fields__
        }
    )
    forged = BetdaqAuthenticatedCapabilityIssuance(issuance.profile, copied)

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="not current product-issued authenticated proof",
    ):
        issue_betdaq_authenticated_read_evidence(forged, integration)


def test_betdaq_lifecycle_requires_official_api_integration(monkeypatch) -> None:
    issuance = _product_issued_betdaq_balance(monkeypatch)
    browser = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION,
        observed_at="2026-09-21T10:01:30+00:00",
        source_ref="browser-automation",
        source_payload_sha256="e" * 64,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="requires official API integration",
    ):
        issue_betdaq_authenticated_read_evidence(issuance, browser)


def test_stale_betdaq_lifecycle_cannot_be_laundered_into_current_matrix_fact(
    monkeypatch,
) -> None:
    issuance = _product_issued_betdaq_balance(
        monkeypatch,
        committed_at="2026-09-21T11:00:00+00:00",
        review_due_at="2026-09-21T12:00:00+00:00",
    )
    integration = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:01:30+00:00",
        source_ref="betdaq-secure-api",
        source_payload_sha256="d" * 64,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="not current product-issued authenticated proof",
    ):
        issue_betdaq_authenticated_read_evidence(issuance, integration)


def test_matrix_fact_expiry_never_outlives_lifecycle_review_boundary(
    monkeypatch,
) -> None:
    issuance = _product_issued_betdaq_balance(
        monkeypatch,
        review_due_at="2026-09-21T10:30:00+00:00",
    )
    integration = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:01:30+00:00",
        source_ref="betdaq-secure-api",
        source_payload_sha256="d" * 64,
    )

    fact = issue_betdaq_authenticated_read_evidence(issuance, integration)
    assert fact.expires_at == "2026-09-21T10:30:00+00:00"

    matrix = build_provider_capability_evidence_matrix(
        issuance.profile,
        integration,
        environment="production",
        application_mode="betdaq-authenticated-readonly",
        matrix_version=1,
        as_of="2026-09-21T10:30:00+00:00",
        matrix_ref="betdaq-review-boundary",
        evidence=(fact,),
    )
    accepted = frozenset({ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN})
    assert matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=accepted,
        at_time="2026-09-21T10:29:59+00:00",
    )
    assert not matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=accepted,
        at_time="2026-09-21T10:30:00+00:00",
    )
