from dataclasses import fields, replace
import pickle

import pytest

import autosport.provider_capability_evidence_matrix as capability_module
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
    ProviderCapabilityEvidenceMatrixJournal,
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


def test_authority_state_exposes_no_module_level_registry_or_matrix_mint():
    assert (
        capability_module.CAPABILITY_EVIDENCE_TRUST_BOUNDARY
        == "trusted-process-api-provenance-v1"
    )
    for name in (
        "_ISSUED_EVIDENCE",
        "_ISSUED_EVIDENCE_SEALS",
        "_ISSUED_MATRICES",
        "_ISSUED_MATRIX_SEALS",
        "_register_product_matrix",
    ):
        assert not hasattr(capability_module, name)


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
    accepted = frozenset({ProviderCapabilityTruthGrade.CONFIGURED})
    assert m.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=accepted,
        at_time=T2,
    )
    assert not m.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=accepted,
        at_time=T3,
    )
    assert not m.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=accepted,
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
        application_mode="delayed-key",
    )
    m = build_provider_capability_evidence_matrix(
        p, i, environment="production", application_mode="delayed-key",
        matrix_version=1, as_of=T4, matrix_ref="delayed", evidence=(f,)
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
    assert not m.qualifies(
        BookmakerCapability.LIVE_QUOTES_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.DEGRADED_OR_DELAYED}),
        at_time=T4,
    )


def test_degraded_grade_requires_expiry():
    p = profile()
    i = integration(p)
    with pytest.raises(ProviderCapabilityEvidenceMatrixError, match="requires expires_at"):
        evidence(
            p,
            BookmakerCapability.LIVE_QUOTES_READ,
            ProviderCapabilityTruthGrade.DEGRADED_OR_DELAYED,
            i=i,
            expires=None,
            quality="provider_delay_seconds=180",
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
        match="authenticated matrix fact lost lifecycle provenance context",
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


def test_same_product_matrix_as_of_mutation_revokes_qualification():
    p = profile()
    configured = evidence(
        p,
        BookmakerCapability.BALANCE_READ,
        ProviderCapabilityTruthGrade.CONFIGURED,
    )
    issued_matrix = matrix(p=p, facts=(configured,), as_of=T3)
    assert issued_matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.CONFIGURED}),
        at_time=T3,
    )

    object.__setattr__(issued_matrix, "as_of", T4)

    assert not issued_matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.CONFIGURED}),
        at_time=T4,
    )
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="product-issued exact object with unchanged payload",
    ):
        issued_matrix.fact_for(BookmakerCapability.BALANCE_READ)


def test_same_product_matrix_fact_substitution_revokes_qualification():
    p = profile()
    documented = evidence(
        p,
        BookmakerCapability.BALANCE_READ,
        ProviderCapabilityTruthGrade.DECLARED_DOCUMENTED,
    )
    configured = evidence(
        p,
        BookmakerCapability.BALANCE_READ,
        ProviderCapabilityTruthGrade.CONFIGURED,
    )
    issued_matrix = matrix(p=p, facts=(documented,))
    assert not issued_matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.CONFIGURED}),
        at_time=T3,
    )

    object.__setattr__(
        issued_matrix,
        "facts",
        tuple(
            configured
            if fact.capability is BookmakerCapability.BALANCE_READ
            else fact
            for fact in issued_matrix.facts
        ),
    )

    assert not issued_matrix.qualifies(
        BookmakerCapability.BALANCE_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.CONFIGURED}),
        at_time=T3,
    )



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


def test_documentation_can_be_recorded_for_audit_but_cannot_override_unknown_profile():
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
    assert not m.qualifies(
        BookmakerCapability.PLACE_BET,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.DECLARED_DOCUMENTED}),
        at_time=T3,
    )
    assert not m.qualifies(
        BookmakerCapability.PLACE_BET,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.WRITE_PERMISSION_PROVEN}),
        at_time=T3,
    )
    assert m.fact_for(BookmakerCapability.PLACE_BET).grade is (
        ProviderCapabilityTruthGrade.DECLARED_DOCUMENTED
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
        validate_capability_matrix_successor(
            first,
            matrix(version=3, predecessor=first.matrix_id, as_of=T4),
        )
    other_scope = matrix(version=2, predecessor=first.matrix_id, as_of=T4, environment="sandbox")
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="authority scope or integration mechanism changed",
    ):
        validate_capability_matrix_successor(first, other_scope)


