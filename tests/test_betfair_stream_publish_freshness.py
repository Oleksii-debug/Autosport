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
    BetfairStreamFreshnessDecision,
    BetfairStreamFreshnessPolicy,
    BetfairStreamFreshnessVerdict,
    BetfairStreamPublishFreshnessRuntime,
    BetfairStreamSubscriptionContext,
)


def sha(c='a'): return c * 64


def context(
    *,
    generation=1,
    conflate_ms=0,
    subscription_id='sub-1',
    provider_request_id=None,
    market_filter_sha256=None,
    market_data_fields=('EX_BEST_OFFERS',),
    ladder_levels=3,
):
    if provider_request_id is None:
        provider_request_id = 6 + generation
    if market_filter_sha256 is None:
        market_filter_sha256 = sha('c')
    return BetfairStreamSubscriptionContext(
        upstream_context_sha256=sha('b'),
        subscription_id=subscription_id,
        provider_request_id=provider_request_id,
        subscription_generation=generation,
        criteria_sha256=sha(),
        market_filter_sha256=market_filter_sha256,
        market_data_fields=market_data_fields,
        ladder_levels=ladder_levels,
        requested_heartbeat_ms=5000,
        requested_conflate_ms=conflate_ms,
    )


def image(
    *,
    market='1.A',
    selection=1,
    price=2.0,
    pt=1000,
    clk='c1',
    initial='i1',
    con=False,
    status=None,
    request_id=7,
    provider_conflate_ms=0,
    provider_heartbeat_ms=5000,
):
    raw = {
        'op': 'mcm',
        'id': request_id,
        'ct': 'SUB_IMAGE',
        'initialClk': initial,
        'clk': clk,
        'pt': pt,
        'conflateMs': provider_conflate_ms,
        'heartbeatMs': provider_heartbeat_ms,
        'mc': [
            {
                'id': market,
                'img': True,
                'con': con,
                'rc': [{'id': selection, 'hc': 0, 'ltp': price}],
            }
        ],
    }
    if status is not None:
        raw['status'] = status
    return raw


def delta(
    *,
    market='1.A',
    selection=1,
    price=2.1,
    pt=1001,
    clk='c2',
    con=False,
    status=None,
    image=False,
    ct=None,
    initial=None,
    request_id=7,
    provider_conflate_ms=0,
    provider_heartbeat_ms=5000,
):
    raw = {
        'op': 'mcm',
        'id': request_id,
        'clk': clk,
        'pt': pt,
        'conflateMs': provider_conflate_ms,
        'heartbeatMs': provider_heartbeat_ms,
        'mc': [
            {
                'id': market,
                'img': image,
                'con': con,
                'rc': [{'id': selection, 'hc': 0, 'ltp': price}],
            }
        ],
    }
    if ct is not None:
        raw['ct'] = ct
    if initial is not None:
        raw['initialClk'] = initial
    if status is not None:
        raw['status'] = status
    return raw


def heartbeat(
    *,
    pt=1001,
    clk='hb',
    status=None,
    request_id=7,
    provider_conflate_ms=0,
    provider_heartbeat_ms=5000,
):
    raw = {
        'op': 'mcm',
        'id': request_id,
        'ct': 'HEARTBEAT',
        'initialClk': 'i1',
        'clk': clk,
        'pt': pt,
        'conflateMs': provider_conflate_ms,
        'heartbeatMs': provider_heartbeat_ms,
        'mc': [],
    }
    if status is not None:
        raw['status'] = status
    return raw


def ltp_identity(market='1.A', selection=1):
    return BetfairQuoteIdentity(BETFAIR_STREAM_SOURCE_ID, market, selection, Decimal('0'), BetfairQuoteSide.LAST_TRADED, None)


