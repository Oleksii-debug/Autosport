from copy import deepcopy
from decimal import Decimal

import pytest

from autosport.betfair_stream_codec import (
    BETFAIR_STREAM_SOURCE_ID,
    BetfairMarketChangeFrame,
    BetfairQuoteIdentity,
    BetfairQuoteSide,
    BetfairStreamApplyResult,
)
from autosport.betfair_stream_publish_freshness import (
    BetfairStreamFreshnessPolicy,
    BetfairStreamFreshnessVerdict,
    BetfairStreamPublishFreshnessRuntime,
    BetfairStreamSubscriptionContext,
)


def sha(c='a'): return c * 64


def context(*, generation=1, conflate_ms=0, subscription_id='sub-1'):
    return BetfairStreamSubscriptionContext(
        authenticated_context_id='betfair-auth-context-1',
        subscription_id=subscription_id,
        subscription_generation=generation,
        criteria_sha256=sha(),
        heartbeat_ms=5000,
        conflate_ms=conflate_ms,
    )


def image(*, market='1.A', selection=1, price=2.0, pt=1000, clk='c1', initial='i1', con=False, status=None):
    raw = {
        'op': 'mcm', 'ct': 'SUB_IMAGE', 'initialClk': initial, 'clk': clk,
        'pt': pt, 'con': con,
        'mc': [{'id': market, 'img': True, 'rc': [{'id': selection, 'hc': 0, 'ltp': price}]}],
    }
    if status is not None: raw['status'] = status
    return raw


def delta(*, market='1.A', selection=1, price=2.1, pt=1001, clk='c2', con=False, status=None, image=False, ct=None, initial=None):
    raw = {
        'op': 'mcm', 'clk': clk, 'pt': pt, 'con': con,
        'mc': [{'id': market, 'img': image, 'rc': [{'id': selection, 'hc': 0, 'ltp': price}]}],
    }
    if ct is not None: raw['ct'] = ct
    if initial is not None: raw['initialClk'] = initial
    if status is not None: raw['status'] = status
    return raw


def heartbeat(*, pt=1001, clk='hb', status=None):
    raw = {'op': 'mcm', 'ct': 'HEARTBEAT', 'initialClk': 'i1', 'clk': clk, 'pt': pt, 'mc': []}
    if status is not None: raw['status'] = status
    return raw


def ltp_identity(market='1.A', selection=1):
    return BetfairQuoteIdentity(BETFAIR_STREAM_SOURCE_ID, market, selection, Decimal('0'), BetfairQuoteSide.LAST_TRADED, None)


def test_positive_provider_publish_is_issued_only_through_raw_canonical_codec_state_path():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    issued = rt.ingest_raw(image(), received_time_ms=1001, ingested_time_ms=1002)
    assert len(issued) == 1
    d = rt.evaluate(ltp_identity(), as_of_ms=1005, policy=BetfairStreamFreshnessPolicy(10))
    assert d.verdict is BetfairStreamFreshnessVerdict.FRESH_PROVIDER_PUBLISH
    assert d.age_ms == 5
    # Public runtime deliberately exposes no decoded-frame/apply-result mutation seam.
    assert not hasattr(rt, 'observe')
    assert not hasattr(rt, 'ingest_result')


def test_caller_created_decoded_dto_and_shadow_result_have_no_public_authority_path():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    assert not any(name for name in dir(rt) if name in {'observe', 'apply_result', 'ingest_result'})
    # Merely creating canonical-looking DTOs cannot modify the runtime ledger.
    fake_frame = BetfairMarketChangeFrame.__new__(BetfairMarketChangeFrame)
    fake_result = BetfairStreamApplyResult.__new__(BetfairStreamApplyResult)
    assert fake_frame is not None and fake_result is not None
    assert rt.resolve(ltp_identity()) is None


def test_heartbeat_never_refreshes_quote_publication_time():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    rec = rt.ingest_raw(image(), received_time_ms=1000, ingested_time_ms=1000)[0]
    rt.ingest_raw(heartbeat(pt=1010), received_time_ms=1010, ingested_time_ms=1010)
    assert rt.resolve(ltp_identity()) == rec
    d = rt.evaluate(ltp_identity(), as_of_ms=1012, policy=BetfairStreamFreshnessPolicy(10))
    assert d.verdict is BetfairStreamFreshnessVerdict.STALE
    assert d.age_ms == 12


