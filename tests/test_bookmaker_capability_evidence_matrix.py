from dataclasses import fields, replace

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_capability_evidence_matrix import (
    BookmakerCapabilityEvidenceCell,
    BookmakerCapabilityEvidenceError,
    BookmakerCapabilityEvidenceMatrix,
    CapabilityDirection,
    CapabilityEvidenceLevel,
    CapabilityEvidenceSource,
    evaluate_bookmaker_capability_evidence,
    is_product_issued_capability_evidence,
    issue_documented_capability_evidence,
)
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationKind,
    bind_bookmaker_integration,
)


PROFILE_AT = "2026-09-22T10:00:00+00:00"
INTEGRATION_AT = "2026-09-22T10:01:00+00:00"
DOC_AT = "2026-09-22T10:02:00+00:00"
OBSERVED_AT = "2026-09-22T10:03:00+00:00"
EXPIRES_AT = "2026-09-23T10:03:00+00:00"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
DOC_URL = "https://example.invalid/provider/reference/place"


def _profile(
    *,
    account_id: str = "account-a",
    capability: BookmakerCapability = BookmakerCapability.PLACE_BET,
    state: BookmakerCapabilityState = BookmakerCapabilityState.SUPPORTED,
    version: int = 1,
) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="provider-a",
        account_id=account_id,
        adapter_id="adapter-a",
        adapter_version="3.2",
        profile_version=version,
        facts=(BookmakerCapabilityFact(capability, state),),
        observed_at=PROFILE_AT,
        source_ref="canonical-capability-profile",
        source_payload_sha256=HASH_A,
    )


def _integration(profile: BookmakerCapabilityProfile):
    return bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=INTEGRATION_AT,
        source_ref="official-api-adapter",
        source_payload_sha256=HASH_B,
    )


def _documented(
    profile: BookmakerCapabilityProfile | None = None,
    *,
    capability: BookmakerCapability = BookmakerCapability.PLACE_BET,
    operation: str = "placeOrders",
    direction: CapabilityDirection = CapabilityDirection.WRITE,
    sport_scope: str | None = None,
    market_scope: str | None = None,
):
    profile = profile or _profile(capability=capability)
    return issue_documented_capability_evidence(
        profile,
        _integration(profile),
        capability=capability,
        operation=operation,
        direction=direction,
        official_doc_url=DOC_URL,
        doc_observed_at=DOC_AT,
        observed_at=OBSERVED_AT,
        evidence_artifact_sha256=HASH_C,
        expires_or_revalidate_at=EXPIRES_AT,
        sport_scope=sport_scope,
        market_scope=market_scope,
        pagination_semantics="provider-documented",
        rate_semantics="provider-documented",
        provider_status_vocabulary=("FAILURE", "SUCCESS"),
        idempotency_semantics="customer-ref-documented-not-authority",
    )


def _unissued_high_level(
    *,
    level: CapabilityEvidenceLevel = CapabilityEvidenceLevel.EXECUTION_OBSERVED,
    source_kind: CapabilityEvidenceSource = CapabilityEvidenceSource.EXECUTION_RECEIPT,
) -> BookmakerCapabilityEvidenceCell:
    read_source = source_kind in {
        CapabilityEvidenceSource.AUTHENTICATED_ACCOUNT_READ,
        CapabilityEvidenceSource.LIVE_MARKET_READ,
    }
    capability = (
        BookmakerCapability.LIVE_QUOTES_READ
        if read_source
        else BookmakerCapability.PLACE_BET
    )
    direction = (
        CapabilityDirection.READ if read_source else CapabilityDirection.WRITE
    )
    operation = "listMarketBook" if read_source else "placeOrders"
    profile = _profile(capability=capability)
    integration = _integration(profile)
    return BookmakerCapabilityEvidenceCell(
        provider_id=profile.venue_id,
        account_id=profile.account_id,
        adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
        integration_kind=integration.integration_kind,
        capability=capability,
        technical_state=profile.state_of(capability),
        operation=operation,
        direction=direction,
        source_kind=source_kind,
        evidence_level=level,
        observed_at=OBSERVED_AT,
        evidence_artifact_sha256=HASH_C,
        expires_or_revalidate_at=EXPIRES_AT,
        sport_scope=(
            "soccer"
            if level >= CapabilityEvidenceLevel.MARKET_OBSERVED
            else None
        ),
        market_scope=(
            "market-1"
            if level >= CapabilityEvidenceLevel.MARKET_OBSERVED
            else None
        ),
        auth_mode="CERT_SESSION",
        idempotency_semantics=(
            "customerOrderRef"
            if level >= CapabilityEvidenceLevel.EXECUTION_OBSERVED
            else None
        ),
        settlement_revision_semantics=(
            "append-only-provider-revisions"
            if level is CapabilityEvidenceLevel.RECONCILED
            else None
        ),
    )


