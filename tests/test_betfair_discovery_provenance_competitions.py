from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

import pytest

from autosport.betfair_multisport_catalog import (
    LIST_EVENTS,
    BetfairCatalogRequest,
    BetfairCompetition,
    BetfairEventType,
    BetfairMarketType,
    build_list_competitions_request,
    build_list_event_types_request,
    build_list_market_types_request,
)
from autosport.betfair_discovery_provenance import (
    BetfairDiscoveryProvenanceError,
    BetfairDiscoveryVisibilityScope,
    build_betfair_discovery_acquisition_evidence,
)


T0 = datetime(2026, 9, 22, 2, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 22, 2, 0, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 22, 2, 0, 2, tzinfo=timezone.utc)


def _scope():
    return BetfairDiscoveryVisibilityScope(
        account_scope_ref="account-A",
        application_scope_ref="application-A",
        key_class="LIVE",
        jurisdiction="UK",
    )


def _evidence(
    *,
    competition_id="31",
    competition_name="Formula 1",
    competition_count=20,
    competition_event_type_id="8",
    market_competition_id=None,
    selected_competition_id=None,
    competition_time=T1,
):
    selected = competition_id if selected_competition_id is None else selected_competition_id
    market_scope = competition_id if market_competition_id is None else market_competition_id
    return build_betfair_discovery_acquisition_evidence(
        discovery_run_id="run-competition",
        visibility_scope=_scope(),
        event_type_request=build_list_event_types_request(),
        event_type_raw_response=b"event-types",
        event_type_observed_at=T0,
        event_types=(BetfairEventType("8", "Motor Sport", 100),),
        selected_event_type_id="8",
        market_type_request=build_list_market_types_request(
            event_type_ids=("8",),
            competition_ids=(market_scope,),
        ),
        market_type_raw_response=b"market-types",
        market_type_observed_at=T2,
        market_types=(BetfairMarketType("WIN", 20),),
        max_age_seconds=60,
        competition_request=build_list_competitions_request(
            event_type_ids=(competition_event_type_id,)
        ),
        competition_raw_response=b"competitions",
        competition_observed_at=competition_time,
        competitions=(
            BetfairCompetition(competition_id, competition_name, competition_count),
        ),
        selected_competition_id=selected,
    )


def test_competition_acquisition_projection_binds_exchange_inventory_and_selection():
    evidence = _evidence()
    projection = evidence.projection()
    assert projection["selected_competition_id"] == "31"
    assert projection["competition_inventory"] == [
        {"competition_id": "31", "market_count": 20}
    ]
    exchange = projection["competition_exchange"]
    assert exchange["method"] == "SportsAPING/v1.0/listCompetitions"
    assert exchange["observed_at_utc"] == "2026-09-22T02:00:01.000000Z"


def test_selected_competition_changes_semantic_identity_for_same_market_type_codes():
    a = _evidence(competition_id="31")
    b = _evidence(competition_id="32")
    assert a.semantic_identity_sha256 != b.semantic_identity_sha256
    assert a.acquisition_evidence_sha256 != b.acquisition_evidence_sha256


def test_localized_competition_name_does_not_replace_provider_identity():
    a = _evidence(competition_name="Formula 1")
    b = _evidence(competition_name="Формула 1")
    assert a.semantic_identity_sha256 == b.semantic_identity_sha256
    assert a.acquisition_evidence_sha256 == b.acquisition_evidence_sha256


def test_competition_count_changes_acquisition_not_semantic_identity():
    a = _evidence(competition_count=20)
    b = _evidence(competition_count=21)
    assert a.semantic_identity_sha256 == b.semantic_identity_sha256
    assert a.acquisition_evidence_sha256 != b.acquisition_evidence_sha256


def test_competition_request_must_match_selected_event_type():
    with pytest.raises(BetfairDiscoveryProvenanceError, match="listCompetitions request"):
        _evidence(competition_event_type_id="7")


def test_selected_competition_must_exist_in_observed_inventory():
    with pytest.raises(BetfairDiscoveryProvenanceError, match="absent from observed competitions"):
        _evidence(selected_competition_id="999")


def test_market_type_request_must_match_selected_competition():
    with pytest.raises(BetfairDiscoveryProvenanceError, match="selected competitionId"):
        _evidence(market_competition_id="999")


def test_competition_observation_must_be_between_event_and_market_observations():
    with pytest.raises(BetfairDiscoveryProvenanceError, match="between"):
        _evidence(competition_time=T0 - timedelta(microseconds=1))
    with pytest.raises(BetfairDiscoveryProvenanceError, match="between"):
        _evidence(competition_time=T2 + timedelta(microseconds=1))


