from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.prophetx_self_match_wipe import (
    CrossingFact,
    IncomingOrderIntent,
    OwnOrderSnapshot,
    PreSubmitDecision,
    ProphetXSelfMatchConflict,
    ProphetXSelfMatchError,
    RestingOwnOrder,
    SnapshotQuality,
    TrackedOrder,
    TrackedOrderStatus,
    WipeCause,
    WipeIncident,
    WipeObservation,
    WriterCoordination,
    reconcile_wipes,
    screen_pre_submit,
)


def intent(**kw):
    d = dict(
        environment="sandbox",
        account_id="acct-1",
        client_order_id="client-new",
        strike_id="strike-new",
        price="1.91",
        quantity="10",
    )
    d.update(kw)
    return IncomingOrderIntent(**d)


def resting(provider_order_id="rest-1", strike_id="strike-rest", **kw):
    d = dict(
        environment="sandbox",
        account_id="acct-1",
        provider_order_id=provider_order_id,
        strike_id=strike_id,
        open_quantity="7",
    )
    d.update(kw)
    return RestingOwnOrder(**d)


def snapshot(*orders, quality=SnapshotQuality.COMPLETE_CURRENT, **kw):
    d = dict(
        environment="sandbox",
        account_id="acct-1",
        snapshot_id="snap-1",
        quality=quality,
        orders=tuple(orders or (resting(),)),
    )
    d.update(kw)
    return OwnOrderSnapshot(**d)


def fact(i=None, r=None, **kw):
    i = i or intent()
    r = r or resting()
    d = dict(
        snapshot_id="snap-1",
        incoming_fingerprint=i.fingerprint,
        resting_provider_order_id=r.provider_order_id,
        incoming_strike_id=i.strike_id,
        resting_strike_id=r.strike_id,
        would_cross=False,
        rule_version="prophetx-cross-v1",
    )
    d.update(kw)
    return CrossingFact(**d)


def tracked(provider_order_id, canonical_order_id, strike_id, **kw):
    d = dict(
        canonical_order_id=canonical_order_id,
        environment="sandbox",
        account_id="acct-1",
        provider_order_id=provider_order_id,
        strike_id=strike_id,
        order_quantity="10",
        cumulative_filled="0",
        status=TrackedOrderStatus.WORKING,
    )
    d.update(kw)
    return TrackedOrder(**d)


def incident(**kw):
    d = dict(
        environment="sandbox",
        account_id="acct-1",
        incoming_provider_order_id="incoming-1",
        relevant_resting_provider_order_ids=("rest-1",),
    )
    d.update(kw)
    return WipeIncident(**d)


def obs(provider_order_id, strike_id, evidence_id=None, **kw):
    d = dict(
        evidence_id=evidence_id or f"ev-{provider_order_id}",
        environment="sandbox",
        account_id="acct-1",
        provider_order_id=provider_order_id,
        strike_id=strike_id,
        cumulative_filled="0",
    )
    d.update(kw)
    return WipeObservation(**d)


def baselines(**rest_kw):
    return (
        tracked("incoming-1", "canon-in", "strike-new"),
        tracked("rest-1", "canon-rest", "strike-rest", **rest_kw),
    )


@pytest.mark.parametrize(
    "quality",
    [SnapshotQuality.PARTIAL, SnapshotQuality.STALE, SnapshotQuality.UNAVAILABLE],
)
def test_non_current_snapshot_waits_fail_closed(quality):
    out = screen_pre_submit(
        intent(),
        snapshot(quality=quality),
        [],
        writer_coordination=WriterCoordination.PROVEN_PRODUCT_SERIALIZED,
    )
    assert out.decision is PreSubmitDecision.WAIT_READBACK
    assert out.authorizes_write is False
    assert out.residual_concurrency_risk is True


def test_known_crossing_blocks_exact_order():
    i = intent()
    r = resting()
    out = screen_pre_submit(
        i,
        snapshot(r),
        [fact(i, r, would_cross=True)],
        writer_coordination=WriterCoordination.PROVEN_PRODUCT_SERIALIZED,
    )
    assert out.decision is PreSubmitDecision.BLOCK_KNOWN_CONFLICT
    assert out.conflicting_provider_order_ids == ("rest-1",)
    assert out.authorizes_write is False


def test_no_known_conflict_never_becomes_write_authority():
    i = intent()
    r = resting()
    out = screen_pre_submit(
        i,
        snapshot(r),
        [fact(i, r, would_cross=False)],
        writer_coordination=WriterCoordination.PROVEN_PRODUCT_SERIALIZED,
    )
    assert out.decision is PreSubmitDecision.NO_KNOWN_CONFLICT_NOT_GUARANTEED
    assert out.authorizes_write is False
    assert out.requires_immediate_recheck_before_submit is True
    assert out.residual_concurrency_risk is False


