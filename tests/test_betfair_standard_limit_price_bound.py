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
from autosport.real_execution_ledger import ExecutionAction

from test_betfair_supervised_execution import _bound, _profile


def _evidence():
    bound, _approval, _goal = _bound(_profile())
    action = bound.execution_plan.actions[0]
    evidence = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action.action_id,
    )
    return bound, action, evidence


def test_standard_back_limit_stays_unproven_while_matchme_applicability_is_unknown() -> None:
    bound, action, evidence = _evidence()

    assert evidence.execution_plan_id == bound.execution_plan.plan_id
    assert evidence.execution_plan_sha256 == bound.execution_plan.fingerprint
    assert evidence.intent_id == bound.intent_id
    assert evidence.intent_sha256 == bound.intent_sha256
    assert evidence.action_id == action.action_id
    assert evidence.bookmaker_id == "betfair"
    assert evidence.side == "BACK"
    assert evidence.price_floor_odds == Decimal("2.00")
    assert evidence.requested_stake == action.requested_stake
    assert evidence.status is BetfairStandardLimitPriceBoundStatus.UNKNOWN_MATCHME_APPLICABILITY
    assert evidence.matchme_applicability_proven is False
    assert evidence.zero_adverse_price_deterioration is False
    assert evidence.execution_feasibility_proven is False
    assert evidence.realized_price_exact is False

    payload = evidence.to_dict()
    assert payload["price_floor_odds"] == ExecutionAction.to_dict(action)["requested_odds"]
    assert payload["requested_stake"] == ExecutionAction.to_dict(action)["requested_stake"]
    assert payload["matchme_applicability_proven"] is False
    assert payload["zero_adverse_price_deterioration"] is False
    assert payload["execution_feasibility_proven"] is False
    assert payload["realized_price_exact"] is False
    assert len(payload["instruction_sha256"]) == 64
    assert payload["evidence_id"] == evidence.evidence_id


def test_evidence_is_not_caller_constructible() -> None:
    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="issued only by the canonical resolver",
    ):
        BetfairStandardLimitPriceBoundEvidence()


def test_action_identity_is_re_resolved_from_bound_plan() -> None:
    bound, _action, _resolved_evidence = _evidence()

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="action is not in bound execution plan",
    ):
        resolve_betfair_standard_limit_price_bound(
            bound=bound,
            action_id="attacker-controlled-action",
        )


def test_exact_bound_type_is_required() -> None:
    bound, action, _resolved_evidence = _evidence()

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

    bound, action, _resolved_evidence = _evidence()

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

    bound, action, evidence = _evidence()
    projection = module._canonical_instruction_projection(action)

    assert projection == {
        "selectionId": int(action.selection_id),
        "handicap": 0,
        "side": "BACK",
        "orderType": "LIMIT",
        "limitOrder": {
            "size": ExecutionAction.to_dict(action)["requested_stake"],
            "price": ExecutionAction.to_dict(action)["requested_odds"],
            "persistenceType": "LAPSE",
        },
    }
    assert evidence.instruction_sha256 == module._digest(projection)
    assert bound.execution_plan.created_at < action.expires_at


def test_instruction_projection_identity_survives_decimal_scale_round_trip() -> None:
    import autosport.betfair_standard_limit_price_bound as module

    _bound_plan, action, _evidence_record = _evidence()
    durable_action = ExecutionAction.to_dict(action)
    reloaded_action = ExecutionAction(**durable_action)

    before = module._canonical_instruction_projection(action)
    after = module._canonical_instruction_projection(reloaded_action)
    before_evidence = module._issue_evidence(
        bound=_bound_plan,
        action=action,
        instruction_sha256=module._digest(before),
    )
    after_evidence = module._issue_evidence(
        bound=_bound_plan,
        action=reloaded_action,
        instruction_sha256=module._digest(after),
    )

    assert before == after
    assert before["limitOrder"]["price"] == durable_action["requested_odds"]
    assert before["limitOrder"]["size"] == durable_action["requested_stake"]
    assert module._digest(before) == module._digest(after)
    assert before_evidence.to_dict() == after_evidence.to_dict()
    assert before_evidence.evidence_id == after_evidence.evidence_id


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
    bound, action, _resolved_evidence = _evidence()

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
    bound, action, _resolved_evidence = _evidence()

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
    bound, action, _resolved_evidence = _evidence()

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


def test_bound_quote_is_unexpired_at_decision_and_realized_price_is_not_backfilled() -> None:
    bound, action, evidence = _evidence()

    assert action.quote_observed_at < bound.execution_plan.created_at < action.expires_at
    assert evidence.price_floor_odds == action.requested_odds
    assert evidence.status is BetfairStandardLimitPriceBoundStatus.UNKNOWN_MATCHME_APPLICABILITY
    assert evidence.matchme_applicability_proven is False
    assert evidence.zero_adverse_price_deterioration is False
    assert evidence.realized_price_exact is False
    assert "accepted_odds" not in evidence.to_dict()