def test_documented_write_endpoint_is_only_l1_and_never_execution_authority():
    cell = _documented()
    evaluation = evaluate_bookmaker_capability_evidence(
        cell,
        as_of="2026-09-22T11:00:00Z",
    )

    assert is_product_issued_capability_evidence(cell) is True
    assert cell.evidence_level is CapabilityEvidenceLevel.DOCUMENTED
    assert evaluation.effective_level is CapabilityEvidenceLevel.DOCUMENTED
    assert evaluation.execution_authorized is False
    assert cell.execution_authorized is False


def test_supported_profile_does_not_upgrade_documentation_to_account_observed():
    cell = _documented()
    assert cell.technical_state is BookmakerCapabilityState.SUPPORTED
    assert (
        evaluate_bookmaker_capability_evidence(
            cell,
            as_of="2026-09-22T11:00:00+00:00",
        ).effective_level
        is CapabilityEvidenceLevel.DOCUMENTED
    )


def test_unsupported_account_profile_can_still_record_provider_documentation():
    profile = _profile(state=BookmakerCapabilityState.UNSUPPORTED)
    cell = _documented(profile)
    evaluation = evaluate_bookmaker_capability_evidence(
        cell,
        as_of="2026-09-22T11:00:00+00:00",
    )

    assert cell.technical_state is BookmakerCapabilityState.UNSUPPORTED
    assert evaluation.effective_level is CapabilityEvidenceLevel.DOCUMENTED


def test_direct_caller_construction_cannot_mint_high_level_authority():
    cell = _unissued_high_level()
    evaluation = evaluate_bookmaker_capability_evidence(
        cell,
        as_of="2026-09-22T11:00:00+00:00",
    )

    assert cell.evidence_level is CapabilityEvidenceLevel.EXECUTION_OBSERVED
    assert is_product_issued_capability_evidence(cell) is False
    assert evaluation.effective_level is CapabilityEvidenceLevel.UNKNOWN
    assert evaluation.reason == "UNISSUED_EVIDENCE"


def test_reconstructed_documentation_copy_loses_product_issuance():
    cell = _documented()
    copied = replace(cell)

    assert copied == cell
    assert copied.evidence_id == cell.evidence_id
    assert is_product_issued_capability_evidence(cell) is True
    assert is_product_issued_capability_evidence(copied) is False
    assert (
        evaluate_bookmaker_capability_evidence(
            copied,
            as_of="2026-09-22T11:00:00Z",
        ).effective_level
        is CapabilityEvidenceLevel.UNKNOWN
    )


def test_stale_documentation_downgrades_to_unknown_at_exact_expiry():
    cell = _documented()
    before = evaluate_bookmaker_capability_evidence(
        cell,
        as_of="2026-09-23T10:02:59+00:00",
    )
    at_expiry = evaluate_bookmaker_capability_evidence(
        cell,
        as_of=EXPIRES_AT,
    )

    assert before.effective_level is CapabilityEvidenceLevel.DOCUMENTED
    assert at_expiry.effective_level is CapabilityEvidenceLevel.UNKNOWN
    assert at_expiry.reason == "STALE_REVALIDATION_REQUIRED"