def test_successor_rejects_adapter_identity_or_version_drift():
    first_profile = profile()
    first = matrix(p=first_profile)

    changed_adapter = replace(first_profile, adapter_id="different-adapter")
    changed_adapter_matrix = matrix(
        p=changed_adapter,
        version=2,
        predecessor=first.matrix_id,
        as_of=T4,
    )
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="authority scope or integration mechanism changed",
    ):
        validate_capability_matrix_successor(first, changed_adapter_matrix)

    changed_version = replace(first_profile, adapter_version="2")
    changed_version_matrix = matrix(
        p=changed_version,
        version=2,
        predecessor=first.matrix_id,
        as_of=T4,
    )
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="authority scope or integration mechanism changed",
    ):
        validate_capability_matrix_successor(first, changed_version_matrix)


def test_successor_rejects_integration_mechanism_drift():
    p = profile()
    official = integration(p)
    first = build_provider_capability_evidence_matrix(
        p,
        official,
        environment="production",
        application_mode="live-key-readonly",
        matrix_version=1,
        as_of=T3,
        matrix_ref="official-first",
    )
    browser = bind_bookmaker_integration(
        p,
        integration_kind=BookmakerIntegrationKind.BROWSER_AUTOMATION,
        observed_at=T2,
        source_ref="browser-integration",
        source_payload_sha256="d" * 64,
    )
    second = build_provider_capability_evidence_matrix(
        p,
        browser,
        environment="production",
        application_mode="live-key-readonly",
        matrix_version=2,
        as_of=T4,
        matrix_ref="browser-second",
        predecessor_matrix_id=first.matrix_id,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="authority scope or integration mechanism changed",
    ):
        validate_capability_matrix_successor(first, second)


def test_successor_rejects_profile_version_and_observation_regression():
    versioned_profile = replace(profile(), profile_version=2)
    versioned_first = matrix(p=versioned_profile)
    version_regressed = replace(versioned_profile, profile_version=1)
    version_regressed_matrix = matrix(
        p=version_regressed,
        version=2,
        predecessor=versioned_first.matrix_id,
        as_of=T4,
    )
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="profile version regressed",
    ):
        validate_capability_matrix_successor(
            versioned_first,
            version_regressed_matrix,
        )

    later_profile = replace(profile(), profile_version=2, observed_at=T1)
    later_first = matrix(p=later_profile)
    observation_regressed = replace(later_profile, observed_at=T0)
    observation_regressed_matrix = matrix(
        p=observation_regressed,
        version=2,
        predecessor=later_first.matrix_id,
        as_of=T4,
    )
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="profile observation moved backwards",
    ):
        validate_capability_matrix_successor(
            later_first,
            observation_regressed_matrix,
        )


def test_successor_rejects_integration_observation_regression():
    p = profile()
    later_integration = bind_bookmaker_integration(
        p,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=T2,
        source_ref="integration-later",
        source_payload_sha256="d" * 64,
    )
    first = build_provider_capability_evidence_matrix(
        p,
        later_integration,
        environment="production",
        application_mode="live-key-readonly",
        matrix_version=1,
        as_of=T3,
        matrix_ref="later-integration-first",
    )
    earlier_integration = integration(p)
    second = build_provider_capability_evidence_matrix(
        p,
        earlier_integration,
        environment="production",
        application_mode="live-key-readonly",
        matrix_version=2,
        as_of=T4,
        matrix_ref="earlier-integration-second",
        predecessor_matrix_id=first.matrix_id,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="integration observation moved backwards",
    ):
        validate_capability_matrix_successor(first, second)


