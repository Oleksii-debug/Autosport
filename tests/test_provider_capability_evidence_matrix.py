from dataclasses import fields, replace
import pickle

import pytest

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
from autosport.provider_capability_evidence_matrix import (
    ProviderCapabilityEvidence,
    ProviderCapabilityEvidenceMatrix,
    ProviderCapabilityEvidenceMatrixError,
    ProviderCapabilityTruthGrade,
    build_provider_capability_evidence_matrix,
    issue_provider_capability_evidence,
    validate_capability_matrix_successor,
)

T0 = "2026-09-22T00:00:00+00:00"
T1 = "2026-09-22T00:01:00+00:00"
T2 = "2026-09-22T00:02:00+00:00"
T3 = "2026-09-22T00:03:00+00:00"
T4 = "2026-09-22T00:04:00+00:00"
H1, H2, H3 = "a" * 64, "b" * 64, "c" * 64


def profile(*, account="acct-a", adapter_version="1", place=BookmakerCapabilityState.SUPPORTED):
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id=account,
        adapter_id="betfair-api",
        adapter_version=adapter_version,
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(BookmakerCapability.BALANCE_READ, BookmakerCapabilityState.SUPPORTED),
            BookmakerCapabilityFact(BookmakerCapability.LIVE_QUOTES_READ, BookmakerCapabilityState.SUPPORTED),
            BookmakerCapabilityFact(BookmakerCapability.PLACE_BET, place),
        ),
        observed_at=T0,
        source_ref="profile",
        source_payload_sha256=H1,
    )


def integration(p):
    return bind_bookmaker_integration(
        p,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=T1,
        source_ref="integration",
        source_payload_sha256=H2,
    )


def evidence(
    p,
    cap,
    grade,
    *,
    i=None,
    observed=T2,
    expires=T4,
    quality=None,
    sports=(),
    markets=(),
    environment="production",
    application_mode="live-key-readonly",
):
    i = i or integration(p)
    if grade in {
        ProviderCapabilityTruthGrade.DECLARED_DOCUMENTED,
        ProviderCapabilityTruthGrade.CONFIGURED,
        ProviderCapabilityTruthGrade.DEGRADED_OR_DELAYED,
        ProviderCapabilityTruthGrade.REVOKED_OR_UNAVAILABLE,
    }:
        expires = None
    return issue_provider_capability_evidence(
        capability=cap,
        profile_state=p.state_of(cap),
        grade=grade,
        profile_id=p.profile_id,
        integration_evidence_id=i.evidence_id,
        environment=environment,
        application_mode=application_mode,
        observed_at=observed,
        expires_at=expires,
        evidence_ref=f"evidence://{cap.value}/{grade.value}",
        evidence_sha256=H3,
        endpoint_operation=f"op:{cap.value}",
        sport_scope=sports,
        market_scope=markets,
        quality_constraint=quality,
    )


def matrix(*, p=None, facts=(), version=1, predecessor=None, as_of=T3, environment="production"):
    p = p or profile()
    i = integration(p)
    return build_provider_capability_evidence_matrix(
        p,
        i,
        environment=environment,
        application_mode="live-key-readonly",
        matrix_version=version,
        as_of=as_of,
        matrix_ref=f"matrix-{version}",
        evidence=facts,
        predecessor_matrix_id=predecessor,
    )


def test_supported_profile_alone_stays_unproven_and_never_authorizes_execution():
    m = matrix()
    assert m.fact_for(BookmakerCapability.BALANCE_READ).grade is ProviderCapabilityTruthGrade.UNKNOWN_UNPROVEN
    assert m.fact_for(BookmakerCapability.PLACE_BET).grade is ProviderCapabilityTruthGrade.UNKNOWN_UNPROVEN
    assert m.provider_write_authorized is False
    assert m.execution_authorized is False
    assert m.real_money_execution is False


def test_generic_issuer_cannot_mint_current_read_authority():
    p = profile()
    i = integration(p)
    for grade in (
        ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN,
        ProviderCapabilityTruthGrade.OBSERVED_OPERATIONAL,
    ):
        with pytest.raises(
            ProviderCapabilityEvidenceMatrixError,
            match="sealed upstream provider verification",
        ):
            evidence(
                p,
                BookmakerCapability.BALANCE_READ,
                grade,
                i=i,
            )

def test_read_and_write_grade_categories_cannot_be_swapped():
    p = profile()
    i = integration(p)
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="authenticated-read"):
        evidence(p, BookmakerCapability.PLACE_BET, ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN, i=i)
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="write-permission"):
        evidence(p, BookmakerCapability.BALANCE_READ, ProviderCapabilityTruthGrade.WRITE_PERMISSION_PROVEN, i=i)


def test_current_operational_evidence_requires_expiry():
    p = profile()
    i = integration(p)
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="requires expires_at"):
        evidence(
            p,
            BookmakerCapability.BALANCE_READ,
            ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN,
            i=i,
            expires=None,
        )


