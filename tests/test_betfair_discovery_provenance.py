from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from autosport.betfair_multisport_catalog import (
    LIST_EVENTS,
    BetfairCatalogRequest,
    BetfairEventType,
    BetfairMarketType,
    build_list_event_types_request,
    build_list_market_types_request,
)
from autosport.betfair_discovery_provenance import (
    BetfairDiscoveryExchange,
    BetfairDiscoveryProvenanceError,
    BetfairDiscoveryVisibilityScope,
    build_betfair_discovery_acquisition_evidence,
)


T0 = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 22, 2, 0, 1, tzinfo=timezone.utc)


def _scope(
    *,
    account="account-A",
    application="application-A",
    key_class="LIVE",
    jurisdiction="UK",
):
    return BetfairDiscoveryVisibilityScope(
        account_scope_ref=account,
        application_scope_ref=application,
        key_class=key_class,
        jurisdiction=jurisdiction,
    )


def _evidence(
    *,
    event_response=b'[{"eventType":{"id":"1"},"marketCount":20}]',
    market_response=b'[{"marketType":"MATCH_ODDS","marketCount":10}]',
    market_count=10,
    display_name="Soccer",
    max_age_seconds=60,
    visibility_scope=None,
    t0=T0,
    t1=T1,
):
    return build_betfair_discovery_acquisition_evidence(
        discovery_run_id="run-001",
        visibility_scope=visibility_scope or _scope(),
        event_type_request=build_list_event_types_request(),
        event_type_raw_response=event_response,
        event_type_observed_at=t0,
        event_types=(BetfairEventType("1", display_name, 20),),
        selected_event_type_id="1",
        market_type_request=build_list_market_types_request(event_type_ids=("1",)),
        market_type_raw_response=market_response,
        market_type_observed_at=t1,
        market_types=(BetfairMarketType("MATCH_ODDS", market_count),),
        max_age_seconds=max_age_seconds,
    )


def test_capture_binds_canonical_request_filter_response_and_time():
    e = _evidence()
    x = e.market_type_exchange
    assert x.request_sha256 == hashlib.sha256(x.canonical_request_json.encode()).hexdigest()
    assert x.filter_sha256 == hashlib.sha256(x.canonical_filter_json.encode()).hexdigest()
    assert x.raw_response_sha256 == hashlib.sha256(x.raw_response).hexdigest()
    assert x.raw_response_size_bytes == len(x.raw_response)
    assert x.observed_at_utc == "2026-09-22T02:00:01.000000Z"
    projection = x.evidence_projection()
    assert projection["canonical_request_json"] == x.canonical_request_json
    assert projection["canonical_filter_json"] == x.canonical_filter_json


def test_canonical_request_digest_is_mapping_order_independent():
    a = BetfairCatalogRequest(
        "SportsAPING/v1.0/listMarketTypes",
        {"filter": {"eventTypeIds": ["1"], "textQuery": "x"}},
    )
    b = BetfairCatalogRequest(
        "SportsAPING/v1.0/listMarketTypes",
        {"filter": {"textQuery": "x", "eventTypeIds": ["1"]}},
    )
    xa = BetfairDiscoveryExchange(a, b"[]", T0)
    xb = BetfairDiscoveryExchange(b, b"[]", T0)
    assert xa.request_sha256 == xb.request_sha256
    assert xa.filter_sha256 == xb.filter_sha256


def test_changed_request_filter_changes_request_and_filter_digest():
    a = BetfairDiscoveryExchange(
        BetfairCatalogRequest(
            "SportsAPING/v1.0/listMarketTypes",
            {"filter": {"eventTypeIds": ["1"]}},
        ),
        b"[]",
        T0,
    )
    b = BetfairDiscoveryExchange(
        BetfairCatalogRequest(
            "SportsAPING/v1.0/listMarketTypes",
            {"filter": {"eventTypeIds": ["7"]}},
        ),
        b"[]",
        T0,
    )
    assert a.request_sha256 != b.request_sha256
    assert a.filter_sha256 != b.filter_sha256


def test_raw_response_bytes_are_exact_not_semantically_normalized():
    a = _evidence(market_response=b'{"x":1}')
    b = _evidence(market_response=b'{ "x": 1 }')
    assert a.semantic_identity_sha256 == b.semantic_identity_sha256
    assert a.acquisition_evidence_sha256 != b.acquisition_evidence_sha256


def test_provider_display_name_does_not_replace_native_semantic_identity():
    a = _evidence(display_name="Soccer")
    b = _evidence(display_name="Fußball")
    assert a.semantic_identity_sha256 == b.semantic_identity_sha256
    assert a.acquisition_evidence_sha256 == b.acquisition_evidence_sha256


def test_count_change_preserves_semantic_identity_but_changes_acquisition_evidence():
    a = _evidence(market_count=10)
    b = _evidence(market_count=11)
    assert a.semantic_identity_sha256 == b.semantic_identity_sha256
    assert a.acquisition_evidence_sha256 != b.acquisition_evidence_sha256