def test_partial_competition_exchange_triplet_fails_closed():
    with pytest.raises(BetfairDiscoveryProvenanceError, match="must be supplied together"):
        build_betfair_discovery_acquisition_evidence(
            discovery_run_id="run-partial",
            visibility_scope=_scope(),
            event_type_request=build_list_event_types_request(),
            event_type_raw_response=b"event-types",
            event_type_observed_at=T0,
            event_types=(BetfairEventType("8", "Motor Sport", 100),),
            selected_event_type_id="8",
            market_type_request=build_list_market_types_request(event_type_ids=("8",)),
            market_type_raw_response=b"market-types",
            market_type_observed_at=T2,
            market_types=(BetfairMarketType("WIN", 20),),
            max_age_seconds=60,
            competition_request=build_list_competitions_request(event_type_ids=("8",)),
        )


def test_competition_scoped_market_types_require_competition_acquisition_evidence():
    with pytest.raises(BetfairDiscoveryProvenanceError, match="requires listCompetitions"):
        build_betfair_discovery_acquisition_evidence(
            discovery_run_id="run-missing-competition",
            visibility_scope=_scope(),
            event_type_request=build_list_event_types_request(),
            event_type_raw_response=b"event-types",
            event_type_observed_at=T0,
            event_types=(BetfairEventType("8", "Motor Sport", 100),),
            selected_event_type_id="8",
            market_type_request=build_list_market_types_request(
                event_type_ids=("8",), competition_ids=("31",)
            ),
            market_type_raw_response=b"market-types",
            market_type_observed_at=T2,
            market_types=(BetfairMarketType("WIN", 20),),
            max_age_seconds=60,
        )


def test_legacy_unscoped_acquisition_projection_remains_competition_free():
    evidence = build_betfair_discovery_acquisition_evidence(
        discovery_run_id="run-unscoped",
        visibility_scope=_scope(),
        event_type_request=build_list_event_types_request(),
        event_type_raw_response=b"event-types",
        event_type_observed_at=T0,
        event_types=(BetfairEventType("1", "Soccer", 100),),
        selected_event_type_id="1",
        market_type_request=build_list_market_types_request(event_type_ids=("1",)),
        market_type_raw_response=b"market-types",
        market_type_observed_at=T2,
        market_types=(BetfairMarketType("MATCH_ODDS", 100),),
        max_age_seconds=60,
    )
    projection = evidence.projection()
    assert "competition_exchange" not in projection
    assert "competition_inventory" not in projection
    assert "selected_competition_id" not in projection


def test_legacy_unscoped_hash_payload_is_byte_compatible_with_precompetition_contract():
    evidence = build_betfair_discovery_acquisition_evidence(
        discovery_run_id="run-unscoped-hash",
        visibility_scope=_scope(),
        event_type_request=build_list_event_types_request(),
        event_type_raw_response=b"event-types",
        event_type_observed_at=T0,
        event_types=(BetfairEventType("1", "Soccer", 100),),
        selected_event_type_id="1",
        market_type_request=build_list_market_types_request(event_type_ids=("1",)),
        market_type_raw_response=b"market-types",
        market_type_observed_at=T2,
        market_types=(BetfairMarketType("MATCH_ODDS", 100),),
        max_age_seconds=60,
    )
    old_semantic_payload = {
        "provider_id": evidence.provider_id,
        "selected_event_type_id": evidence.selected_event_type_id,
        "market_type_codes": ["MATCH_ODDS"],
    }
    old_acquisition_payload = {
        "schema_version": 1,
        "provider_id": evidence.provider_id,
        "discovery_run_id": evidence.discovery_run_id,
        "visibility_scope": evidence.visibility_scope.projection(),
        "event_type_exchange": evidence.event_type_exchange.evidence_projection(),
        "event_types": evidence.event_type_inventory_projection(),
        "selected_event_type_id": evidence.selected_event_type_id,
        "market_type_exchange": evidence.market_type_exchange.evidence_projection(),
        "market_types": evidence.market_type_inventory_projection(),
        "max_age_seconds": evidence.max_age_seconds,
    }

    def digest(payload):
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    assert evidence.semantic_identity_sha256 == digest(old_semantic_payload)
    assert evidence.acquisition_evidence_sha256 == digest(old_acquisition_payload)


def test_competition_exchange_wrong_method_fails_closed():
    wrong = BetfairCatalogRequest(
        LIST_EVENTS,
        {"filter": {"eventTypeIds": ["8"]}},
    )
    with pytest.raises(BetfairDiscoveryProvenanceError, match="listCompetitions"):
        build_betfair_discovery_acquisition_evidence(
            discovery_run_id="run-wrong-method",
            visibility_scope=_scope(),
            event_type_request=build_list_event_types_request(),
            event_type_raw_response=b"event-types",
            event_type_observed_at=T0,
            event_types=(BetfairEventType("8", "Motor Sport", 100),),
            selected_event_type_id="8",
            market_type_request=build_list_market_types_request(
                event_type_ids=("8",), competition_ids=("31",)
            ),
            market_type_raw_response=b"market-types",
            market_type_observed_at=T2,
            market_types=(BetfairMarketType("WIN", 20),),
            max_age_seconds=60,
            competition_request=wrong,
            competition_raw_response=b"competitions",
            competition_observed_at=T1,
            competitions=(BetfairCompetition("31", "Formula 1", 20),),
            selected_competition_id="31",
        )
