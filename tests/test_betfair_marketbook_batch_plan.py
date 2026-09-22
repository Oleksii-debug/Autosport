import json

import pytest

from autosport.betfair_marketbook_batch_plan import (
    MarketBookBatchPlanError,
    MarketBookReadPlan,
)
from autosport.betfair_marketbook_request_budget import MarketBookRequestBudget


def mids(count: int) -> tuple[str, ...]:
    return tuple(f"1.{index:09d}" for index in range(count))


def plan(count: int, price_data=(), **kwargs) -> MarketBookReadPlan:
    return MarketBookReadPlan(mids(count), "OPEN", tuple(price_data), **kwargs)


def sizes(p: MarketBookReadPlan) -> list[int]:
    return [len(batch.market_ids) for batch in p.batches]


def test_all_offers_overflow_is_split_without_dropping_markets():
    p = plan(12, ("EX_ALL_OFFERS",))
    assert sizes(p) == [11, 1]
    assert tuple(mid for batch in p.batches for mid in batch.market_ids) == p.market_ids
    assert all(batch.total_points["numerator"] <= 200 * batch.total_points["denominator"] for batch in p.batches)


def test_documented_projection_boundaries_drive_batch_sizes_via_parent_authority():
    assert sizes(plan(7, ("EX_ALL_OFFERS", "EX_TRADED"))) == [6, 1]
    assert sizes(plan(41, ("EX_BEST_OFFERS",))) == [40, 1]
    assert sizes(plan(101)) == [100, 1]
    assert sizes(plan(13, ("EX_BEST_OFFERS",), best_prices_depth=10)) == [12, 1]


def test_each_batch_binds_exact_parent_budget_evidence():
    p = plan(12, ("EX_ALL_OFFERS",))
    for batch in p.batches:
        parent = MarketBookRequestBudget(batch.market_ids, p.price_data, p.best_prices_depth)
        assert parent.allowed is True
        assert batch.budget_evidence_id == parent.evidence_id
        assert batch.total_points == {
            "numerator": parent.total_points.numerator,
            "denominator": parent.total_points.denominator,
        }


def test_input_order_is_canonical_but_duplicates_fail_closed():
    a = MarketBookReadPlan(("1.3", "1.1", "1.2"), "OPEN", ("EX_BEST_OFFERS",))
    b = MarketBookReadPlan(("1.2", "1.3", "1.1"), "OPEN", ("EX_BEST_OFFERS",))
    assert a.plan_id == b.plan_id
    assert a.market_ids == ("1.1", "1.2", "1.3")
    with pytest.raises(MarketBookBatchPlanError, match="duplicates"):
        MarketBookReadPlan(("1.1", "1.1"), "OPEN")


def test_open_and_closed_truth_are_separate_plan_identities():
    open_plan = plan(3, ("EX_BEST_OFFERS",))
    closed_plan = MarketBookReadPlan(mids(3), "CLOSED", ("EX_BEST_OFFERS",))
    assert open_plan.plan_id != closed_plan.plan_id
    with pytest.raises(MarketBookBatchPlanError, match="OPEN or CLOSED"):
        MarketBookReadPlan(mids(1), "OPEN+CLOSED")


def test_response_shaping_request_fields_are_hash_bound():
    base = plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3)
    variants = [
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, virtualise=True),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, rollover_stakes=True),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, order_projection="EXECUTABLE"),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, match_projection="NO_ROLLUP"),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, include_overall_position=True),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, partition_matched_by_strategy_ref=True),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, customer_strategy_refs=("s1",)),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, matched_since="2026-09-22T00:00:00Z"),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, bet_ids=("bet-1",)),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, currency_code="EUR"),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, locale="en"),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, rollup_model="STAKE", rollup_limit=5),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, provider_scope_id="BETFAIR:APP-A"),
        plan(3, ("EX_BEST_OFFERS",), best_prices_depth=3, policy_version="v2"),
    ]
    assert len({base.plan_id, *(item.plan_id for item in variants)}) == len(variants) + 1


