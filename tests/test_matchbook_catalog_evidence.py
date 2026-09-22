import json
import pytest
from autosport.matchbook_catalog_evidence import *


def raw(key, rows): return json.dumps({key: rows}, separators=(",", ":")).encode()
def req(offset=0, filters=(("sport-ids", "15,17"), ("states", "open,suspended"))):
    return MatchbookCatalogRequest(CatalogResource.EVENTS, offset=offset, per_page=2, filters=filters)


def test_request_hash_canonicalizes_filter_order_and_forces_no_prices():
    a = MatchbookCatalogRequest(CatalogResource.EVENTS, filters=(("states", "open"), ("sport-ids", "15")))
    b = MatchbookCatalogRequest(CatalogResource.EVENTS, filters=(("sport-ids", "15"), ("states", "open")))
    assert a.request_sha256 == b.request_sha256
    assert ("include-prices", "false") in a.query


def test_offset_changes_request_not_scope_but_page_size_changes_scope():
    a = MatchbookCatalogRequest(CatalogResource.SPORTS, offset=0, per_page=20)
    b = MatchbookCatalogRequest(CatalogResource.SPORTS, offset=20, per_page=20)
    c = MatchbookCatalogRequest(CatalogResource.SPORTS, offset=0, per_page=21)
    assert a.request_sha256 != b.request_sha256 and a.scope_sha256 == b.scope_sha256
    assert a.scope_sha256 != c.scope_sha256


def test_market_request_binds_event():
    r = MatchbookCatalogRequest(CatalogResource.MARKETS, event_id="123")
    assert r.path.endswith("/123/markets")
    with pytest.raises(MatchbookCatalogEvidenceError): MatchbookCatalogRequest(CatalogResource.MARKETS)
    with pytest.raises(MatchbookCatalogEvidenceError): MatchbookCatalogRequest(CatalogResource.SPORTS, event_id=1)


def test_filter_validation():
    with pytest.raises(MatchbookCatalogEvidenceError): MatchbookCatalogRequest(CatalogResource.EVENTS, filters=(("offset", "2"),))
    with pytest.raises(MatchbookCatalogEvidenceError): MatchbookCatalogRequest(CatalogResource.EVENTS, filters=(("states", "open"), ("states", "closed")))
    with pytest.raises(MatchbookCatalogEvidenceError): MatchbookCatalogRequest(CatalogResource.SPORTS, filters=(("sport-ids", "15"),))


def test_raw_bytes_are_hash_bound_even_when_json_semantics_match():
    r = MatchbookCatalogRequest(CatalogResource.SPORTS)
    a = parse_matchbook_catalog_page(r, b'[{"id":15,"name":"Soccer"}]', observed_at="2026-09-22T20:00:00Z")
    b = parse_matchbook_catalog_page(r, b'[ {"id":15,"name":"Soccer"} ]', observed_at="2026-09-22T20:00:00Z")
    assert a.entities == b.entities and a.raw_response_sha256 != b.raw_response_sha256 and a.page_sha256 != b.page_sha256


@pytest.mark.parametrize("value", [15, "15"])
def test_native_id_accepts_integer_or_canonical_decimal_string(value):
    p = parse_matchbook_catalog_page(MatchbookCatalogRequest(CatalogResource.SPORTS), json.dumps([{"id": value}]).encode(), observed_at="2026-09-22T20:00:00Z")
    assert p.entities[0].native_id == "15"


@pytest.mark.parametrize("value", [True, 0, -1, "015", "15.0", str(1 << 63)])
def test_bad_native_ids_fail_closed(value):
    with pytest.raises(MatchbookCatalogEvidenceError):
        parse_matchbook_catalog_page(MatchbookCatalogRequest(CatalogResource.SPORTS), json.dumps([{"id": value}]).encode(), observed_at="2026-09-22T20:00:00Z")


def test_duplicate_json_keys_and_nonfinite_fail_closed():
    r = MatchbookCatalogRequest(CatalogResource.EVENTS)
    with pytest.raises(MatchbookCatalogEvidenceError): parse_matchbook_catalog_page(r, b'{"events":[],"events":[]}', observed_at="2026-09-22T20:00:00Z")
    with pytest.raises(MatchbookCatalogEvidenceError): parse_matchbook_catalog_page(r, b'{"events":[{"id":1,"x":NaN}]}', observed_at="2026-09-22T20:00:00Z")


def test_event_preserves_provider_sport_parent():
    p = parse_matchbook_catalog_page(MatchbookCatalogRequest(CatalogResource.EVENTS, per_page=2), raw("events", [{"id": 100, "sport-id": "15", "name": "A"}]), observed_at="2026-09-22T20:00:00Z")
    assert p.entities[0].native_id == "100" and p.entities[0].parent_sport_id == "15"


