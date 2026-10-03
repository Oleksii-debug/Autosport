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
    CapabilityAvailability,
    CapabilityAvailabilityState,
    CapabilityEvidence,
    CapabilityEvidenceJournal,
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
    observed_minute: int = 0,
    committed_at: str = "2026-09-21T10:01:00+00:00",
    review_due_at: str = "2026-09-21T11:01:00+00:00",
    predecessor_id: str | None = None,
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
        clock=lambda: datetime(
            2026, 9, 21, 10, observed_minute, tzinfo=timezone.utc
        ),
    )
    return issue_betdaq_authenticated_capability_evidence(
        client,
        BookmakerCapability.BALANCE_READ,
        committed_at=committed_at,
        review_due_at=review_due_at,
        predecessor_id=predecessor_id,
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
    assert fact.observed_at == "2026-09-21T10:01:30+00:00"
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


def test_betdaq_matrix_authority_cannot_predate_lifecycle_commit(
    monkeypatch,
) -> None:
    issuance = _product_issued_betdaq_balance(monkeypatch)
    integration = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:00:00+00:00",
        source_ref="betdaq-secure-api",
        source_payload_sha256="d" * 64,
    )
    fact = issue_betdaq_authenticated_read_evidence(issuance, integration)
    assert fact.observed_at == "2026-09-21T10:01:00+00:00"

    matrix = build_provider_capability_evidence_matrix(
        issuance.profile,
        integration,
        environment="production",
        application_mode="betdaq-authenticated-readonly",
        matrix_version=1,
        as_of="2026-09-21T10:02:00+00:00",
        matrix_ref="betdaq-commit-causality",
        evidence=(fact,),
    )
    accepted = frozenset({ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN})
    assert not matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=accepted,
        at_time="2026-09-21T10:00:59+00:00",
    )
    assert matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=accepted,
        at_time="2026-09-21T10:01:00+00:00",
    )


def test_late_integration_cannot_resurrect_expired_betdaq_lifecycle(
    monkeypatch,
) -> None:
    issuance = _product_issued_betdaq_balance(monkeypatch)
    integration = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T11:00:00+00:00",
        source_ref="betdaq-secure-api-late",
        source_payload_sha256="f" * 64,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="became available at or after its expiry",
    ):
        issue_betdaq_authenticated_read_evidence(issuance, integration)


def test_betdaq_revalidation_requires_predecessor_journal_for_matrix_composition(
    monkeypatch,
) -> None:
    first = _product_issued_betdaq_balance(monkeypatch)
    successor = _product_issued_betdaq_balance(
        monkeypatch,
        observed_minute=5,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-21T11:06:00+00:00",
        predecessor_id=first.evidence.evidence_id,
    )
    integration = bind_bookmaker_integration(
        successor.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:06:30+00:00",
        source_ref="betdaq-secure-api-revalidation",
        source_payload_sha256="1" * 64,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="valid lifecycle predecessor chain",
    ):
        issue_betdaq_authenticated_read_evidence(successor, integration)


def test_betdaq_revalidation_composes_after_exact_predecessor_chain_validation(
    monkeypatch,
) -> None:
    first = _product_issued_betdaq_balance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(first.evidence)
    successor = _product_issued_betdaq_balance(
        monkeypatch,
        observed_minute=5,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-21T11:06:00+00:00",
        predecessor_id=first.evidence.evidence_id,
    )
    integration = bind_bookmaker_integration(
        successor.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:06:30+00:00",
        source_ref="betdaq-secure-api-revalidation",
        source_payload_sha256="1" * 64,
    )

    fact = issue_betdaq_authenticated_read_evidence(
        successor,
        integration,
        journal=journal,
    )
    assert fact.grade is ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN
    assert fact.observed_at == "2026-09-21T10:06:30+00:00"
    assert fact.expires_at == "2026-09-21T11:05:00+00:00"
    assert fact.evidence_sha256 == successor.evidence.evidence_id


def test_betdaq_matrix_rejects_noncanonical_lifecycle_journal_type(
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

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="exact CapabilityEvidenceJournal",
    ):
        issue_betdaq_authenticated_read_evidence(
            issuance,
            integration,
            journal=object(),
        )