def test_external_writer_race_remains_explicit():
    i = intent()
    r = resting()
    out = screen_pre_submit(
        i,
        snapshot(r),
        [fact(i, r)],
        writer_coordination=WriterCoordination.EXTERNAL_WRITERS_POSSIBLE,
    )
    assert out.decision is PreSubmitDecision.NO_KNOWN_CONFLICT_NOT_GUARANTEED
    assert out.residual_concurrency_risk is True



def test_complete_snapshot_with_unassessed_resting_order_waits():
    out = screen_pre_submit(
        intent(),
        snapshot(),
        [],
        writer_coordination=WriterCoordination.PROVEN_PRODUCT_SERIALIZED,
    )
    assert out.decision is PreSubmitDecision.WAIT_READBACK
    assert out.authorizes_write is False
    assert out.residual_concurrency_risk is True

def test_snapshot_scope_mismatch_fails():
    with pytest.raises(ProphetXSelfMatchConflict):
        screen_pre_submit(
            intent(),
            snapshot(account_id="other"),
            [],
            writer_coordination=WriterCoordination.UNKNOWN,
        )


def test_crossing_fact_must_bind_exact_snapshot_and_strikes():
    i = intent()
    r = resting()
    with pytest.raises(ProphetXSelfMatchConflict):
        screen_pre_submit(
            i,
            snapshot(r),
            [fact(i, r, snapshot_id="other")],
            writer_coordination=WriterCoordination.UNKNOWN,
        )
    with pytest.raises(ProphetXSelfMatchConflict):
        screen_pre_submit(
            i,
            snapshot(r),
            [fact(i, r, resting_strike_id="display-name-alias")],
            writer_coordination=WriterCoordination.UNKNOWN,
        )
    with pytest.raises(ProphetXSelfMatchConflict):
        screen_pre_submit(
            i,
            snapshot(r),
            [fact(i, r, incoming_strike_id="other-strike")],
            writer_coordination=WriterCoordination.UNKNOWN,
        )


def test_crossing_fact_for_absent_order_fails():
    i = intent()
    r = resting(provider_order_id="rest-2")
    with pytest.raises(ProphetXSelfMatchConflict):
        screen_pre_submit(
            i,
            snapshot(),
            [fact(i, r)],
            writer_coordination=WriterCoordination.UNKNOWN,
        )


def test_duplicate_crossing_facts_fail_even_if_both_negative():
    i = intent()
    r = resting()
    f = fact(i, r)
    with pytest.raises(ProphetXSelfMatchConflict):
        screen_pre_submit(
            i,
            snapshot(r),
            [f, f],
            writer_coordination=WriterCoordination.UNKNOWN,
        )


def test_incoming_only_wiped_requires_resting_readback_and_does_not_invent_pair():
    out = reconcile_wipes(
        incident(),
        baselines(),
        [obs("incoming-1", "strike-new")],
    )
    assert [e.provider_order_id for e in out.effects] == ["incoming-1"]
    assert out.required_readback_provider_order_ids == ("rest-1",)
    assert out.requires_readback is True
    assert out.cause is WipeCause.WIPED_CAUSE_UNRESOLVED
    assert out.total_released_quantity == Decimal("10")


def test_both_wiped_release_each_order_once():
    out = reconcile_wipes(
        incident(),
        baselines(),
        [
            obs("incoming-1", "strike-new"),
            obs("rest-1", "strike-rest"),
        ],
    )
    assert {e.provider_order_id for e in out.effects} == {"incoming-1", "rest-1"}
    assert out.required_readback_provider_order_ids == ()
    assert out.total_released_quantity == Decimal("20")
    assert all(e.cause is WipeCause.WIPED_CAUSE_UNRESOLVED for e in out.effects)


def test_partial_fill_then_wipe_releases_only_remainder():
    tracked_orders = (
        tracked("incoming-1", "canon-in", "strike-new"),
        tracked(
            "rest-1",
            "canon-rest",
            "strike-rest",
            cumulative_filled="3",
            status=TrackedOrderStatus.PARTIALLY_FILLED,
        ),
    )
    out = reconcile_wipes(
        incident(),
        tracked_orders,
        [
            obs("rest-1", "strike-rest", cumulative_filled="4"),
        ],
    )
    effect = out.effects[0]
    assert effect.filled_quantity == Decimal("4")
    assert effect.released_quantity == Decimal("6")


def test_event_transition_like_wipe_is_not_mislabeled_self_match():
    out = reconcile_wipes(
        incident(),
        baselines(),
        [obs("incoming-1", "strike-new")],
    )
    assert out.cause is WipeCause.WIPED_CAUSE_UNRESOLVED
    assert "SELF_MATCH" not in out.cause.value


def test_exact_replay_is_idempotent_and_projection_stable_across_restart():
    o = obs("incoming-1", "strike-new")
    a = reconcile_wipes(incident(), baselines(), [o])
    b = reconcile_wipes(incident(), baselines(), [o, o])
    c = reconcile_wipes(incident(), baselines(), [o])
    assert a == b == c
    assert a.projection_id == c.projection_id
    assert a.effects[0].effect_id == c.effects[0].effect_id


