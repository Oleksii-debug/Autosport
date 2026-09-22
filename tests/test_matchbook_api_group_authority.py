from __future__ import annotations

import random

import pytest

from autosport.matchbook_api_group_authority import (
    ApiGroup,
    AmbiguousEndpointAuthority,
    DEFAULT_ENDPOINT_GROUP_REGISTRY,
    DEFAULT_ENDPOINT_SPECS,
    EndpointGroupRegistry,
    EndpointSpec,
    InvalidEndpointEvidence,
    UnknownEndpoint,
)


def classify(method: str, path: str):
    return DEFAULT_ENDPOINT_GROUP_REGISTRY.classify(method=method, path=path)


@pytest.mark.parametrize(
    ("method", "path", "group", "operation_id"),
    [
        ("POST", "/bpapi/rest/security/session", ApiGroup.SECURITY, "matchbook.security.login"),
        ("DELETE", "/bpapi/rest/security/session", ApiGroup.SECURITY, "matchbook.security.logout"),
        ("GET", "/edge/rest/account", ApiGroup.ACCOUNT, "matchbook.account.get"),
        ("GET", "/edge/rest/account/balance", ApiGroup.ACCOUNT, "matchbook.account.balance"),
        ("GET", "/edge/rest/account/sports", ApiGroup.ACCOUNT, "matchbook.account.sports"),
        ("GET", "/edge/rest/navigation", ApiGroup.NAVIGATION, "matchbook.navigation.get"),
        ("GET", "/edge/rest/events", ApiGroup.EVENTS, "matchbook.events.list"),
        ("GET", "/edge/rest/events/123", ApiGroup.EVENTS, "matchbook.events.get"),
        ("GET", "/edge/rest/events/123/markets", ApiGroup.EVENTS, "matchbook.markets.list"),
        ("GET", "/edge/rest/events/123/markets/456/runners", ApiGroup.EVENTS, "matchbook.runners.list"),
        ("GET", "/edge/rest/events/123/markets/456/runners/789/prices", ApiGroup.EVENTS, "matchbook.prices.get"),
        ("GET", "/edge/rest/v2/offers", ApiGroup.BETTING_READ, "matchbook.offers.list_unsettled"),
        ("GET", "/edge/rest/v2/offers/987", ApiGroup.BETTING_READ, "matchbook.offers.get_unsettled"),
        ("POST", "/edge/rest/v2/offers", ApiGroup.BETTING_WRITE, "matchbook.offers.submit"),
        ("GET", "/edge/rest/reports/v2/bets/current", ApiGroup.REPORTS, "matchbook.reports.current_bets"),
        ("GET", "/edge/rest/reports/v2/bets/settled", ApiGroup.REPORTS, "matchbook.reports.settled_bets"),
        ("GET", "/edge/rest/reports/v2/offers/current", ApiGroup.REPORTS, "matchbook.reports.current_offers"),
        ("GET", "/edge/rest/reports/v1/transactions", ApiGroup.REPORTS, "matchbook.reports.wallet_transactions"),
    ],
)
def test_documented_endpoint_classification(method, path, group, operation_id):
    receipt = classify(method, path)
    assert receipt.group is group
    assert receipt.operation_id == operation_id
    assert receipt.registry_sha256 == DEFAULT_ENDPOINT_GROUP_REGISTRY.registry_sha256


def test_same_concrete_path_can_have_different_documented_group_by_method():
    read = classify("GET", "/edge/rest/v2/offers")
    write = classify("POST", "/edge/rest/v2/offers")
    assert read.group is ApiGroup.BETTING_READ
    assert write.group is ApiGroup.BETTING_WRITE


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/edge/rest/v2/offers"),
        ("GET", "/edge/rest/v3/offers"),
        ("GET", "/edge/rest/unknown"),
        ("get", "/edge/rest/events"),
        ("GET ", "/edge/rest/events"),
        ("GET", "/edge/rest/events/"),
        ("GET", "/edge/rest/events?offset=0"),
        ("GET", "/edge/rest/events#fragment"),
        ("GET", "/edge//rest/events"),
        ("GET", "/edge/rest/../events"),
        ("GET", "/edge/rest/events/%31"),
        ("GET", "\\edge\\rest\\events"),
        ("GET", "https://api.matchbook.com/edge/rest/events"),
    ],
)
def test_unknown_or_noncanonical_requests_fail_closed(method, path):
    with pytest.raises(UnknownEndpoint):
        classify(method, path)


def test_path_parameter_is_opaque_but_one_segment_only():
    assert classify("GET", "/edge/rest/events/event-A").operation_id == "matchbook.events.get"
    with pytest.raises(UnknownEndpoint):
        classify("GET", "/edge/rest/events/event-A/extra")


def test_query_must_be_removed_before_authority_classification():
    with pytest.raises(UnknownEndpoint):
        classify("GET", "/edge/rest/reports/v2/bets/current?offset=0")


def test_registry_fingerprint_is_order_independent():
    forward = EndpointGroupRegistry(DEFAULT_ENDPOINT_SPECS)
    reverse = EndpointGroupRegistry(reversed(DEFAULT_ENDPOINT_SPECS))
    assert forward.registry_sha256 == reverse.registry_sha256