def test_restart_journal_can_requalify_only_with_fresh_product_issued_successor(
    monkeypatch,
) -> None:
    first = _product_issued_betdaq_balance(monkeypatch)
    durable = CapabilityEvidenceJournal()
    durable.publish(first.evidence)
    restored = CapabilityEvidenceJournal.from_json(durable.to_json())

    successor = _product_issued_betdaq_balance(
        monkeypatch,
        observed_minute=5,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-21T11:06:00+00:00",
        predecessor_id=first.evidence.evidence_id,
    )
    integration = bind_bookmaker_integration(
        successor.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:06:30+00:00",
        source_ref="betdaq-secure-api-restart",
        source_payload_sha256="2" * 64,
    )

    fact = issue_betdaq_authenticated_read_evidence(
        successor,
        integration,
        journal=restored,
    )
    assert fact.evidence_sha256 == successor.evidence.evidence_id
    assert successor.evidence.evidence_id in restored.to_json()

    matrix = build_provider_capability_evidence_matrix(
        successor.profile,
        integration,
        environment="production",
        application_mode="betdaq-authenticated-readonly",
        matrix_version=1,
        as_of="2026-09-21T10:07:00+00:00",
        matrix_ref="betdaq-restart-reacquisition",
        evidence=(fact,),
    )
    assert matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=frozenset(
            {ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN}
        ),
        at_time="2026-09-21T10:07:00+00:00",
    )


def test_rejected_copied_successor_cannot_poison_caller_lifecycle_journal(
    monkeypatch,
) -> None:
    first = _product_issued_betdaq_balance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(first.evidence)
    before = journal.to_json()

    successor = _product_issued_betdaq_balance(
        monkeypatch,
        observed_minute=5,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-21T11:06:00+00:00",
        predecessor_id=first.evidence.evidence_id,
    )
    copied = CapabilityEvidence(
        **{
            field: getattr(successor.evidence, field)
            for field in successor.evidence.__dataclass_fields__
        }
    )
    forged = BetdaqAuthenticatedCapabilityIssuance(successor.profile, copied)
    integration = bind_bookmaker_integration(
        successor.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:06:30+00:00",
        source_ref="betdaq-secure-api-forged-successor",
        source_payload_sha256="3" * 64,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="not current product-issued authenticated proof",
    ):
        issue_betdaq_authenticated_read_evidence(
            forged,
            integration,
            journal=journal,
        )

    assert journal.to_json() == before


def test_rejected_late_successor_cannot_poison_caller_lifecycle_journal(
    monkeypatch,
) -> None:
    first = _product_issued_betdaq_balance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(first.evidence)
    before = journal.to_json()

    successor = _product_issued_betdaq_balance(
        monkeypatch,
        observed_minute=5,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-21T11:06:00+00:00",
        predecessor_id=first.evidence.evidence_id,
    )
    integration = bind_bookmaker_integration(
        successor.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T11:05:00+00:00",
        source_ref="betdaq-secure-api-too-late-successor",
        source_payload_sha256="4" * 64,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="became available at or after its expiry",
    ):
        issue_betdaq_authenticated_read_evidence(
            successor,
            integration,
            journal=journal,
        )

    assert journal.to_json() == before


def test_already_published_exact_lifecycle_evidence_can_compose_matrix_fact(
    monkeypatch,
) -> None:
    issuance = _product_issued_betdaq_balance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(issuance.evidence)
    before = journal.to_json()
    integration = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:01:30+00:00",
        source_ref="betdaq-secure-api-existing-journal",
        source_payload_sha256="5" * 64,
    )

    fact = issue_betdaq_authenticated_read_evidence(
        issuance,
        integration,
        journal=journal,
    )

    assert fact.evidence_sha256 == issuance.evidence.evidence_id
    assert journal.to_json() == before


def test_superseded_lifecycle_evidence_cannot_requalify_matrix_authority(
    monkeypatch,
) -> None:
    first = _product_issued_betdaq_balance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(first.evidence)
    successor = _product_issued_betdaq_balance(
        monkeypatch,
        observed_minute=5,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-21T11:06:00+00:00",
        predecessor_id=first.evidence.evidence_id,
    )
    journal.publish(successor.evidence)
    before = journal.to_json()
    integration = bind_bookmaker_integration(
        first.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:01:30+00:00",
        source_ref="betdaq-secure-api-historical",
        source_payload_sha256="6" * 64,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="superseded by newer same-scope evidence",
    ):
        issue_betdaq_authenticated_read_evidence(
            first,
            integration,
            journal=journal,
        )

    assert journal.to_json() == before


@pytest.mark.parametrize(
    "availability_state",
    [
        CapabilityAvailabilityState.DEGRADED,
        CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE,
    ],
)
def test_negative_runtime_availability_blocks_authenticated_matrix_admission(
    monkeypatch,
    availability_state,
) -> None:
    issuance = _product_issued_betdaq_balance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(issuance.evidence)
    journal.publish_availability(
        CapabilityAvailability(
            evidence_id=issuance.evidence.evidence_id,
            state=availability_state,
            observed_at="2026-09-21T10:01:15+00:00",
            source_ref="betdaq-runtime-health",
            source_payload_sha256="7" * 64,
        )
    )
    before = journal.to_json()
    integration = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:01:30+00:00",
        source_ref="betdaq-secure-api-health-aware",
        source_payload_sha256="8" * 64,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="negative runtime availability",
    ):
        issue_betdaq_authenticated_read_evidence(
            issuance,
            integration,
            journal=journal,
        )

    assert journal.to_json() == before