def test_positive_provider_publish_is_issued_only_through_raw_canonical_codec_state_path():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    issued = rt.ingest_raw(image(), received_time_ms=1001, ingested_time_ms=1002)
    assert len(issued) == 1
    d = rt.evaluate(ltp_identity(), as_of_ms=1005, policy=BetfairStreamFreshnessPolicy(10))
    assert d.verdict is BetfairStreamFreshnessVerdict.AUTH_CONTEXT_UNPROVEN
    assert d.age_ms == 5
    assert not d.decision_eligible
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
    d = rt.evaluate(ltp_identity(), as_of_ms=1003, policy=BetfairStreamFreshnessPolicy(100))
    assert d.verdict is BetfairStreamFreshnessVerdict.AUTH_CONTEXT_UNPROVEN
    assert not d.decision_eligible


def test_conflated_change_supersedes_exact_record_without_minting_new_exact_age():
    rt = BetfairStreamPublishFreshnessRuntime(context(conflate_ms=1000))
    rt.ingest_raw(image(price=2.0), received_time_ms=1000, ingested_time_ms=1000)
    issued = rt.ingest_raw(delta(price=2.2, pt=1010, con=True), received_time_ms=1010, ingested_time_ms=1010)
    assert issued == ()
    # LAST_TRADED identity is price-independent, so changed datum invalidation is decisive.
    assert rt.resolve(ltp_identity()) is None


def test_delayed_180s_provider_timing_cannot_pass_60s_freshness_gate_even_with_recent_pt():
    rt = BetfairStreamPublishFreshnessRuntime(context(conflate_ms=0))
    rt.ingest_raw(
        image(provider_conflate_ms=180_000),
        received_time_ms=1000,
        ingested_time_ms=1000,
    )
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
    assert bounded.verdict is BetfairStreamFreshnessVerdict.AUTH_CONTEXT_UNPROVEN
    assert not bounded.decision_eligible


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
    refreshed = restarted.evaluate(
        ltp_identity(),
        as_of_ms=1006,
        policy=BetfairStreamFreshnessPolicy(10),
    )
    assert refreshed.verdict is BetfairStreamFreshnessVerdict.AUTH_CONTEXT_UNPROVEN
    assert not refreshed.decision_eligible


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
    d1 = r1.evaluate(ltp_identity(), as_of_ms=1005, policy=BetfairStreamFreshnessPolicy(100))
    assert d1.verdict is BetfairStreamFreshnessVerdict.AUTH_CONTEXT_UNPROVEN
    assert not d1.decision_eligible
    assert r2.evaluate(ltp_identity(), as_of_ms=1005, policy=BetfairStreamFreshnessPolicy(100)).verdict is BetfairStreamFreshnessVerdict.UNKNOWN


def test_missing_provider_request_id_is_rejected_before_state_mutation():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    raw = image()
    raw.pop('id')
    with pytest.raises(ValueError, match='lacks provider request id'):
        rt.ingest_raw(raw, received_time_ms=1000, ingested_time_ms=1000)
    assert rt.resolve(ltp_identity()) is None


def test_mismatched_provider_request_id_is_rejected_before_state_mutation():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    with pytest.raises(ValueError, match='another subscription'):
        rt.ingest_raw(
            image(request_id=99),
            received_time_ms=1000,
            ingested_time_ms=1000,
        )
    assert rt.resolve(ltp_identity()) is None
    issued = rt.ingest_raw(image(), received_time_ms=1000, ingested_time_ms=1000)
    assert len(issued) == 1


def test_subscription_generation_cannot_reuse_provider_request_id():
    rt = BetfairStreamPublishFreshnessRuntime(context(generation=1, provider_request_id=7))
    with pytest.raises(ValueError, match='new provider request id'):
        rt.replace_subscription(
            context(
                generation=2,
                subscription_id='sub-2',
                provider_request_id=7,
            )
        )


