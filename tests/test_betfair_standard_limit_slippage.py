from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.betfair_standard_limit_slippage import (
    BetfairStandardLimitSlippageError,
    ProspectiveSlippageUnknownReason,
    build_betfair_standard_back_limit_instruction,
    prove_betfair_standard_back_limit_zero_slippage,
)
from autosport.prospective_applicable_cost import ApplicableCostKnowledge
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan
from autosport.supervised_execution import (
    BoundSupervisedExecutionPlan,
    ExecutionLegConstraint,
    ProfileBinding,
    _bound_binding_sha256,
)

OBSERVED_AT = "2026-09-21T03:00:00+00:00"
CREATED_AT = "2026-09-21T03:00:01+00:00"
EXPIRES_AT = "2026-09-21T03:01:00+00:00"
PROVIDER_REF = "provider-order-ref-735"


def _action(**changes: object) -> ExecutionAction:
    action = ExecutionAction(
        action_id="leg-735",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.23456789",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.10"),
        requested_stake=Decimal("5.00"),
        quote_id="quote-735",
        quote_observed_at=OBSERVED_AT,
        expires_at=EXPIRES_AT,
    )
    return replace(action, **changes) if changes else action


def _bound(*, created_at: str = CREATED_AT) -> BoundSupervisedExecutionPlan:
    action = _action()
    placeholder = ExecutionPlan(
        plan_id="placeholder",
        bookmaker_profile_version="profile-bundle-1",
        decision_id="decision-735",
        approval_id="approval-735",
        created_at=created_at,
        actions=(action,),
    )
    profile_bindings = (
        ProfileBinding(
            venue_id="betfair",
            account_id="acct-1",
            adapter_id="betfair-exchange-jsonrpc-readonly",
            adapter_version="1",
            profile_version=1,
            profile_sha256="d" * 64,
        ),
    )
    constraints = (
        ExecutionLegConstraint(
            leg_id=action.action_id,
            side="BACK",
            quote_expires_at=EXPIRES_AT,
            max_slippage_fraction=Decimal("0.05"),
        ),
    )
    binding_sha = _bound_binding_sha256(
        placeholder,
        "a" * 64,
        "b" * 64,
        "intent-735",
        "c" * 64,
        "e" * 64,
        profile_bindings,
        constraints,
    )
    execution_plan = replace(
        placeholder,
        plan_id=f"supervised-v2-{binding_sha}",
    )
    return BoundSupervisedExecutionPlan(
        execution_plan=execution_plan,
        portfolio_plan_sha256="a" * 64,
        economic_goal_contract_sha256="b" * 64,
        intent_id="intent-735",
        intent_sha256="c" * 64,
        approval_fingerprint="e" * 64,
        profile_bindings=profile_bindings,
        constraints=constraints,
    )


def test_projection_is_exact_plain_back_limit_placeorders_instruction() -> None:
    instruction = build_betfair_standard_back_limit_instruction(
        _action(),
        provider_order_ref=PROVIDER_REF,
    )

    assert instruction == {
        "selectionId": 42,
        "handicap": 0,
        "side": "BACK",
        "orderType": "LIMIT",
        "limitOrder": {
            "size": "5.00",
            "price": "2.10",
            "persistenceType": "LAPSE",
        },
        "customerOrderRef": PROVIDER_REF,
    }


def test_plain_back_limit_yields_sealed_known_zero_price_slippage() -> None:
    bound = _bound()
    instruction = build_betfair_standard_back_limit_instruction(
        bound.execution_plan.actions[0],
        provider_order_ref=PROVIDER_REF,
    )

    assessment = prove_betfair_standard_back_limit_zero_slippage(
        bound,
        action_id="leg-735",
        provider_order_ref=PROVIDER_REF,
        instruction=instruction,
    )

    assert assessment.knowledge is ApplicableCostKnowledge.KNOWN_ZERO
    assert assessment.amount == Decimal("0")
    assert assessment.complete is True
    assert assessment.unknown_reason is None
    assert assessment.evidence is not None
    assert assessment.evidence.portfolio_plan_sha256 == "a" * 64
    assert assessment.evidence.intent_id == "intent-735"
    assert assessment.evidence.action_id == "leg-735"
    assert assessment.evidence.quote_id == "quote-735"
    assert assessment.evidence.decision_quote == "2.10"
    assert assessment.evidence.decision_at == CREATED_AT
    assert assessment.evidence.knowledge is ApplicableCostKnowledge.KNOWN_ZERO
    assert assessment.evidence.evidence_sha256 == assessment.evidence.to_dict()[
        "evidence_sha256"
    ]


def test_fok_or_any_extra_instruction_semantic_fails_closed() -> None:
    bound = _bound()
    instruction = build_betfair_standard_back_limit_instruction(
        bound.execution_plan.actions[0],
        provider_order_ref=PROVIDER_REF,
    )
    instruction["limitOrder"] = {
        **instruction["limitOrder"],
        "timeInForce": "FILL_OR_KILL",
    }

    assessment = prove_betfair_standard_back_limit_zero_slippage(
        bound,
        action_id="leg-735",
        provider_order_ref=PROVIDER_REF,
        instruction=instruction,
    )

    assert assessment.knowledge is ApplicableCostKnowledge.UNKNOWN_UNPROVEN
    assert assessment.amount is None
    assert assessment.complete is False
    assert (
        assessment.unknown_reason
        is ProspectiveSlippageUnknownReason.NONSTANDARD_LIMIT_INSTRUCTION
    )


def test_provider_identity_mismatch_fails_closed() -> None:
    bound = _bound()
    instruction = build_betfair_standard_back_limit_instruction(
        bound.execution_plan.actions[0],
        provider_order_ref=PROVIDER_REF,
    )

    assessment = prove_betfair_standard_back_limit_zero_slippage(
        bound,
        action_id="leg-735",
        provider_order_ref=PROVIDER_REF,
        instruction=instruction,
        provider_adapter_version="future-version",
    )

    assert assessment.knowledge is ApplicableCostKnowledge.UNKNOWN_UNPROVEN
    assert (
        assessment.unknown_reason
        is ProspectiveSlippageUnknownReason.PROVIDER_IDENTITY_MISMATCH
    )


def test_quote_not_active_at_decision_fails_closed() -> None:
    bound = _bound(created_at=EXPIRES_AT)
    instruction = build_betfair_standard_back_limit_instruction(
        bound.execution_plan.actions[0],
        provider_order_ref=PROVIDER_REF,
    )

    assessment = prove_betfair_standard_back_limit_zero_slippage(
        bound,
        action_id="leg-735",
        provider_order_ref=PROVIDER_REF,
        instruction=instruction,
    )

    assert assessment.knowledge is ApplicableCostKnowledge.UNKNOWN_UNPROVEN
    assert (
        assessment.unknown_reason
        is ProspectiveSlippageUnknownReason.QUOTE_NOT_ACTIVE_AT_DECISION
    )


def test_lay_projection_is_not_eligible_for_zero_authority() -> None:
    with pytest.raises(BetfairStandardLimitSlippageError):
        build_betfair_standard_back_limit_instruction(
            _action(side="LAY"),
            provider_order_ref=PROVIDER_REF,
        )