def test_unrelated_market_delta_does_not_retimestamp_existing_quote():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    a = rt.ingest_raw(image(market='1.A'), received_time_ms=1000, ingested_time_ms=1000)[0]
    rt.ingest_raw(delta(market='1.B', selection=2, pt=1005), received_time_ms=1005, ingested_time_ms=1005)
    assert rt.resolve(ltp_identity('1.A', 1)) == a
    assert rt.resolve(ltp_identity('1.B', 2)).publish_time_ms == 1005


def test_503_invalidates_cached_positive_and_healthy_heartbeat_does_not_restore_it():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    rt.ingest_raw(image(), received_time_ms=1000, ingested_time_ms=1000)
    rt.ingest_raw(heartbeat(pt=1001, clk='hb1', status=503), received_time_ms=1001, ingested_time_ms=1001)
    assert rt.resolve(ltp_identity()) is None
    rt.ingest_raw(heartbeat(pt=1002, clk='hb2'), received_time_ms=1002, ingested_time_ms=1002)
    assert rt.evaluate(ltp_identity(), as_of_ms=1002, policy=BetfairStreamFreshnessPolicy(100)).verdict is BetfairStreamFreshnessVerdict.UNKNOWN
    rt.ingest_raw(delta(pt=1003, clk='c3'), received_time_ms=1003, ingested_time_ms=1003)
    assert rt.evaluate(ltp_identity(), as_of_ms=1003, policy=BetfairStreamFreshnessPolicy(100)).decision_eligible


def test_conflated_change_supersedes_exact_record_without_minting_new_exact_age():
    rt = BetfairStreamPublishFreshnessRuntime(context(conflate_ms=1000))
    rt.ingest_raw(image(price=2.0), received_time_ms=1000, ingested_time_ms=1000)
    issued = rt.ingest_raw(delta(price=2.2, pt=1010, con=True), received_time_ms=1010, ingested_time_ms=1010)
    assert issued == ()
    # LAST_TRADED identity is price-independent, so changed datum invalidation is decisive.
    assert rt.resolve(ltp_identity()) is None


def test_delayed_180s_context_cannot_pass_60s_freshness_gate_even_with_recent_pt():
    rt = BetfairStreamPublishFreshnessRuntime(context(conflate_ms=180_000))
    rt.ingest_raw(image(), received_time_ms=1000, ingested_time_ms=1000)
    d = rt.evaluate(ltp_identity(), as_of_ms=1001, policy=BetfairStreamFreshnessPolicy(60_000))
    assert d.verdict is BetfairStreamFreshnessVerdict.UNKNOWN
    assert 'conflation' in d.reason


def test_resub_delta_changes_only_patched_datum_and_keeps_other_publish_time():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    raw = image(market='1.A', selection=1)
    raw['mc'].append({'id': '1.B', 'img': True, 'rc': [{'id': 2, 'hc': 0, 'ltp': 3.0}]})
    first = rt.ingest_raw(raw, received_time_ms=1000, ingested_time_ms=1000)
    by_market = {r.quote.identity.market_id: r for r in first}
    rt.ingest_raw(delta(market='1.B', selection=2, price=3.2, pt=1010, clk='c2', ct='RESUB_DELTA', initial='i2'), received_time_ms=1010, ingested_time_ms=1010)
    assert rt.resolve(ltp_identity('1.A', 1)) == by_market['1.A']
    assert rt.resolve(ltp_identity('1.B', 2)).publish_time_ms == 1010


def test_completed_market_image_replacement_drops_absent_old_quote():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    raw = image(market='1.A', selection=1)
    raw['mc'][0]['rc'].append({'id': 2, 'hc': 0, 'ltp': 3.0})
    rt.ingest_raw(raw, received_time_ms=1000, ingested_time_ms=1000)
    rt.ingest_raw(delta(market='1.A', selection=1, price=2.2, pt=1010, image=True), received_time_ms=1010, ingested_time_ms=1010)
    assert rt.resolve(ltp_identity('1.A', 2)) is None
    assert rt.resolve(ltp_identity('1.A', 1)).publish_time_ms == 1010