def test_delayed_message_from_retired_subscription_has_zero_effect():
    rt = BetfairStreamPublishFreshnessRuntime(context(generation=1, provider_request_id=7))
    rt.ingest_raw(image(request_id=7), received_time_ms=1000, ingested_time_ms=1000)
    rt.replace_subscription(
        context(
            generation=2,
            subscription_id='sub-2',
            provider_request_id=8,
        )
    )
    assert rt.resolve(ltp_identity()) is None
    with pytest.raises(ValueError, match='another subscription'):
        rt.ingest_raw(
            delta(request_id=7, pt=1010, clk='late-a'),
            received_time_ms=1010,
            ingested_time_ms=1010,
        )
    assert rt.resolve(ltp_identity()) is None
    issued = rt.ingest_raw(
        image(request_id=8, pt=1011, clk='b1', initial='bi'),
        received_time_ms=1011,
        ingested_time_ms=1011,
    )
    assert len(issued) == 1
    assert issued[0].provider_request_id == 8


def test_server_reported_conflation_overrides_optimistic_requested_configuration():
    rt = BetfairStreamPublishFreshnessRuntime(context(conflate_ms=0))
    rec = rt.ingest_raw(
        image(provider_conflate_ms=180_000),
        received_time_ms=1000,
        ingested_time_ms=1000,
    )[0]
    assert rec.requested_conflate_ms == 0
    assert rec.provider_conflate_ms == 180_000
    d = rt.evaluate(
        ltp_identity(),
        as_of_ms=1001,
        policy=BetfairStreamFreshnessPolicy(60_000),
    )
    assert d.verdict is BetfairStreamFreshnessVerdict.UNKNOWN
    assert 'provider-reported conflation' in d.reason


def test_missing_provider_reported_timing_cannot_mint_current_freshness():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    rec = rt.ingest_raw(
        image(provider_conflate_ms=None, provider_heartbeat_ms=None),
        received_time_ms=1000,
        ingested_time_ms=1000,
    )[0]
    assert rec.provider_conflate_ms is None
    assert rec.provider_heartbeat_ms is None
    d = rt.evaluate(
        ltp_identity(),
        as_of_ms=1001,
        policy=BetfairStreamFreshnessPolicy(100),
    )
    assert d.verdict is BetfairStreamFreshnessVerdict.UNKNOWN
    assert 'provider-reported stream timing is unavailable' in d.reason


def test_provider_timing_change_invalidates_prior_datum_until_republished():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    rt.ingest_raw(image(), received_time_ms=1000, ingested_time_ms=1000)
    assert rt.resolve(ltp_identity()) is not None
    rt.ingest_raw(
        heartbeat(pt=1001, clk='timing-change', provider_conflate_ms=1000),
        received_time_ms=1001,
        ingested_time_ms=1001,
    )
    assert rt.resolve(ltp_identity()) is None
    assert rt.evaluate(
        ltp_identity(),
        as_of_ms=1001,
        policy=BetfairStreamFreshnessPolicy(10_000),
    ).verdict is BetfairStreamFreshnessVerdict.UNKNOWN


def test_plaintext_upstream_auth_label_is_rejected_as_persisted_provenance():
    with pytest.raises(ValueError, match='upstream_context_sha256'):
        BetfairStreamSubscriptionContext(
            upstream_context_sha256='betfair-prod',
            subscription_id='sub-1',
            provider_request_id=7,
            subscription_generation=1,
            criteria_sha256=sha(),
            market_filter_sha256=sha('c'),
            market_data_fields=('EX_BEST_OFFERS',),
            ladder_levels=3,
            requested_heartbeat_ms=5000,
            requested_conflate_ms=0,
        )


def test_direct_fresh_decision_dto_is_never_decision_eligible_without_origin_authority():
    d = BetfairStreamFreshnessDecision(
        verdict=BetfairStreamFreshnessVerdict.FRESH_PROVIDER_PUBLISH,
        reason='caller-created',
        evidence_id=sha('c'),
        age_ms=0,
        policy_id=sha('d'),
        context_id=sha('e'),
    )
    assert not d.decision_eligible


def test_persisted_context_uses_secret_free_digest_and_distinguishes_requested_timing():
    rt = BetfairStreamPublishFreshnessRuntime(context(conflate_ms=1234))
    rt.ingest_raw(
        image(provider_conflate_ms=4321),
        received_time_ms=1000,
        ingested_time_ms=1000,
    )
    payload = rt.to_dict()
    ctx = payload['context']
    assert ctx['upstream_context_sha256'] == sha('b')
    assert ctx['requested_conflate_ms'] == 1234
    assert 'authenticated_context_id' not in ctx
    assert 'conflate_ms' not in ctx
    row = payload['records'][0]
    assert row['provider_conflate_ms'] == 4321
    assert row['requested_conflate_ms'] == 1234