def test_successor_time_must_advance_strictly():
    first = matrix(as_of=T3)
    same_instant = matrix(
        version=2,
        predecessor=first.matrix_id,
        as_of=T3,
    )
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="successor time must advance strictly",
    ):
        validate_capability_matrix_successor(first, same_instant)


def test_matrix_journal_linearizes_successors_and_rejects_sibling_fork():
    journal = ProviderCapabilityEvidenceMatrixJournal()
    first = matrix(as_of=T3)
    second = matrix(
        version=2,
        predecessor=first.matrix_id,
        as_of=T4,
    )
    assert journal.publish(first) == first.matrix_id
    assert journal.publish(first) == first.matrix_id
    assert journal.publish(second) == second.matrix_id
    assert journal.latest_for(first) is second
    assert journal.latest_for(first, as_of=T2) is None
    assert journal.latest_for(first, as_of=T3) is first
    assert journal.latest_for(first, as_of=T4) is second

    sibling = build_provider_capability_evidence_matrix(
        first.profile,
        first.integration,
        environment=first.environment,
        application_mode=first.application_mode,
        matrix_version=2,
        as_of=T4,
        matrix_ref="matrix-2-sibling",
        predecessor_matrix_id=first.matrix_id,
    )
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="extend exact journal latest",
    ):
        journal.publish(sibling)


def test_matrix_journal_historical_lookup_walks_multiple_successors():
    journal = ProviderCapabilityEvidenceMatrixJournal()
    first = matrix(as_of=T2)
    second = matrix(
        version=2,
        predecessor=first.matrix_id,
        as_of=T3,
    )
    third = matrix(
        version=3,
        predecessor=second.matrix_id,
        as_of=T4,
    )
    journal.publish(first)
    journal.publish(second)
    journal.publish(third)

    assert journal.latest_for(first, as_of=T1) is None
    assert journal.latest_for(first, as_of=T2) is first
    assert journal.latest_for(first, as_of=T3) is second
    assert journal.latest_for(first, as_of=T4) is third


def test_matrix_journal_historical_lookup_rejects_mutated_intermediate():
    journal = ProviderCapabilityEvidenceMatrixJournal()
    first = matrix(as_of=T2)
    second = matrix(
        version=2,
        predecessor=first.matrix_id,
        as_of=T3,
    )
    third = matrix(
        version=3,
        predecessor=second.matrix_id,
        as_of=T4,
    )
    journal.publish(first)
    journal.publish(second)
    journal.publish(third)
    object.__setattr__(second, "matrix_ref", "mutated-intermediate")

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="mutated after publication",
    ):
        journal.latest_for(first, as_of=T3)


def test_matrix_journal_rejects_second_root_for_same_authority_scope():
    journal = ProviderCapabilityEvidenceMatrixJournal()
    first = matrix(as_of=T3)
    alternate_root = build_provider_capability_evidence_matrix(
        first.profile,
        first.integration,
        environment=first.environment,
        application_mode=first.application_mode,
        matrix_version=1,
        as_of=T3,
        matrix_ref="alternate-root",
    )
    journal.publish(first)

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="already has a root",
    ):
        journal.publish(alternate_root)


def test_matrix_journal_rejects_successor_without_root_and_copied_matrix():
    first = matrix(as_of=T3)
    second = matrix(
        version=2,
        predecessor=first.matrix_id,
        as_of=T4,
    )
    empty = ProviderCapabilityEvidenceMatrixJournal()
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="requires an existing journal root",
    ):
        empty.publish(second)

    copied = replace(first)
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="requires product-issued exact matrix",
    ):
        empty.publish(copied)


