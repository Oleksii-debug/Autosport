from __future__ import annotations

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
    ProviderCapabilityTruthGrade,
    build_provider_capability_evidence_matrix,
    issue_provider_capability_evidence,
)


T0 = "2026-09-22T00:00:00+00:00"
T1 = "2026-09-22T00:01:00+00:00"
T2 = "2026-09-22T00:02:00+00:00"
T3 = "2026-09-22T00:03:00+00:00"
H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-a",
        adapter_id="betfair-api",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.LIVE_QUOTES_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=T0,
        source_ref="profile",
        source_payload_sha256=H1,
    )


def _integration(profile: BookmakerCapabilityProfile):
    return bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at=T1,
        source_ref="integration",
        source_payload_sha256=H2,
    )


def _configured_fact(
    profile: BookmakerCapabilityProfile,
    integration,
    *,
    sport_scope: tuple[str, ...] = (),
    market_scope: tuple[str, ...] = (),
):
    return issue_provider_capability_evidence(
        capability=BookmakerCapability.LIVE_QUOTES_READ,
        profile_state=BookmakerCapabilityState.SUPPORTED,
        grade=ProviderCapabilityTruthGrade.CONFIGURED,
        profile_id=profile.profile_id,
        integration_evidence_id=integration.evidence_id,
        environment="production",
        application_mode="live-key-readonly",
        observed_at=T2,
        evidence_ref="evidence://configured/live-quotes",
        evidence_sha256=H3,
        endpoint_operation="listMarketBook",
        sport_scope=sport_scope,
        market_scope=market_scope,
    )


def _matrix(fact):
    profile = _profile()
    integration = _integration(profile)
    # Re-issue against the exact matrix profile/integration authority.
    fact = _configured_fact(
        profile,
        integration,
        sport_scope=fact[0],
        market_scope=fact[1],
    )
    return build_provider_capability_evidence_matrix(
        profile,
        integration,
        environment="production",
        application_mode="live-key-readonly",
        matrix_version=1,
        as_of=T3,
        matrix_ref="matrix-scope-consumption",
        evidence=(fact,),
    )


def _qualifies_unscoped(matrix) -> bool:
    return matrix.qualifies(
        BookmakerCapability.LIVE_QUOTES_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.CONFIGURED}),
        at_time=T3,
    )


def test_unscoped_query_accepts_only_unscoped_capability_fact() -> None:
    matrix = _matrix(((), ()))

    assert _qualifies_unscoped(matrix)


def test_sport_scoped_fact_cannot_qualify_as_provider_wide() -> None:
    matrix = _matrix((("tennis",), ()))

    assert not _qualifies_unscoped(matrix)


def test_market_scoped_fact_cannot_qualify_as_provider_wide() -> None:
    matrix = _matrix(((), ("match_odds",)))

    assert not _qualifies_unscoped(matrix)


def test_sport_and_market_scoped_fact_cannot_qualify_as_provider_wide() -> None:
    matrix = _matrix((("tennis",), ("match_odds",)))

    assert not _qualifies_unscoped(matrix)


def _qualifies_scoped(matrix, *, sport=None, market_family=None) -> bool:
    return matrix.qualifies_scoped(
        BookmakerCapability.LIVE_QUOTES_READ,
        accepted_grades=frozenset({ProviderCapabilityTruthGrade.CONFIGURED}),
        at_time=T3,
        sport=sport,
        market_family=market_family,
    )


def test_explicit_scoped_query_matches_all_restricted_axes() -> None:
    matrix = _matrix((("tennis",), ("match_odds",)))

    assert _qualifies_scoped(
        matrix,
        sport="tennis",
        market_family="match_odds",
    )
    assert not _qualifies_scoped(
        matrix,
        sport="football",
        market_family="match_odds",
    )
    assert not _qualifies_scoped(
        matrix,
        sport="tennis",
        market_family="winner",
    )


def test_explicit_scoped_query_cannot_drop_a_restricted_axis() -> None:
    matrix = _matrix((("tennis",), ("match_odds",)))

    assert not _qualifies_scoped(matrix, sport="tennis")
    assert not _qualifies_scoped(matrix, market_family="match_odds")


def test_single_axis_restrictions_are_consumable_without_overgeneralization() -> None:
    sport_matrix = _matrix((("tennis",), ()))
    market_matrix = _matrix(((), ("match_odds",)))

    assert _qualifies_scoped(sport_matrix, sport="tennis")
    assert not _qualifies_scoped(sport_matrix, sport="football")
    assert _qualifies_scoped(market_matrix, market_family="match_odds")
    assert not _qualifies_scoped(market_matrix, market_family="winner")


def test_provider_wide_fact_can_satisfy_an_explicit_narrower_scope() -> None:
    matrix = _matrix(((), ()))

    assert _qualifies_scoped(
        matrix,
        sport="tennis",
        market_family="match_odds",
    )