def test_subscription_projection_axes_are_bound_into_context_and_evidence_identity():
    base = context()
    display = context(market_data_fields=('EX_BEST_OFFERS_DISP',))
    deeper = context(ladder_levels=10)
    another_filter = context(market_filter_sha256=sha('d'))

    assert len({base.context_id, display.context_id, deeper.context_id, another_filter.context_id}) == 4

    rt = BetfairStreamPublishFreshnessRuntime(base)
    rec = rt.ingest_raw(image(), received_time_ms=1000, ingested_time_ms=1000)[0]
    assert rec.market_filter_sha256 == sha('c')
    assert rec.market_data_fields == ('EX_BEST_OFFERS',)
    assert rec.ladder_levels == 3

    display_rt = BetfairStreamPublishFreshnessRuntime(display)
    display_rec = display_rt.ingest_raw(
        image(),
        received_time_ms=1000,
        ingested_time_ms=1000,
    )[0]
    assert display_rec.evidence_id != rec.evidence_id


@pytest.mark.parametrize(
    ('fields', 'ladder_levels', 'match'),
    [
        (('EX_BEST_OFFERS', 'EX_BEST_OFFERS'), 3, 'sorted and contain no duplicates'),
        (('EX_LTP', 'EX_BEST_OFFERS'), 3, 'sorted and contain no duplicates'),
        (('EX_BEST_OFFERS',), None, 'ladder_levels is required'),
        (('EX_LTP',), 3, 'only authoritative for best-offer'),
    ],
)
def test_subscription_projection_rejects_ambiguous_field_depth_contract(
    fields,
    ladder_levels,
    match,
):
    with pytest.raises(ValueError, match=match):
        context(market_data_fields=fields, ladder_levels=ladder_levels)


@pytest.mark.parametrize('ladder_levels', [0, 11, True, 3.0])
def test_subscription_projection_rejects_noncanonical_ladder_levels(ladder_levels):
    with pytest.raises(ValueError, match='ladder_levels'):
        context(ladder_levels=ladder_levels)


def test_subscription_projection_market_filter_requires_canonical_digest():
    with pytest.raises(ValueError, match='market_filter_sha256'):
        context(market_filter_sha256='caller-market-filter')


def test_restart_payload_projection_tamper_is_rejected():
    rt = BetfairStreamPublishFreshnessRuntime(context())
    rt.ingest_raw(image(), received_time_ms=1000, ingested_time_ms=1000)
    payload = rt.to_dict()
    payload['context']['ladder_levels'] = 10
    with pytest.raises(ValueError, match='context_id'):
        BetfairStreamPublishFreshnessRuntime.from_dict(payload)


def test_subscription_replacement_with_new_projection_clears_prior_datum_authority():
    rt = BetfairStreamPublishFreshnessRuntime(context(generation=1, provider_request_id=7))
    rt.ingest_raw(image(request_id=7), received_time_ms=1000, ingested_time_ms=1000)
    assert rt.resolve(ltp_identity()) is not None

    rt.replace_subscription(
        context(
            generation=2,
            subscription_id='sub-2',
            provider_request_id=8,
            market_data_fields=('EX_BEST_OFFERS_DISP',),
        )
    )
    assert rt.resolve(ltp_identity()) is None
    issued = rt.ingest_raw(
        image(request_id=8, pt=1001, clk='p2', initial='p2i'),
        received_time_ms=1001,
        ingested_time_ms=1001,
    )
    assert issued[0].market_data_fields == ('EX_BEST_OFFERS_DISP',)
    assert not rt.evaluate(
        ltp_identity(),
        as_of_ms=1001,
        policy=BetfairStreamFreshnessPolicy(100),
    ).decision_eligible