def test_registry_fingerprint_changes_if_documented_group_changes():
    original = DEFAULT_ENDPOINT_SPECS[0]
    changed = EndpointSpec(
        original.operation_id,
        original.method,
        original.path_template,
        ApiGroup.ACCOUNT,
        original.source_url,
        original.evidence_as_of,
    )
    registry = EndpointGroupRegistry((changed,) + DEFAULT_ENDPOINT_SPECS[1:])
    assert registry.registry_sha256 != DEFAULT_ENDPOINT_GROUP_REGISTRY.registry_sha256


def test_registry_rejects_duplicate_operation_id():
    a = EndpointSpec("same", "GET", "/a", ApiGroup.EVENTS, "https://developers.matchbook.com/reference/a")
    b = EndpointSpec("same", "POST", "/b", ApiGroup.EVENTS, "https://developers.matchbook.com/reference/b")
    with pytest.raises(AmbiguousEndpointAuthority, match="operation_id"):
        EndpointGroupRegistry((a, b))


def test_registry_rejects_exact_duplicate_route():
    a = EndpointSpec("a", "GET", "/x/{id}", ApiGroup.EVENTS, "https://developers.matchbook.com/reference/a")
    b = EndpointSpec("b", "GET", "/x/{other}", ApiGroup.REPORTS, "https://developers.matchbook.com/reference/b")
    with pytest.raises(AmbiguousEndpointAuthority, match="overlapping"):
        EndpointGroupRegistry((a, b))


def test_registry_rejects_literal_parameter_overlap():
    a = EndpointSpec("a", "GET", "/x/current", ApiGroup.EVENTS, "https://developers.matchbook.com/reference/a")
    b = EndpointSpec("b", "GET", "/x/{id}", ApiGroup.REPORTS, "https://developers.matchbook.com/reference/b")
    with pytest.raises(AmbiguousEndpointAuthority, match="overlapping"):
        EndpointGroupRegistry((a, b))


def test_registry_allows_same_shape_for_different_methods():
    a = EndpointSpec("a", "GET", "/x/{id}", ApiGroup.BETTING_READ, "https://developers.matchbook.com/reference/a")
    b = EndpointSpec("b", "DELETE", "/x/{id}", ApiGroup.BETTING_WRITE, "https://developers.matchbook.com/reference/b")
    registry = EndpointGroupRegistry((a, b))
    assert registry.classify(method="GET", path="/x/1").group is ApiGroup.BETTING_READ
    assert registry.classify(method="DELETE", path="/x/1").group is ApiGroup.BETTING_WRITE


@pytest.mark.parametrize(
    "template",
    ["x/y", "/", "/x/", "/x//y", "/x/{bad-name}", "/x/pre{id}", "/x/%7Bid%7D"],
)
def test_invalid_template_is_rejected(template):
    with pytest.raises(InvalidEndpointEvidence):
        EndpointSpec("x", "GET", template, ApiGroup.EVENTS, "https://developers.matchbook.com/reference/x")


def test_non_official_source_url_rejected():
    with pytest.raises(InvalidEndpointEvidence, match="official"):
        EndpointSpec("x", "GET", "/x", ApiGroup.EVENTS, "https://example.com/x")


def test_evidence_date_is_exact_iso_date():
    with pytest.raises(InvalidEndpointEvidence, match="YYYY-MM-DD"):
        EndpointSpec("x", "GET", "/x", ApiGroup.EVENTS, "https://developers.matchbook.com/reference/x", "22-09-2026")


def test_receipt_binds_documented_source_and_template():
    receipt = classify("GET", "/edge/rest/events/123/markets/456/runners/789/prices")
    assert receipt.path_template == "/edge/rest/events/{event_id}/markets/{market_id}/runners/{runner_id}/prices"
    assert receipt.source_url == "https://developers.matchbook.com/reference/get-prices"
    assert receipt.evidence_as_of == "2026-09-22"


def test_betting_read_classification_does_not_claim_numeric_limit():
    receipt = classify("GET", "/edge/rest/v2/offers")
    assert receipt.group is ApiGroup.BETTING_READ
    assert not hasattr(receipt, "requests_per_minute")


def test_50000_parameterized_known_routes_remain_deterministic():
    rng = random.Random(20260922)
    for _ in range(50_000):
        event = rng.randrange(1, 10**12)
        market = rng.randrange(1, 10**12)
        runner = rng.randrange(1, 10**12)
        receipt = classify(
            "GET",
            f"/edge/rest/events/{event}/markets/{market}/runners/{runner}/prices",
        )
        assert receipt.operation_id == "matchbook.prices.get"
        assert receipt.group is ApiGroup.EVENTS


def test_20000_malformed_route_mutations_fail_closed():
    rng = random.Random(20260923)
    canonical = "/edge/rest/events/123/markets/456/runners/789/prices"
    for i in range(20_000):
        mode = i % 5
        if mode == 0:
            candidate = canonical + f"/extra{rng.randrange(10**6)}"
        elif mode == 1:
            candidate = canonical.replace("/events/", "/Events/", 1)
        elif mode == 2:
            candidate = canonical + "?offset=0"
        elif mode == 3:
            candidate = canonical.replace("/123/", "/../", 1)
        else:
            candidate = canonical.replace("/456/", "/%34%35%36/", 1)
        with pytest.raises(UnknownEndpoint):
            classify("GET", candidate)