def test_best_offer_overrides_follow_provider_applicability_and_fail_closed():
    valid = plan(2, ("EX_BEST_OFFERS",), virtualise=True, rollover_stakes=True, rollup_model="PAYOUT", rollup_limit=10)
    assert valid.request_contract_payload["virtualise"] is True
    assert valid.request_contract_payload["rollover_stakes"] is True
    assert valid.request_contract_payload["ex_best_offers_overrides"] == {
        "rollup_model": "PAYOUT", "rollup_limit": 10
    }
    with pytest.raises(MarketBookBatchPlanError, match="rollup_limit is required"):
        plan(1, ("EX_BEST_OFFERS",), rollup_model="STAKE")
    with pytest.raises(MarketBookBatchPlanError, match="ignored by Betfair"):
        plan(1, ("EX_BEST_OFFERS",), rollup_limit=10)
    with pytest.raises(MarketBookBatchPlanError, match="unsupported rollup_model"):
        plan(1, ("EX_BEST_OFFERS",), rollup_model="RISK", rollup_limit=10)
    with pytest.raises(MarketBookBatchPlanError, match="effective EX_BEST_OFFERS"):
        plan(1, ("EX_ALL_OFFERS",), rollup_model="STAKE", rollup_limit=10)
    with pytest.raises(MarketBookBatchPlanError, match="exchange-offers"):
        plan(1, ("SP_TRADED",), virtualise=True)
    with pytest.raises(MarketBookBatchPlanError, match="exchange-offers"):
        plan(1, ("SP_TRADED",), rollover_stakes=True)


def test_order_and_match_projection_unknown_values_fail_closed():
    with pytest.raises(MarketBookBatchPlanError, match="unsupported order_projection"):
        plan(1, order_projection="EVERYTHING")
    with pytest.raises(MarketBookBatchPlanError, match="unsupported match_projection"):
        plan(1, match_projection="RAW")


def test_combined_identifier_limit_can_be_stricter_than_weight_budget():
    bet_ids = tuple(f"bet-{index:03d}" for index in range(240))
    p = plan(21, (), bet_ids=bet_ids)
    assert sizes(p) == [10, 10, 1]
    assert all(batch.combined_identifier_count <= 250 for batch in p.batches)
    with pytest.raises(MarketBookBatchPlanError, match="no identifier capacity"):
        plan(1, (), bet_ids=tuple(f"bet-{index:03d}" for index in range(250)))


def test_round_trip_is_restart_stable_and_tamper_evident():
    original = plan(
        25,
        ("EX_ALL_OFFERS",),
        order_projection="ALL",
        match_projection="ROLLED_UP_BY_PRICE",
        include_overall_position=False,
        customer_strategy_refs=("beta", "alpha"),
        bet_ids=("bet-2", "bet-1"),
        locale="en",
        provider_scope_id="BETFAIR:APP-READONLY",
    )
    restored = MarketBookReadPlan.from_json(original.to_json())
    assert restored == original
    assert restored.plan_id == original.plan_id
    assert restored.to_json() == original.to_json()

    raw = json.loads(original.to_json())
    raw["evidence"]["batches"][0]["market_ids"] = ["1.injected"]
    with pytest.raises(MarketBookBatchPlanError, match="canonical recomputation"):
        MarketBookReadPlan.from_json(json.dumps(raw))


def test_post_init_tampering_cannot_reuse_stale_positive_weight_authority():
    p = plan(2, ("EX_BEST_OFFERS",))
    object.__setattr__(p, "price_data", ("EX_FAKE",))
    with pytest.raises(MarketBookBatchPlanError, match="unsupported"):
        _ = p.plan_id


def test_plan_never_grants_dispatch_or_execution_authority():
    p = plan(1, ("EX_BEST_OFFERS",))
    assert p.evidence_payload["complete_market_coverage"] is True
    assert p.evidence_payload["provider_dispatch_authorized"] is False
    assert p.evidence_payload["execution_authorized"] is False