def test_evidence_cannot_be_backdated_before_product_observation():
    cell = _documented()
    evaluation = evaluate_bookmaker_capability_evidence(
        cell,
        as_of="2026-09-22T10:02:59+00:00",
    )

    assert evaluation.effective_level is CapabilityEvidenceLevel.UNKNOWN
    assert evaluation.reason == "NOT_YET_AVAILABLE"


def test_direction_is_mechanically_bound_to_capability():
    profile = _profile()
    with pytest.raises(
        BookmakerCapabilityEvidenceError,
        match="READ/WRITE direction disagree",
    ):
        issue_documented_capability_evidence(
            profile,
            _integration(profile),
            capability=BookmakerCapability.PLACE_BET,
            operation="placeOrders",
            direction=CapabilityDirection.READ,
            official_doc_url=DOC_URL,
            doc_observed_at=DOC_AT,
            observed_at=OBSERVED_AT,
            evidence_artifact_sha256=HASH_C,
            expires_or_revalidate_at=EXPIRES_AT,
        )


def test_read_observation_source_cannot_substantiate_write_operation():
    profile = _profile()
    integration = _integration(profile)
    with pytest.raises(
        BookmakerCapabilityEvidenceError,
        match="read evidence source cannot substantiate a WRITE",
    ):
        BookmakerCapabilityEvidenceCell(
            provider_id=profile.venue_id,
            account_id=profile.account_id,
            adapter_id=profile.adapter_id,
            adapter_version=profile.adapter_version,
            profile_id=profile.profile_id,
            integration_evidence_id=integration.evidence_id,
            integration_kind=integration.integration_kind,
            capability=BookmakerCapability.PLACE_BET,
            technical_state=BookmakerCapabilityState.SUPPORTED,
            operation="placeOrders",
            direction=CapabilityDirection.WRITE,
            source_kind=CapabilityEvidenceSource.LIVE_MARKET_READ,
            evidence_level=CapabilityEvidenceLevel.MARKET_OBSERVED,
            observed_at=OBSERVED_AT,
            evidence_artifact_sha256=HASH_C,
            expires_or_revalidate_at=EXPIRES_AT,
            sport_scope="soccer",
            market_scope="market-1",
            auth_mode="CERT_SESSION",
        )


def test_documentation_requires_safe_https_source_identity():
    profile = _profile()
    with pytest.raises(BookmakerCapabilityEvidenceError, match="HTTPS URL"):
        issue_documented_capability_evidence(
            profile,
            _integration(profile),
            capability=BookmakerCapability.PLACE_BET,
            operation="placeOrders",
            direction=CapabilityDirection.WRITE,
            official_doc_url="http://user:secret@example.invalid/docs#fragment",
            doc_observed_at=DOC_AT,
            observed_at=OBSERVED_AT,
            evidence_artifact_sha256=HASH_C,
            expires_or_revalidate_at=EXPIRES_AT,
        )


def test_source_kind_and_level_cannot_be_relabelled():
    profile = _profile()
    integration = _integration(profile)
    with pytest.raises(
        BookmakerCapabilityEvidenceError,
        match="mechanically derived",
    ):
        BookmakerCapabilityEvidenceCell(
            provider_id=profile.venue_id,
            account_id=profile.account_id,
            adapter_id=profile.adapter_id,
            adapter_version=profile.adapter_version,
            profile_id=profile.profile_id,
            integration_evidence_id=integration.evidence_id,
            integration_kind=integration.integration_kind,
            capability=BookmakerCapability.PLACE_BET,
            technical_state=BookmakerCapabilityState.SUPPORTED,
            operation="placeOrders",
            direction=CapabilityDirection.WRITE,
            source_kind=CapabilityEvidenceSource.DOCUMENTATION,
            evidence_level=CapabilityEvidenceLevel.EXECUTION_OBSERVED,
            observed_at=OBSERVED_AT,
            evidence_artifact_sha256=HASH_C,
            expires_or_revalidate_at=EXPIRES_AT,
            official_doc_url=DOC_URL,
            doc_observed_at=DOC_AT,
        )