def test_conflicting_replay_of_same_evidence_id_fails():
    o = obs("incoming-1", "strike-new")
    with pytest.raises(ProphetXSelfMatchConflict):
        reconcile_wipes(
            incident(),
            baselines(),
            [o, replace(o, cumulative_filled=Decimal("1"))],
        )


def test_conflicting_terminal_wipe_state_for_same_order_fails():
    with pytest.raises(ProphetXSelfMatchConflict):
        reconcile_wipes(
            incident(),
            baselines(),
            [
                obs("incoming-1", "strike-new", evidence_id="ev-1"),
                obs(
                    "incoming-1",
                    "strike-new",
                    evidence_id="ev-2",
                    cumulative_filled="1",
                ),
            ],
        )


def test_same_terminal_state_with_distinct_evidence_ids_is_not_double_counted():
    a = obs("incoming-1", "strike-new", evidence_id="ev-1")
    b = obs("incoming-1", "strike-new", evidence_id="ev-2")
    out = reconcile_wipes(incident(), baselines(), [a, b])
    assert len(out.effects) == 1
    assert out.total_released_quantity == Decimal("10")


def test_observation_cannot_reduce_prior_known_fill_or_exceed_quantity():
    tracked_orders = (
        tracked("incoming-1", "canon-in", "strike-new"),
        tracked(
            "rest-1",
            "canon-rest",
            "strike-rest",
            cumulative_filled="3",
            status=TrackedOrderStatus.PARTIALLY_FILLED,
        ),
    )
    with pytest.raises(ProphetXSelfMatchConflict):
        reconcile_wipes(
            incident(),
            tracked_orders,
            [obs("rest-1", "strike-rest", cumulative_filled="2")],
        )
    with pytest.raises(ProphetXSelfMatchConflict):
        reconcile_wipes(
            incident(),
            baselines(),
            [obs("rest-1", "strike-rest", cumulative_filled="11")],
        )


def test_unrelated_or_cross_account_wipe_cannot_join_incident():
    with pytest.raises(ProphetXSelfMatchConflict):
        reconcile_wipes(
            incident(),
            baselines(),
            [obs("other-order", "strike-x")],
        )
    with pytest.raises(ProphetXSelfMatchConflict):
        reconcile_wipes(
            incident(),
            baselines(),
            [obs("rest-1", "strike-rest", account_id="acct-2")],
        )


def test_strike_identity_mismatch_fails_closed():
    with pytest.raises(ProphetXSelfMatchConflict):
        reconcile_wipes(
            incident(),
            baselines(),
            [obs("rest-1", "wrong-strike")],
        )


def test_missing_incident_baseline_fails():
    with pytest.raises(ProphetXSelfMatchConflict):
        reconcile_wipes(
            incident(),
            [tracked("incoming-1", "canon-in", "strike-new")],
            [],
        )


def test_separate_accounts_cannot_share_one_incident():
    wrong = tracked(
        "rest-1",
        "canon-rest",
        "strike-rest",
        account_id="acct-2",
    )
    with pytest.raises(ProphetXSelfMatchConflict):
        reconcile_wipes(
            incident(),
            (
                tracked("incoming-1", "canon-in", "strike-new"),
                wrong,
            ),
            [],
        )


def test_full_fill_then_wipe_has_zero_release_not_negative():
    out = reconcile_wipes(
        incident(),
        baselines(),
        [obs("incoming-1", "strike-new", cumulative_filled="10")],
    )
    assert out.effects[0].released_quantity == Decimal("0")


def test_exact_decimal_ingress_rejects_float_and_bool():
    with pytest.raises(ProphetXSelfMatchError):
        intent(price=1.91)
    with pytest.raises(ProphetXSelfMatchError):
        tracked(
            "incoming-1",
            "canon-in",
            "strike-new",
            order_quantity=True,
        )


def test_working_and_partial_fill_baseline_invariants():
    with pytest.raises(ProphetXSelfMatchError):
        tracked(
            "x",
            "c",
            "s",
            cumulative_filled="1",
            status=TrackedOrderStatus.WORKING,
        )
    with pytest.raises(ProphetXSelfMatchError):
        tracked(
            "x",
            "c",
            "s",
            cumulative_filled="0",
            status=TrackedOrderStatus.PARTIALLY_FILLED,
        )


def test_duplicate_resting_ids_in_snapshot_fail():
    r = resting()
    with pytest.raises(ProphetXSelfMatchConflict):
        snapshot(r, r)


def test_incident_requires_distinct_nonempty_resting_ids():
    with pytest.raises(ProphetXSelfMatchError):
        incident(relevant_resting_provider_order_ids=())
    with pytest.raises(ProphetXSelfMatchError):
        incident(relevant_resting_provider_order_ids=("incoming-1",))
    with pytest.raises(ProphetXSelfMatchError):
        incident(relevant_resting_provider_order_ids=("rest-1", "rest-1"))
