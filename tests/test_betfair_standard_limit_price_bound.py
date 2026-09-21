from __future__ import annotations

from decimal import Decimal
import json

import pytest

from autosport.betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
    BetfairStandardLimitPriceBoundStatus,
    resolve_betfair_standard_limit_price_bound,
)

from test_betfair_supervised_execution import _bound, _profile


def _bound_action():
    bound, _approval, _goal = _bound(_profile())
    action = bound.execution_plan.actions[0]
    return bound, action


def test_standard_back_limit_without_matchme_applicability_fails_closed() -> None:
    bound, action = _bound_action()

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="MatchMe non-applicability",
    ):
        resolve_betfair_standard_limit_price_bound(
            bound=bound,
            action_id=action.action_id,
        )

def test_evidence_is_not_caller_constructible() -> None:
    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="issued only by the canonical resolver",
    ):
        BetfairStandardLimitPriceBoundEvidence()


def test_canonical_private_issuer_also_fails_closed_without_matchme_applicability() -> None:
    import autosport.betfair_standard_limit_price_bound as module

    bound, action = _bound_action()
    instruction = module._canonical_instruction_projection(action)

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="MatchMe non-applicability",
    ):
        module._issue_evidence(
            bound=bound,
            action=action,
            instruction_sha256=module._digest(instruction),
        )


def test_action_identity_is_re_resolved_from_bound_plan() -> None:
    bound, _action = _bound_action()

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="action is not in bound execution plan",
    ):
        resolve_betfair_standard_limit_price_bound(
            bound=bound,
            action_id="attacker-controlled-action",
        )


def test_exact_bound_type_is_required() -> None:
    bound, action = _bound_action()

    class ShadowBound(type(bound)):
        pass

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="exact canonical BoundSupervisedExecutionPlan",
    ):
        resolve_betfair_standard_limit_price_bound(
            bound=ShadowBound(
                execution_plan=bound.execution_plan,
                portfolio_plan_sha256=bound.portfolio_plan_sha256,
                economic_goal_contract_sha256=bound.economic_goal_contract_sha256,
                intent_id=bound.intent_id,
                intent_sha256=bound.intent_sha256,
                approval_fingerprint=bound.approval_fingerprint,
                profile_bindings=bound.profile_bindings,
                constraints=bound.constraints,
            ),
            action_id=action.action_id,
        )


def test_public_provider_client_rebinding_cannot_mint_a_different_contract(monkeypatch) -> None:
    import autosport.betfair_standard_limit_price_bound as module

    bound, action = _bound_action()

    class ShadowClient:
        pass

    monkeypatch.setattr(
        module,
        "BetfairSupervisedPlaceOrdersClient",
        ShadowClient,
        raising=False,
    )

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="canonical Betfair placeOrders request could not be captured",
    ):
        resolve_betfair_standard_limit_price_bound(
            bound=bound,
            action_id=action.action_id,
        )


def test_instruction_projection_is_captured_from_real_place_action_request() -> None:
    import autosport.betfair_standard_limit_price_bound as module

    bound, action = _bound_action()
    projection = module._canonical_instruction_projection(action)

    assert projection == {
        "selectionId": int(action.selection_id),
        "handicap": 0,
        "side": "BACK",
        "orderType": "LIMIT",
        "limitOrder": {
            "size": str(action.requested_stake),
            "price": str(action.requested_odds),
            "persistenceType": "LAPSE",
        },
    }
    assert len(module._digest(projection)) == 64
    assert bound.execution_plan.created_at < action.expires_at


def _replace_captured_request(
    monkeypatch,
    mutate,
) -> None:
    import autosport.betfair_standard_limit_price_bound as module

    original = module._CANONICAL_PLACE_ACTION

    def drifted(self, action, **kwargs):
        try:
            return original(self, action, **kwargs)
        except module._CapturedPlaceOrdersRequest as captured:
            envelope = json.loads(captured.body.decode("utf-8"))
            mutate(envelope["params"]["instructions"][0])
            raise module._CapturedPlaceOrdersRequest(
                json.dumps(
                    envelope,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ) from None

    monkeypatch.setattr(module, "_CANONICAL_PLACE_ACTION", drifted)


def test_same_version_time_in_force_write_drift_fails_closed(monkeypatch) -> None:
    bound, action = _bound_action()

    def add_fill_or_kill(instruction) -> None:
        instruction["timeInForce"] = "FILL_OR_KILL"

    _replace_captured_request(monkeypatch, add_fill_or_kill)

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="nonstandard Betfair order transformation",
    ):
        resolve_betfair_standard_limit_price_bound(
            bound=bound,
            action_id=action.action_id,
        )


def test_same_version_price_write_drift_fails_closed(monkeypatch) -> None:
    bound, action = _bound_action()

    def worsen_submitted_limit(instruction) -> None:
        instruction["limitOrder"]["price"] = "1.99"

    _replace_captured_request(monkeypatch, worsen_submitted_limit)

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="does not preserve the bound standard LIMIT",
    ):
        resolve_betfair_standard_limit_price_bound(
            bound=bound,
            action_id=action.action_id,
        )


def test_same_version_smart_order_write_drift_fails_closed(monkeypatch) -> None:
    bound, action = _bound_action()

    def add_bet_target(instruction) -> None:
        instruction["betTargetType"] = "PAYOUT"

    _replace_captured_request(monkeypatch, add_bet_target)

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="nonstandard Betfair order transformation",
    ):
        resolve_betfair_standard_limit_price_bound(
            bound=bound,
            action_id=action.action_id,
        )


def test_bound_quote_is_unexpired_but_zero_slippage_stays_unissued() -> None:
    bound, action = _bound_action()

    assert action.quote_observed_at < bound.execution_plan.created_at < action.expires_at
    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="MatchMe non-applicability",
    ):
        resolve_betfair_standard_limit_price_bound(
            bound=bound,
            action_id=action.action_id,
        )