def test_expired_evidence_does_not_renew_on_later_matrix_rebuild():
    p = profile()
    i = integration(p)
    f = issue_provider_capability_evidence(
        capability=BookmakerCapability.BALANCE_READ,
        profile_state=p.state_of(BookmakerCapability.BALANCE_READ),
        grade=ProviderCapabilityTruthGrade.CONFIGURED,
        profile_id=p.profile_id,
        integration_evidence_id=i.evidence_id,
        environment="production",
        application_mode="live-key-readonly",
        observed_at=T2,
        expires_at=T3,
        evidence_ref="evidence://balance_read/configured-expiring",
        evidence_sha256=H3,
        endpoint_operation="op:balance_read",
    )
    assert f.expires_at == T3
    m = build_provider_capability_evidence_matrix(
        p, i, environment="production", application_mode="live-key-readonly",
        matrix_version=1, as_of=T4, matrix_ref="restart", evidence=(f,)
    )
    assert not m.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.CONFIGURED}),
        at_time=T4,
    )


def test_delayed_live_quotes_are_not_live_operational():
    p = profile()
    i = integration(p)
    f = evidence(
        p,
        BookmakerCapability.LIVE_QUOTES_READ,
        ProviderCapabilityTruthGrade.DEGRADED_OR_DELAYED,
        i=i,
        quality="provider_delay_seconds=180",
    )
    m = build_provider_capability_evidence_matrix(
        p, i, environment="production", application_mode="delayed-key",
        matrix_version=1, as_of=T3, matrix_ref="delayed", evidence=(f,)
    )
    assert m.qualifies(
        BookmakerCapability.LIVE_QUOTES_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.DEGRADED_OR_DELAYED}),
        at_time=T3,
    )
    assert not m.qualifies(
        BookmakerCapability.LIVE_QUOTES_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.OBSERVED_OPERATIONAL}),
        at_time=T3,
    )


def test_degraded_grade_requires_quality_constraint():
    p = profile()
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="quality_constraint"):
        evidence(
            p,
            BookmakerCapability.LIVE_QUOTES_READ,
            ProviderCapabilityTruthGrade.DEGRADED_OR_DELAYED,
        )


def test_unsupported_profile_rejects_positive_write_evidence():
    p = profile(place=BookmakerCapabilityState.UNSUPPORTED)
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="profile_state=supported"):
        evidence(p, BookmakerCapability.PLACE_BET, ProviderCapabilityTruthGrade.WRITE_PERMISSION_PROVEN)


def test_revoked_never_qualifies_even_if_caller_lists_revoked_as_accepted():
    p = profile()
    f = evidence(p, BookmakerCapability.PLACE_BET, ProviderCapabilityTruthGrade.REVOKED_OR_UNAVAILABLE)
    m = matrix(p=p, facts=(f,))
    assert not m.qualifies(
        BookmakerCapability.PLACE_BET,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.REVOKED_OR_UNAVAILABLE}),
        at_time=T3,
    )


def test_evidence_cannot_transfer_to_other_account_or_adapter_version():
    p1 = profile()
    f = evidence(p1, BookmakerCapability.BALANCE_READ, ProviderCapabilityTruthGrade.CONFIGURED)
    p2 = profile(account="acct-b", adapter_version="2")
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="profile binding mismatch"):
        matrix(p=p2, facts=(f,))


def test_caller_constructed_or_copied_positive_fact_cannot_mint_matrix_authority():
    p = profile()
    issued = evidence(
        p,
        BookmakerCapability.BALANCE_READ,
        ProviderCapabilityTruthGrade.CONFIGURED,
    )
    direct = ProviderCapabilityEvidence(**{
        field.name: getattr(issued, field.name)
        for field in fields(ProviderCapabilityEvidence)
    })
    copied = replace(issued)
    assert direct.evidence_id == issued.evidence_id == copied.evidence_id
    for forged in (direct, copied):
        with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="product-issued exact object"):
            matrix(p=p, facts=(forged,))


def test_same_product_issued_object_mutation_revokes_authority():
    p = profile()
    issued = evidence(
        p,
        BookmakerCapability.BALANCE_READ,
        ProviderCapabilityTruthGrade.CONFIGURED,
    )
    original_evidence_id = issued.evidence_id
    already_built = matrix(p=p, facts=(issued,))
    assert already_built.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.CONFIGURED}),
        at_time=T3,
    )

    object.__setattr__(
        issued,
        "grade",
        ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN,
    )
    object.__setattr__(issued, "expires_at", T4)

    assert issued.evidence_id != original_evidence_id
    assert not already_built.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=frozenset(
            {ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN}
        ),
        at_time=T3,
    )
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="product-issued exact object",
    ):
        matrix(p=p, facts=(issued,))


def test_same_product_issued_object_payload_edit_revokes_weak_grade():
    p = profile()
    issued = evidence(
        p,
        BookmakerCapability.BALANCE_READ,
        ProviderCapabilityTruthGrade.CONFIGURED,
    )
    already_built = matrix(p=p, facts=(issued,))
    object.__setattr__(issued, "evidence_ref", "evidence://tampered")

    assert not already_built.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.CONFIGURED}),
        at_time=T3,
    )
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="product-issued exact object",
    ):
        matrix(p=p, facts=(issued,))