def test_matrix_journal_latest_requires_exact_published_anchor():
    journal = ProviderCapabilityEvidenceMatrixJournal()
    first = matrix(as_of=T3)
    journal.publish(first)

    copied = replace(first)
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="anchor is not an exact published matrix",
    ):
        journal.latest_for(copied)

    alternate_root = build_provider_capability_evidence_matrix(
        first.profile,
        first.integration,
        environment=first.environment,
        application_mode=first.application_mode,
        matrix_version=1,
        as_of=T3,
        matrix_ref="unpublished-alternate-root",
    )
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="anchor is not an exact published matrix",
    ):
        journal.latest_for(alternate_root)


def test_matrix_journal_latest_rejects_unpublished_anchor_on_empty_journal():
    first = matrix(as_of=T3)
    journal = ProviderCapabilityEvidenceMatrixJournal()

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="anchor is not an exact published matrix",
    ):
        journal.latest_for(first, as_of=T3)


def test_matrix_journal_detects_post_publication_mutation():
    journal = ProviderCapabilityEvidenceMatrixJournal()
    first = matrix(as_of=T3)
    journal.publish(first)
    object.__setattr__(first, "matrix_ref", "mutated-after-publication")

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="mutated after publication",
    ):
        journal.latest_for(first)


def test_matrix_journal_historical_lookup_rejects_mutated_future_successor():
    journal = ProviderCapabilityEvidenceMatrixJournal()
    first = matrix(as_of=T3)
    second = matrix(
        version=2,
        predecessor=first.matrix_id,
        as_of=T4,
    )
    journal.publish(first)
    journal.publish(second)
    object.__setattr__(second, "matrix_ref", "mutated-future-successor")

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="mutated after publication",
    ):
        journal.latest_for(first, as_of=T3)


def test_successor_validation_rejects_post_build_matrix_mutation():
    first = matrix()
    second = matrix(version=2, predecessor=first.matrix_id, as_of=T4)
    object.__setattr__(second, "matrix_ref", "tampered-matrix-ref")

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="product-issued exact objects with unchanged payload",
    ):
        validate_capability_matrix_successor(first, second)


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



def test_forged_checker_globals_cannot_mint_copied_positive_fact(monkeypatch):
    p = profile()
    i = integration(p)
    original_fact = evidence(
        p,
        BookmakerCapability.BALANCE_READ,
        ProviderCapabilityTruthGrade.DECLARED_DOCUMENTED,
        i=i,
    )
    original_matrix = matrix(p=p, facts=(original_fact,))
    copied_fact = replace(original_fact)
    forged_facts = tuple(
        copied_fact
        if fact.capability is BookmakerCapability.BALANCE_READ
        else fact
        for fact in original_matrix.facts
    )

    monkeypatch.setattr(
        capability_module,
        "_is_product_issued",
        lambda _fact: True,
        raising=False,
    )
    monkeypatch.setattr(
        capability_module,
        "_is_product_issued_matrix",
        lambda _matrix: True,
        raising=False,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="positive capability evidence must be product-issued exact object",
    ):
        replace(original_matrix, facts=forged_facts)


def test_forged_matrix_checker_global_cannot_authorize_copied_matrix(monkeypatch):
    original = matrix()
    copied = replace(original)

    monkeypatch.setattr(
        capability_module,
        "_is_product_issued_matrix",
        lambda _matrix: True,
        raising=False,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="capability matrix must be product-issued exact object",
    ):
        copied.fact_for(BookmakerCapability.BALANCE_READ)

    journal = ProviderCapabilityEvidenceMatrixJournal()
    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="matrix journal requires product-issued exact matrix",
    ):
        journal.publish(copied)


def test_forged_matrix_checker_global_cannot_validate_copied_successor(monkeypatch):
    first = matrix(as_of=T3)
    second = matrix(
        version=2,
        predecessor=first.matrix_id,
        as_of=T4,
    )
    copied_second = replace(second)

    monkeypatch.setattr(
        capability_module,
        "_is_product_issued_matrix",
        lambda _matrix: True,
        raising=False,
    )

    with pytest.raises(
        ProviderCapabilityEvidenceMatrixError,
        match="successor matrices must be product-issued exact objects",
    ):
        validate_capability_matrix_successor(first, copied_second)