def test_policy_change_cannot_hide_under_same_acquisition_identity():
    a = _evidence(max_age_seconds=60)
    b = _evidence(max_age_seconds=120)
    assert a.semantic_identity_sha256 == b.semantic_identity_sha256
    assert a.acquisition_evidence_sha256 != b.acquisition_evidence_sha256


def test_observation_time_change_changes_acquisition_identity():
    a = _evidence()
    b = _evidence(t1=T1 + timedelta(microseconds=1))
    assert a.acquisition_evidence_sha256 != b.acquisition_evidence_sha256


@pytest.mark.parametrize(
    "bad_time",
    [
        datetime(2026, 9, 22, 2, 0),
        datetime(2026, 9, 22, 3, 0, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_capture_requires_strict_utc_time(bad_time):
    with pytest.raises(BetfairDiscoveryProvenanceError, match="UTC"):
        BetfairDiscoveryExchange(build_list_event_types_request(), b"[]", bad_time)


def test_market_type_observation_cannot_precede_event_type_observation():
    with pytest.raises(BetfairDiscoveryProvenanceError, match="precede"):
        _evidence(t0=T1, t1=T0)


def test_market_type_request_must_scope_exact_selected_event_type():
    with pytest.raises(BetfairDiscoveryProvenanceError, match="scoped"):
        build_betfair_discovery_acquisition_evidence(
            discovery_run_id="run",
            visibility_scope=_scope(),
            event_type_request=build_list_event_types_request(),
            event_type_raw_response=b"[]",
            event_type_observed_at=T0,
            event_types=(BetfairEventType("1", "Soccer", 1),),
            selected_event_type_id="1",
            market_type_request=build_list_market_types_request(event_type_ids=("1", "7")),
            market_type_raw_response=b"[]",
            market_type_observed_at=T1,
            market_types=(BetfairMarketType("MATCH_ODDS", 1),),
            max_age_seconds=60,
        )


def test_selected_event_type_must_exist_in_observed_inventory():
    with pytest.raises(BetfairDiscoveryProvenanceError, match="absent"):
        build_betfair_discovery_acquisition_evidence(
            discovery_run_id="run",
            visibility_scope=_scope(),
            event_type_request=build_list_event_types_request(),
            event_type_raw_response=b"[]",
            event_type_observed_at=T0,
            event_types=(BetfairEventType("7", "Horse Racing", 1),),
            selected_event_type_id="1",
            market_type_request=build_list_market_types_request(event_type_ids=("1",)),
            market_type_raw_response=b"[]",
            market_type_observed_at=T1,
            market_types=(),
            max_age_seconds=60,
        )


def test_wrong_exchange_method_fails_closed():
    wrong = BetfairCatalogRequest(LIST_EVENTS, {"filter": {"eventTypeIds": ["1"]}})
    with pytest.raises(BetfairDiscoveryProvenanceError, match="listEventTypes"):
        build_betfair_discovery_acquisition_evidence(
            discovery_run_id="run",
            visibility_scope=_scope(),
            event_type_request=wrong,
            event_type_raw_response=b"[]",
            event_type_observed_at=T0,
            event_types=(BetfairEventType("1", "Soccer", 1),),
            selected_event_type_id="1",
            market_type_request=build_list_market_types_request(event_type_ids=("1",)),
            market_type_raw_response=b"[]",
            market_type_observed_at=T1,
            market_types=(),
            max_age_seconds=60,
        )


def test_duplicate_market_type_codes_fail_closed():
    with pytest.raises(BetfairDiscoveryProvenanceError, match="duplicate market_type_code"):
        build_betfair_discovery_acquisition_evidence(
            discovery_run_id="run",
            visibility_scope=_scope(),
            event_type_request=build_list_event_types_request(),
            event_type_raw_response=b"[]",
            event_type_observed_at=T0,
            event_types=(BetfairEventType("1", "Soccer", 1),),
            selected_event_type_id="1",
            market_type_request=build_list_market_types_request(event_type_ids=("1",)),
            market_type_raw_response=b"[]",
            market_type_observed_at=T1,
            market_types=(
                BetfairMarketType("MATCH_ODDS", 1),
                BetfairMarketType("MATCH_ODDS", 2),
            ),
            max_age_seconds=60,
        )


@pytest.mark.parametrize("bad_age", [0, -1, True, 1.5, "60"])
def test_max_age_is_policy_metadata_with_strict_positive_integer_shape(bad_age):
    with pytest.raises(BetfairDiscoveryProvenanceError, match="positive integer"):
        _evidence(max_age_seconds=bad_age)


@pytest.mark.parametrize("bad_run", ["", " x", "x ", "a\nb"])
def test_discovery_run_id_is_stable_ascii_token(bad_run):
    with pytest.raises(BetfairDiscoveryProvenanceError, match="discovery_run_id"):
        build_betfair_discovery_acquisition_evidence(
            discovery_run_id=bad_run,
            visibility_scope=_scope(),
            event_type_request=build_list_event_types_request(),
            event_type_raw_response=b"[]",
            event_type_observed_at=T0,
            event_types=(BetfairEventType("1", "Soccer", 1),),
            selected_event_type_id="1",
            market_type_request=build_list_market_types_request(event_type_ids=("1",)),
            market_type_raw_response=b"[]",
            market_type_observed_at=T1,
            market_types=(),
            max_age_seconds=60,
        )



def test_visibility_scope_drift_changes_acquisition_not_semantic_identity():
    base = _evidence()
    variants = (
        _scope(account="account-B"),
        _scope(application="application-B"),
        _scope(key_class="DELAYED"),
        _scope(jurisdiction="AU"),
    )
    for scope in variants:
        changed = _evidence(visibility_scope=scope)
        assert changed.semantic_identity_sha256 == base.semantic_identity_sha256
        assert changed.acquisition_evidence_sha256 != base.acquisition_evidence_sha256


def test_visibility_scope_is_durable_nonsecret_reference_metadata():
    evidence = _evidence()
    projection = evidence.projection()
    assert projection["visibility_scope"] == {
        "account_scope_ref": "account-A",
        "application_scope_ref": "application-A",
        "key_class": "LIVE",
        "jurisdiction": "UK",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_scope_ref", ""),
        ("application_scope_ref", " app"),
        ("key_class", "LIVE\n"),
        ("jurisdiction", "E U"),
    ],
)
def test_visibility_scope_requires_stable_ascii_tokens(field, value):
    kwargs = {
        "account_scope_ref": "account-A",
        "application_scope_ref": "application-A",
        "key_class": "LIVE",
        "jurisdiction": "UK",
    }
    kwargs[field] = value
    with pytest.raises(BetfairDiscoveryProvenanceError):
        BetfairDiscoveryVisibilityScope(**kwargs)



def test_projection_preserves_sorted_provider_native_inventory_and_counts():
    evidence = build_betfair_discovery_acquisition_evidence(
        discovery_run_id="run-inventory",
        visibility_scope=_scope(),
        event_type_request=build_list_event_types_request(),
        event_type_raw_response=b"event-types",
        event_type_observed_at=T0,
        event_types=(
            BetfairEventType("7", "Horse Racing", 5),
            BetfairEventType("1", "Soccer", 20),
        ),
        selected_event_type_id="1",
        market_type_request=build_list_market_types_request(event_type_ids=("1",)),
        market_type_raw_response=b"market-types",
        market_type_observed_at=T1,
        market_types=(
            BetfairMarketType("WIN", 2),
            BetfairMarketType("MATCH_ODDS", 10),
        ),
        max_age_seconds=60,
    )
    projection = evidence.projection()
    assert projection["event_type_inventory"] == [
        {"event_type_id": "1", "market_count": 20},
        {"event_type_id": "7", "market_count": 5},
    ]
    assert projection["market_type_inventory"] == [
        {"market_type_code": "MATCH_ODDS", "market_count": 10},
        {"market_type_code": "WIN", "market_count": 2},
    ]
    assert all("display_name" not in row for row in projection["event_type_inventory"])


def test_inventory_count_change_is_visible_in_projection_and_acquisition_digest():
    a = _evidence(market_count=10)
    b = _evidence(market_count=11)
    assert a.projection()["market_type_inventory"] != b.projection()["market_type_inventory"]
    assert a.acquisition_evidence_sha256 != b.acquisition_evidence_sha256
    assert a.semantic_identity_sha256 == b.semantic_identity_sha256


def test_projection_never_mints_freshness_or_execution_authority():
    projection = _evidence().projection()
    assert projection["grants_freshness_authority"] is False
    assert projection["grants_execution_authority"] is False
    assert "is_fresh" not in projection
    assert "provider_ttl" not in projection


def test_market_type_input_order_does_not_change_identity():
    def make(items):
        return build_betfair_discovery_acquisition_evidence(
            discovery_run_id="run",
            visibility_scope=_scope(),
            event_type_request=build_list_event_types_request(),
            event_type_raw_response=b"[]",
            event_type_observed_at=T0,
            event_types=(BetfairEventType("1", "Soccer", 3),),
            selected_event_type_id="1",
            market_type_request=build_list_market_types_request(event_type_ids=("1",)),
            market_type_raw_response=b"[]",
            market_type_observed_at=T1,
            market_types=items,
            max_age_seconds=60,
        )
    x = BetfairMarketType("MATCH_ODDS", 2)
    y = BetfairMarketType("WIN", 1)
    a = make((x, y))
    b = make((y, x))
    assert a.semantic_identity_sha256 == b.semantic_identity_sha256
    assert a.acquisition_evidence_sha256 == b.acquisition_evidence_sha256


def test_raw_response_must_be_nonempty_immutable_bytes():
    with pytest.raises(BetfairDiscoveryProvenanceError, match="must not be empty"):
        BetfairDiscoveryExchange(build_list_event_types_request(), b"", T0)
    with pytest.raises(BetfairDiscoveryProvenanceError, match="immutable bytes"):
        BetfairDiscoveryExchange(build_list_event_types_request(), bytearray(b"[]"), T0)