def test_pickle_replay_cannot_recreate_positive_qualification_authority():
    p = profile()
    issued = evidence(
        p,
        BookmakerCapability.BALANCE_READ,
        ProviderCapabilityTruthGrade.CONFIGURED,
    )
    original = matrix(p=p, facts=(issued,))
    replayed = pickle.loads(pickle.dumps(original))
    assert original.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.CONFIGURED}),
        at_time=T3,
    )
    assert not replayed.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.CONFIGURED}),
        at_time=T3,
    )



def test_matrix_is_complete_and_builder_rejects_duplicate_capability():
    m = matrix()
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="exact evidence"):
        replace(m, facts=("not-evidence", *m.facts[1:]))
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="every capability"):
        replace(m, facts=m.facts[:-1])
    p = profile()
    f = evidence(p, BookmakerCapability.BALANCE_READ, ProviderCapabilityTruthGrade.CONFIGURED)
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="duplicate capability"):
        matrix(p=p, facts=(f, f))


def test_future_fact_and_noncanonical_scopes_fail_closed():
    p = profile()
    future = evidence(
        p,
        BookmakerCapability.BALANCE_READ,
        ProviderCapabilityTruthGrade.CONFIGURED,
        observed=T4,
        expires="2026-09-22T00:05:00+00:00",
    )
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="after as_of"):
        matrix(p=p, facts=(future,))
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="must be sorted"):
        evidence(
            p,
            BookmakerCapability.LIVE_QUOTES_READ,
            ProviderCapabilityTruthGrade.OBSERVED_OPERATIONAL,
            sports=("tennis", "football"),
        )
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="must be unique"):
        evidence(
            p,
            BookmakerCapability.LIVE_QUOTES_READ,
            ProviderCapabilityTruthGrade.OBSERVED_OPERATIONAL,
            markets=("match_odds", "match_odds"),
        )


def test_documentation_can_be_recorded_without_account_permission_but_never_mints_write():
    p = profile(place=BookmakerCapabilityState.UNKNOWN)
    i = integration(p)
    documented = evidence(
        p,
        BookmakerCapability.PLACE_BET,
        ProviderCapabilityTruthGrade.DECLARED_DOCUMENTED,
        i=i,
    )
    m = build_provider_capability_evidence_matrix(
        p,
        i,
        environment="production",
        application_mode="live-key-readonly",
        matrix_version=1,
        as_of=T3,
        matrix_ref="matrix-docs",
        evidence=(documented,),
    )
    assert m.qualifies(
        BookmakerCapability.PLACE_BET,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.DECLARED_DOCUMENTED}),
        at_time=T3,
    )
    assert not m.qualifies(
        BookmakerCapability.PLACE_BET,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.WRITE_PERMISSION_PROVEN}),
        at_time=T3,
    )
    assert m.provider_write_authorized is False
    assert m.execution_authorized is False


def test_matrix_digest_is_order_independent_for_supplied_evidence():
    p = profile()
    a = evidence(p, BookmakerCapability.BALANCE_READ, ProviderCapabilityTruthGrade.CONFIGURED)
    b = evidence(p, BookmakerCapability.LIVE_QUOTES_READ, ProviderCapabilityTruthGrade.DECLARED_DOCUMENTED)
    left = matrix(p=p, facts=(a, b))
    right = matrix(p=p, facts=(b, a))
    assert left.to_canonical_dict() == right.to_canonical_dict()
    assert left.matrix_id == right.matrix_id
    assert len(left.matrix_id) == 64


def test_successor_binds_predecessor_exact_version_scope_and_time():
    first = matrix()
    second = matrix(version=2, predecessor=first.matrix_id, as_of=T4)
    validate_capability_matrix_successor(first, second)
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="advance by one"):
        validate_capability_matrix_successor(first, replace(second, matrix_version=3))
    other_scope = matrix(version=2, predecessor=first.matrix_id, as_of=T4, environment="sandbox")
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="scope changed"):
        validate_capability_matrix_successor(first, other_scope)


def test_version_lineage_rules_and_raw_types_fail_closed():
    first = matrix()
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="v1 cannot"):
        replace(first, predecessor_matrix_id="d" * 64)
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="requires predecessor"):
        replace(first, matrix_version=2)
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="positive int"):
        replace(first, matrix_version=True)
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="non-empty frozenset"):
        first.qualifies(
            BookmakerCapability.BALANCE_READ,
            accepted_grades={ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN},
            at_time=T3,
        )


def test_contract_has_no_secret_fields():
    names = {
        f.name
        for cls in (ProviderCapabilityEvidence, ProviderCapabilityEvidenceMatrix)
        for f in fields(cls)
    }
    forbidden = ("password", "secret", "token", "cookie", "credential", "api_key")
    assert not {name for name in names if any(part in name for part in forbidden)}