def test_market_level_requires_exact_sport_and_market_scope():
    base = _unissued_high_level(
        level=CapabilityEvidenceLevel.MARKET_OBSERVED,
        source_kind=CapabilityEvidenceSource.LIVE_MARKET_READ,
    )
    values = base.to_canonical_dict()
    assert values["sport_scope"] == "soccer"
    assert values["market_scope"] == "market-1"

    profile = _profile(capability=BookmakerCapability.LIVE_QUOTES_READ)
    integration = _integration(profile)
    with pytest.raises(
        BookmakerCapabilityEvidenceError,
        match="sport_scope and market_scope",
    ):
        BookmakerCapabilityEvidenceCell(
            provider_id=profile.venue_id,
            account_id=profile.account_id,
            adapter_id=profile.adapter_id,
            adapter_version=profile.adapter_version,
            profile_id=profile.profile_id,
            integration_evidence_id=integration.evidence_id,
            integration_kind=integration.integration_kind,
            capability=BookmakerCapability.LIVE_QUOTES_READ,
            technical_state=BookmakerCapabilityState.SUPPORTED,
            operation="listMarketBook",
            direction=CapabilityDirection.READ,
            source_kind=CapabilityEvidenceSource.LIVE_MARKET_READ,
            evidence_level=CapabilityEvidenceLevel.MARKET_OBSERVED,
            observed_at=OBSERVED_AT,
            evidence_artifact_sha256=HASH_C,
            expires_or_revalidate_at=EXPIRES_AT,
            auth_mode="SESSION",
        )


def test_execution_and_reconciliation_require_distinct_semantic_proof():
    execution = _unissued_high_level()
    reconciled = _unissued_high_level(
        level=CapabilityEvidenceLevel.RECONCILED,
        source_kind=CapabilityEvidenceSource.RECONCILIATION,
    )

    assert execution.evidence_level is CapabilityEvidenceLevel.EXECUTION_OBSERVED
    assert reconciled.evidence_level is CapabilityEvidenceLevel.RECONCILED
    assert execution.evidence_id != reconciled.evidence_id
    for cell in (execution, reconciled):
        assert (
            evaluate_bookmaker_capability_evidence(
                cell,
                as_of="2026-09-22T11:00:00Z",
            ).effective_level
            is CapabilityEvidenceLevel.UNKNOWN
        )


def test_reconciled_shape_fails_closed_without_revision_semantics():
    profile = _profile()
    integration = _integration(profile)
    with pytest.raises(
        BookmakerCapabilityEvidenceError,
        match="settlement/revision semantics",
    ):
        BookmakerCapabilityEvidenceCell(
            provider_id=profile.venue_id,
            account_id=profile.account_id,
            adapter_id=profile.adapter_id,
            adapter_version=profile.adapter_version,
            profile_id=profile.profile_id,
            integration_evidence_id=integration.evidence_id,
            integration_kind=integration.integration_kind,
            capability=BookmakerCapability.PLACE_BET,
            technical_state=BookmakerCapabilityState.SUPPORTED,
            operation="placeOrders",
            direction=CapabilityDirection.WRITE,
            source_kind=CapabilityEvidenceSource.RECONCILIATION,
            evidence_level=CapabilityEvidenceLevel.RECONCILED,
            observed_at=OBSERVED_AT,
            evidence_artifact_sha256=HASH_C,
            expires_or_revalidate_at=EXPIRES_AT,
            sport_scope="soccer",
            market_scope="market-1",
            auth_mode="CERT_SESSION",
            idempotency_semantics="customerOrderRef",
        )