def test_subscription_replacement_clears_old_authority_and_generation_cannot_roll_back():
    rt = BetfairStreamPublishFreshnessRuntime(context(generation=1))
    rt.ingest_raw(image(), received_time_ms=1000, ingested_time_ms=1000)
    rt.replace_subscription(context(generation=2, subscription_id='sub-2'))
    assert rt.resolve(ltp_identity()) is None
    with pytest.raises(ValueError, match='strictly advance'):
        rt.replace_subscription(context(generation=1, subscription_id='old'))


def test_provider_publish_receive_clock_skew_requires_explicit_bounded_policy():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    rt.ingest_raw(image(pt=1001), received_time_ms=1000, ingested_time_ms=1002)
    strict = rt.evaluate(ltp_identity(), as_of_ms=1002, policy=BetfairStreamFreshnessPolicy(10, 0))
    assert strict.verdict is BetfairStreamFreshnessVerdict.FUTURE
    bounded = rt.evaluate(ltp_identity(), as_of_ms=1002, policy=BetfairStreamFreshnessPolicy(10, 1))
    assert bounded.decision_eligible


def test_restart_restores_exact_audit_record_but_quarantines_positive_until_resync():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    rec = rt.ingest_raw(image(), received_time_ms=1001, ingested_time_ms=1002)[0]
    payload = rt.to_dict()
    restarted = BetfairStreamPublishFreshnessRuntime.from_dict(deepcopy(payload))
    assert restarted.resolve(ltp_identity()) == rec
    d = restarted.evaluate(ltp_identity(), as_of_ms=1005, policy=BetfairStreamFreshnessPolicy(10))
    assert d.verdict is BetfairStreamFreshnessVerdict.UNKNOWN
    assert 'resynchronization' in d.reason
    # New canonical image re-establishes the datum rather than restart itself doing so.
    restarted.ingest_raw(image(pt=1006, clk='newc', initial='newi'), received_time_ms=1006, ingested_time_ms=1006)
    assert restarted.evaluate(ltp_identity(), as_of_ms=1006, policy=BetfairStreamFreshnessPolicy(10)).decision_eligible


def test_restart_payload_tamper_is_rejected():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    rt.ingest_raw(image(), received_time_ms=1001, ingested_time_ms=1002)
    payload = rt.to_dict()
    payload['records'][0]['publish_time_ms'] = 999
    with pytest.raises(ValueError, match='invalid publication record'):
        BetfairStreamPublishFreshnessRuntime.from_dict(payload)


def test_restart_payload_cannot_drop_last_publish_regression_fence():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    rt.ingest_raw(image(), received_time_ms=1001, ingested_time_ms=1002)
    payload = rt.to_dict()
    payload['last_publish_time_ms'] = None
    with pytest.raises(ValueError, match='require last_publish_time_ms'):
        BetfairStreamPublishFreshnessRuntime.from_dict(payload)


def test_segmented_message_is_rejected_by_canonical_codec_before_freshness():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    raw = image(); raw['segmentType'] = 'SEG_START'
    with pytest.raises(ValueError, match='reassembly'):
        rt.ingest_raw(raw, received_time_ms=1000, ingested_time_ms=1000)


def test_same_provider_pt_with_different_receive_latency_has_same_publish_boundary_but_distinct_evidence():
    r1 = BetfairStreamPublishFreshnessRuntime(context())
    r2 = BetfairStreamPublishFreshnessRuntime(context())
    e1 = r1.ingest_raw(image(), received_time_ms=1001, ingested_time_ms=1002)[0]
    e2 = r2.ingest_raw(image(), received_time_ms=1010, ingested_time_ms=1011)[0]
    assert e1.publish_time_ms == e2.publish_time_ms == 1000
    assert e1.frame_sha256 == e2.frame_sha256
    assert e1.evidence_id != e2.evidence_id
    assert r1.evaluate(ltp_identity(), as_of_ms=1005, policy=BetfairStreamFreshnessPolicy(100)).decision_eligible
    assert r2.evaluate(ltp_identity(), as_of_ms=1005, policy=BetfairStreamFreshnessPolicy(100)).verdict is BetfairStreamFreshnessVerdict.UNKNOWN
