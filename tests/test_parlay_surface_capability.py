from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from autosport.parlay_surface_capability import (
    DataUsability,
    ParlaySurfaceObservation,
    Surface,
    SurfaceCapabilityError,
    TechnicalSupport,
    evaluate_surface_capability,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
DIGEST = hashlib.sha256(b"provider-bytes").hexdigest()


def obs(**overrides):
    values = dict(
        sport_key="basketball_nba",
        surface=Surface.ODDS,
        observed_at=T0,
        available_at=T0 + timedelta(seconds=1),
        status_code=200,
        response_sha256=DIGEST,
        row_count=3,
        pagination_complete=True,
        requested_market="h2h",
        served_markets=("h2h",),
        oldest_row_age_seconds=5,
    )
    values.update(overrides)
    return ParlaySurfaceObservation(**values)


def decide(value, **kwargs):
    return evaluate_surface_capability(
        value,
        as_of=kwargs.pop("as_of", T0 + timedelta(seconds=2)),
        max_row_age_seconds=kwargs.pop("max_row_age_seconds", 30),
        **kwargs,
    )


def test_supported_fresh_exact_surface_is_usable():
    result = decide(obs())
    assert result.technical_support is TechnicalSupport.OBSERVED_SUPPORTED
    assert result.data_usability is DataUsability.USABLE
    assert result.provider_write_authorized is False
    assert result.execution_authorized is False
    assert result.real_money_execution is False


def test_market_routed_elsewhere_is_not_supported_here():
    result = decide(obs(
        requested_market="h2h_1st_half",
        served_markets=(),
        unservable_markets=("h2h_1st_half",),
        served_elsewhere=(("h2h_1st_half", "/v1/sports/{sport_key}/live/period_markets"),),
        row_count=0,
        oldest_row_age_seconds=None,
    ))
    assert result.technical_support is TechnicalSupport.ROUTE_ELSEWHERE
    assert result.route_elsewhere.endswith("/live/period_markets")
    assert result.data_usability is DataUsability.UNAVAILABLE


def test_generic_200_without_market_service_witness_is_unknown():
    result = decide(obs(served_markets=()))
    assert result.technical_support is TechnicalSupport.UNKNOWN
    assert result.reason == "MISSING_MARKET_SERVICE_WITNESS"


def test_empty_rows_do_not_destroy_technical_support():
    result = decide(obs(row_count=0, oldest_row_age_seconds=None))
    assert result.technical_support is TechnicalSupport.OBSERVED_SUPPORTED
    assert result.data_usability is DataUsability.EMPTY

def test_props_200_without_market_service_witness_is_unknown():
    result = decide(obs(
        surface=Surface.PROPS,
        requested_market="player_points",
        served_markets=(),
        row_count=8,
    ))
    assert result.technical_support is TechnicalSupport.UNKNOWN
    assert result.reason == "MISSING_MARKET_SERVICE_WITNESS"


def test_props_explicit_market_service_witness_can_be_usable():
    result = decide(obs(
        surface=Surface.PROPS,
        requested_market="player_points",
        served_markets=("player_points",),
        row_count=8,
    ))
    assert result.technical_support is TechnicalSupport.OBSERVED_SUPPORTED
    assert result.data_usability is DataUsability.USABLE




def test_incomplete_pagination_cannot_be_usable():
    result = decide(obs(surface=Surface.PROPS, requested_market=None, served_markets=(), pagination_complete=False))
    assert result.technical_support is TechnicalSupport.OBSERVED_SUPPORTED
    assert result.data_usability is DataUsability.INCOMPLETE


def test_missing_freshness_cannot_be_usable():
    result = decide(obs(oldest_row_age_seconds=None))
    assert result.data_usability is DataUsability.UNKNOWN


def test_oldest_row_controls_freshness_not_best_row():
    result = decide(obs(oldest_row_age_seconds=31))
    assert result.data_usability is DataUsability.STALE


def test_exact_freshness_boundary_is_allowed():
    result = decide(obs(oldest_row_age_seconds=30))
    assert result.data_usability is DataUsability.USABLE


def test_future_available_evidence_is_not_causal():
    result = decide(obs(available_at=T0 + timedelta(seconds=3)), as_of=T0 + timedelta(seconds=2))
    assert result.technical_support is TechnicalSupport.UNKNOWN
    assert result.reason == "EVIDENCE_NOT_CAUSALLY_AVAILABLE"


def test_observational_decision_never_resolves_source_authority():
    result = decide(obs())
    assert result.technical_support is TechnicalSupport.OBSERVED_SUPPORTED
    assert result.source_authority_resolved is False


def test_caller_cannot_mint_provider_origin_verification():
    with pytest.raises(TypeError):
        ParlaySurfaceObservation(
            sport_key="basketball_nba",
            surface=Surface.ODDS,
            observed_at=T0,
            available_at=T0,
            status_code=200,
            provider_origin_verified=True,
            response_sha256=DIGEST,
            row_count=1,
            pagination_complete=True,
        )


@pytest.mark.parametrize("status", [429, 500, 502, 599])
def test_transient_http_states_remain_unknown(status):
    result = decide(obs(
        status_code=status,
        row_count=0,
        error_code="RATE_LIMITED" if status == 429 else "SERVER_ERROR",
        oldest_row_age_seconds=None,
    ))
    assert result.technical_support is TechnicalSupport.TRANSIENT_UNKNOWN
    assert result.data_usability is DataUsability.UNAVAILABLE


@pytest.mark.parametrize("code", ["HISTORICAL_LIMIT", "TIER_GATED", "CREDIT_LIMIT", "OTHER"])
def test_403_error_code_cannot_mint_entitlement_authority(code):
    result = decide(obs(
        surface=Surface.HISTORICAL_ODDS,
        status_code=403,
        row_count=0,
        error_code=code,
        oldest_row_age_seconds=None,
    ))
    assert result.technical_support is TechnicalSupport.UNKNOWN
    assert result.data_usability is DataUsability.UNAVAILABLE
    assert result.reason == "FORBIDDEN_UNRESOLVED"
    assert result.source_authority_resolved is False


def test_market_cannot_be_both_served_and_unservable():
    with pytest.raises(SurfaceCapabilityError):
        obs(unservable_markets=("h2h",))


def test_route_requires_unservable_witness():
    with pytest.raises(SurfaceCapabilityError):
        obs(served_elsewhere=(("h2h", "/v1/sports/{sport_key}/props"),))


def test_duplicate_market_route_is_rejected():
    with pytest.raises(SurfaceCapabilityError):
        obs(
            requested_market="team_totals",
            served_markets=(),
            unservable_markets=("team_totals",),
            served_elsewhere=(
                ("team_totals", "/v1/sports/{sport_key}/props"),
                ("team_totals", "/v1/sports/{sport_key}/odds"),
            ),
        )


def test_noncanonical_sport_key_rejected():
    with pytest.raises(SurfaceCapabilityError):
        obs(sport_key="Basketball NBA")


def test_bool_cannot_pass_as_status_code():
    with pytest.raises(SurfaceCapabilityError):
        obs(status_code=True)


def test_non_200_cannot_claim_rows():
    with pytest.raises(SurfaceCapabilityError):
        obs(status_code=403, error_code="HISTORICAL_LIMIT")


def test_http_200_cannot_carry_error_code():
    with pytest.raises(SurfaceCapabilityError):
        obs(error_code="SOMETHING")


def test_evidence_identity_changes_on_market_service_truth():
    a = obs()
    b = obs(
        served_markets=(),
        unservable_markets=("h2h",),
        served_elsewhere=(("h2h", "/v1/sports/{sport_key}/props"),),
        row_count=0,
        oldest_row_age_seconds=None,
    )
    assert a.evidence_id != b.evidence_id


def test_equivalent_timezone_normalizes_to_one_identity():
    plus2 = timezone(timedelta(hours=2))
    a = obs()
    b = obs(
        observed_at=T0.astimezone(plus2),
        available_at=(T0 + timedelta(seconds=1)).astimezone(plus2),
    )
    assert a.evidence_id == b.evidence_id


def test_unordered_markets_are_rejected_not_silently_normalized():
    with pytest.raises(SurfaceCapabilityError):
        obs(requested_market="spreads", served_markets=("spreads", "h2h"))


def test_unknown_market_unservable_without_route_is_not_global_unsupported():
    result = decide(obs(
        requested_market="correct_score",
        served_markets=(),
        unservable_markets=("correct_score",),
        row_count=0,
        oldest_row_age_seconds=None,
    ))
    assert result.technical_support is TechnicalSupport.UNKNOWN
    assert result.reason == "MARKET_UNSERVABLE_HERE"


def test_auth_failure_is_not_capability_failure():
    result = decide(obs(status_code=401, row_count=0, error_code="INVALID_API_KEY", oldest_row_age_seconds=None))
    assert result.technical_support is TechnicalSupport.UNKNOWN
    assert result.reason == "AUTHENTICATION_UNRESOLVED"


def test_route_evidence_is_order_stable_only_when_canonical():
    value = obs(
        requested_market="correct_score",
        served_markets=(),
        unservable_markets=("btts", "correct_score"),
        served_elsewhere=(
            ("btts", "/v1/sports/{sport_key}/props"),
            ("correct_score", "/v1/sports/{sport_key}/props"),
        ),
        row_count=0,
        oldest_row_age_seconds=None,
    )
    assert decide(value).technical_support is TechnicalSupport.ROUTE_ELSEWHERE


def test_decision_rejects_subclassed_observation():
    class Forged(ParlaySurfaceObservation):
        pass

    forged = Forged(
        sport_key="basketball_nba",
        surface=Surface.ODDS,
        observed_at=T0,
        available_at=T0,
        status_code=200,
        response_sha256=DIGEST,
        row_count=1,
        pagination_complete=True,
        requested_market="h2h",
        served_markets=("h2h",),
        oldest_row_age_seconds=1,
    )
    with pytest.raises(SurfaceCapabilityError):
        decide(forged)