def test_matrix_exact_lookup_never_widens_endpoint_account_or_market_scope():
    cell = _documented(sport_scope="soccer", market_scope="market-1")
    matrix = BookmakerCapabilityEvidenceMatrix((cell,))

    exact = matrix.best_exact(
        provider_id="provider-a",
        account_id="account-a",
        adapter_id="adapter-a",
        capability=BookmakerCapability.PLACE_BET,
        operation="placeOrders",
        direction=CapabilityDirection.WRITE,
        as_of="2026-09-22T11:00:00Z",
        sport_scope="soccer",
        market_scope="market-1",
    )
    wrong_account = matrix.best_exact(
        provider_id="provider-a",
        account_id="account-b",
        adapter_id="adapter-a",
        capability=BookmakerCapability.PLACE_BET,
        operation="placeOrders",
        direction=CapabilityDirection.WRITE,
        as_of="2026-09-22T11:00:00Z",
        sport_scope="soccer",
        market_scope="market-1",
    )
    wrong_operation = matrix.best_exact(
        provider_id="provider-a",
        account_id="account-a",
        adapter_id="adapter-a",
        capability=BookmakerCapability.PLACE_BET,
        operation="replaceOrders",
        direction=CapabilityDirection.WRITE,
        as_of="2026-09-22T11:00:00Z",
        sport_scope="soccer",
        market_scope="market-1",
    )
    provider_wide_query = matrix.best_exact(
        provider_id="provider-a",
        account_id="account-a",
        adapter_id="adapter-a",
        capability=BookmakerCapability.PLACE_BET,
        operation="placeOrders",
        direction=CapabilityDirection.WRITE,
        as_of="2026-09-22T11:00:00Z",
    )

    assert exact is not None
    assert exact.effective_level is CapabilityEvidenceLevel.DOCUMENTED
    assert wrong_account is None
    assert wrong_operation is None
    assert provider_wide_query is None


def test_matrix_rejects_duplicate_evidence_identity():
    cell = _documented()
    with pytest.raises(
        BookmakerCapabilityEvidenceError,
        match="duplicate evidence_id",
    ):
        BookmakerCapabilityEvidenceMatrix((cell, cell))


def test_integration_evidence_must_bind_the_exact_profile():
    profile_a = _profile(account_id="account-a")
    profile_b = _profile(account_id="account-b")
    integration_a = _integration(profile_a)

    with pytest.raises(Exception, match="does not match capability profile identity"):
        issue_documented_capability_evidence(
            profile_b,
            integration_a,
            capability=BookmakerCapability.PLACE_BET,
            operation="placeOrders",
            direction=CapabilityDirection.WRITE,
            official_doc_url=DOC_URL,
            doc_observed_at=DOC_AT,
            observed_at=OBSERVED_AT,
            evidence_artifact_sha256=HASH_C,
            expires_or_revalidate_at=EXPIRES_AT,
        )


def test_status_vocabulary_is_canonical_sorted_unique():
    profile = _profile()
    with pytest.raises(BookmakerCapabilityEvidenceError, match="sorted and unique"):
        issue_documented_capability_evidence(
            profile,
            _integration(profile),
            capability=BookmakerCapability.PLACE_BET,
            operation="placeOrders",
            direction=CapabilityDirection.WRITE,
            official_doc_url=DOC_URL,
            doc_observed_at=DOC_AT,
            observed_at=OBSERVED_AT,
            evidence_artifact_sha256=HASH_C,
            expires_or_revalidate_at=EXPIRES_AT,
            provider_status_vocabulary=("SUCCESS", "FAILURE", "SUCCESS"),
        )


def test_contract_contains_no_secret_or_execution_enablement_fields():
    names = {field.name for field in fields(BookmakerCapabilityEvidenceCell)}
    forbidden = {
        "password",
        "session_token",
        "application_key",
        "cookie",
        "credential_secret",
        "private_key",
        "execution_enabled",
        "real_money_enabled",
    }
    assert names.isdisjoint(forbidden)