def test_future_negative_availability_does_not_retroactively_block_matrix_admission(
    monkeypatch,
) -> None:
    issuance = _product_issued_betdaq_balance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(issuance.evidence)
    journal.publish_availability(
        CapabilityAvailability(
            evidence_id=issuance.evidence.evidence_id,
            state=CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE,
            observed_at="2026-09-21T10:02:00+00:00",
            source_ref="betdaq-runtime-health-future",
            source_payload_sha256="9" * 64,
        )
    )
    integration = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:01:30+00:00",
        source_ref="betdaq-secure-api-before-outage",
        source_payload_sha256="a" * 64,
    )

    fact = issue_betdaq_authenticated_read_evidence(
        issuance,
        integration,
        journal=journal,
    )

    assert fact.grade is ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN
    assert fact.observed_at == "2026-09-21T10:01:30+00:00"


def test_caller_available_assertion_does_not_become_required_positive_health_authority(
    monkeypatch,
) -> None:
    issuance = _product_issued_betdaq_balance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(issuance.evidence)
    journal.publish_availability(
        CapabilityAvailability(
            evidence_id=issuance.evidence.evidence_id,
            state=CapabilityAvailabilityState.AVAILABLE,
            observed_at="2026-09-21T10:01:15+00:00",
            source_ref="caller-available-is-audit-only",
            source_payload_sha256="b" * 64,
        )
    )
    integration = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:01:30+00:00",
        source_ref="betdaq-secure-api-independent-read-proof",
        source_payload_sha256="c" * 64,
    )

    fact = issue_betdaq_authenticated_read_evidence(
        issuance,
        integration,
        journal=journal,
    )

    assert fact.grade is ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN


def test_available_audit_record_cannot_launder_prior_outage_into_matrix_authority(
    monkeypatch,
) -> None:
    issuance = _product_issued_betdaq_balance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(issuance.evidence)
    journal.publish_availability(
        CapabilityAvailability(
            evidence_id=issuance.evidence.evidence_id,
            state=CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE,
            observed_at="2026-09-21T10:01:10+00:00",
            source_ref="provider-outage",
            source_payload_sha256="d" * 64,
        )
    )
    journal.publish_availability(
        CapabilityAvailability(
            evidence_id=issuance.evidence.evidence_id,
            state=CapabilityAvailabilityState.AVAILABLE,
            observed_at="2026-09-21T10:01:20+00:00",
            source_ref="caller-claims-recovered",
            source_payload_sha256="e" * 64,
        )
    )
    before = journal.to_json()
    integration = bind_bookmaker_integration(
        issuance.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:01:30+00:00",
        source_ref="betdaq-secure-api-after-claimed-recovery",
        source_payload_sha256="f" * 64,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="negative runtime availability",
    ):
        issue_betdaq_authenticated_read_evidence(
            issuance,
            integration,
            journal=journal,
        )

    assert journal.to_json() == before


def test_fresh_successor_can_recover_matrix_authority_after_old_evidence_outage(
    monkeypatch,
) -> None:
    first = _product_issued_betdaq_balance(monkeypatch)
    journal = CapabilityEvidenceJournal()
    journal.publish(first.evidence)
    journal.publish_availability(
        CapabilityAvailability(
            evidence_id=first.evidence.evidence_id,
            state=CapabilityAvailabilityState.TEMPORARILY_UNAVAILABLE,
            observed_at="2026-09-21T10:02:00+00:00",
            source_ref="old-provider-outage",
            source_payload_sha256="1" * 64,
        )
    )
    successor = _product_issued_betdaq_balance(
        monkeypatch,
        observed_minute=5,
        committed_at="2026-09-21T10:06:00+00:00",
        review_due_at="2026-09-21T11:06:00+00:00",
        predecessor_id=first.evidence.evidence_id,
    )
    integration = bind_bookmaker_integration(
        successor.profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-21T10:06:30+00:00",
        source_ref="betdaq-secure-api-fresh-recovery",
        source_payload_sha256="2" * 64,
    )

    fact = issue_betdaq_authenticated_read_evidence(
        successor,
        integration,
        journal=journal,
    )

    assert fact.grade is ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN
    assert fact.evidence_sha256 == successor.evidence.evidence_id
    assert journal.latest_evidence_id_for(successor.evidence) == successor.evidence.evidence_id