def test_market_parent_is_endpoint_bound_and_conflict_rejected():
    r = MatchbookCatalogRequest(CatalogResource.MARKETS, event_id=100, per_page=2)
    p = parse_matchbook_catalog_page(r, raw("markets", [{"id": 200, "event-id": "100", "market-type": "winner"}]), observed_at="2026-09-22T20:00:00Z")
    assert p.entities[0].parent_event_id == "100"
    with pytest.raises(MatchbookCatalogEvidenceError): parse_matchbook_catalog_page(r, raw("markets", [{"id": 201, "event-id": 101}]), observed_at="2026-09-22T20:00:00Z")


def test_duplicate_and_oversized_page_rejected():
    r = MatchbookCatalogRequest(CatalogResource.EVENTS, per_page=2)
    with pytest.raises(MatchbookCatalogEvidenceError): parse_matchbook_catalog_page(r, raw("events", [{"id": 1}, {"id": "1"}]), observed_at="2026-09-22T20:00:00Z")
    with pytest.raises(MatchbookCatalogEvidenceError): parse_matchbook_catalog_page(r, raw("events", [{"id": 1}, {"id": 2}, {"id": 3}]), observed_at="2026-09-22T20:00:00Z")


def test_contiguous_pages_never_mint_atomic_complete_or_absence_truth():
    a = parse_matchbook_catalog_page(req(0), raw("events", [{"id": 1}, {"id": 2}]), observed_at="2026-09-22T20:00:00Z")
    b = parse_matchbook_catalog_page(req(2), raw("events", [{"id": 3}]), observed_at="2026-09-22T20:00:01Z")
    o = compose_matchbook_catalog_observation((a, b))
    assert o.entity_count == 3 and not o.snapshot_atomic and not o.catalog_complete and not o.authoritative_absence


def test_empty_tail_still_not_authoritative_absence():
    a = parse_matchbook_catalog_page(req(0), raw("events", [{"id": 1}, {"id": 2}]), observed_at="2026-09-22T20:00:00Z")
    b = parse_matchbook_catalog_page(req(2), raw("events", []), observed_at="2026-09-22T20:00:01Z")
    o = compose_matchbook_catalog_observation((a, b))
    assert not o.catalog_complete and not o.authoritative_absence


def test_gap_overlap_and_nonzero_start_rejected():
    p0 = parse_matchbook_catalog_page(req(0), raw("events", [{"id": 1}]), observed_at="2026-09-22T20:00:00Z")
    p1 = parse_matchbook_catalog_page(req(1), raw("events", [{"id": 2}]), observed_at="2026-09-22T20:00:01Z")
    p3 = parse_matchbook_catalog_page(req(3), raw("events", [{"id": 3}]), observed_at="2026-09-22T20:00:02Z")
    with pytest.raises(MatchbookCatalogEvidenceError): compose_matchbook_catalog_observation((p0, p1))
    with pytest.raises(MatchbookCatalogEvidenceError): compose_matchbook_catalog_observation((p0, p3))
    with pytest.raises(MatchbookCatalogEvidenceError): compose_matchbook_catalog_observation((p1,))


def test_scope_change_between_pages_rejected():
    a = parse_matchbook_catalog_page(req(0), raw("events", [{"id": 1}]), observed_at="2026-09-22T20:00:00Z")
    b = parse_matchbook_catalog_page(req(2, (("sport-ids", "15"), ("states", "open"))), raw("events", [{"id": 2}]), observed_at="2026-09-22T20:00:01Z")
    with pytest.raises(MatchbookCatalogEvidenceError): compose_matchbook_catalog_observation((a, b))


def test_repeated_entity_across_pages_and_clock_regression_rejected():
    a = parse_matchbook_catalog_page(req(0), raw("events", [{"id": 1}, {"id": 2}]), observed_at="2026-09-22T20:00:02Z")
    repeat = parse_matchbook_catalog_page(req(2), raw("events", [{"id": 2}]), observed_at="2026-09-22T20:00:03Z")
    earlier = parse_matchbook_catalog_page(req(2), raw("events", [{"id": 3}]), observed_at="2026-09-22T20:00:01Z")
    with pytest.raises(MatchbookCatalogEvidenceError): compose_matchbook_catalog_observation((a, repeat))
    with pytest.raises(MatchbookCatalogEvidenceError): compose_matchbook_catalog_observation((a, earlier))


def test_digest_deterministic_and_no_network_write_api():
    a = parse_matchbook_catalog_page(req(0), raw("events", [{"id": 1}, {"id": 2}]), observed_at="2026-09-22T20:00:00Z")
    b = parse_matchbook_catalog_page(req(2), raw("events", [{"id": 3}]), observed_at="2026-09-22T20:00:01Z")
    assert compose_matchbook_catalog_observation((a, b)).observation_sha256 == compose_matchbook_catalog_observation((a, b)).observation_sha256
    import autosport.matchbook_catalog_evidence as m
    exported = " ".join(m.__all__).lower()
    assert "urlopen" not in exported and "login" not in exported and "place_order" not in exported
