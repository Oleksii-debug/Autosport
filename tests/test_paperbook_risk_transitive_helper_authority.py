from __future__ import annotations

from decimal import Decimal
from types import FunctionType

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


def _reconstruct_pre_wrapper_evaluate() -> FunctionType:
    """Recover the already-published raw evaluate spec used by the active wrappers."""

    public_root = vars(PaperRiskPolicy)["evaluate"]
    assert type(public_root) is FunctionType
    outer_spec = public_root.__globals__["_EVALUATE_SPEC"]
    assert type(outer_spec) is tuple and len(outer_spec) == 6
    outer_globals = dict(outer_spec[5])
    nested_spec = outer_globals["_EVALUATE_SPEC"]
    assert type(nested_spec) is tuple and len(nested_spec) == 6

    code, name, defaults, kwdefaults, closure, global_bindings = nested_spec
    assert type(global_bindings) is tuple
    reconstructed = FunctionType(
        code,
        dict(global_bindings),
        name=name,
        argdefs=defaults,
        closure=closure,
    )
    if kwdefaults is not None:
        reconstructed.__kwdefaults__ = dict(kwdefaults)
    return reconstructed


def _bounded_policy() -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.10"),
        max_committed_fraction=Decimal("0.10"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )


def _permissive_fraction_limits(self) -> tuple[Decimal, Decimal]:
    del self
    return Decimal("1"), Decimal("1")


def _concentration_policy_and_context() -> tuple[
    PaperRiskPolicy,
    PaperBook,
    ProposedTicketRiskContext,
]:
    goal = EconomicGoalContract(
        goal_id="goal",
        revision=1,
        bankroll_id="bankroll",
        currency="USD",
        max_stake_fraction=Decimal("1"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_event_concentration_fraction=Decimal("0.5"),
        max_turnover_fraction=Decimal("10"),
        max_risk_of_ruin=Decimal("1"),
        max_execution_slippage_fraction=Decimal("1"),
        max_quote_age_seconds=Decimal("60"),
        max_concurrent_positions=10,
    )
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )
    observed_at = "2026-09-29T00:00:00+00:00"
    leg = TicketLeg("event", "market", "selection", Decimal("2"))
    quote = MarketEvent(
        event_id="event",
        market_id="market",
        selection_id="selection",
        decimal_odds=Decimal("2"),
        observed_ts=observed_at,
        source_id="provider",
        sequence=1,
        ingest_ts=observed_at,
    )
    context = ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(quote,),
        provider_accounts=(("provider", "account"),),
        bankroll_id="bankroll",
        currency="USD",
        proposal_ts=observed_at,
    )
    return policy, PaperBook("100"), context


def test_reconstructed_evaluate_rejects_transitive_fraction_limit_descriptor_retarget() -> None:
    """First-level _derived_risk_values witness must cover its class dispatch graph."""

    reconstructed = _reconstruct_pre_wrapper_evaluate()
    policy = _bounded_policy()
    book = PaperBook("100")
    amount = Decimal("50")

    assert reconstructed(policy, book, amount).allowed is False

    original = vars(PaperRiskPolicy)["_effective_fraction_limits"]
    assert type(original) is FunctionType
    type.__setattr__(PaperRiskPolicy, "_effective_fraction_limits", _permissive_fraction_limits)
    try:
        decision = reconstructed(policy, book, amount)
        assert decision.allowed is False, (
            "reconstructed evaluate accepted a stake after transitive risk-helper "
            "descriptor retargeting"
        )
    finally:
        type.__setattr__(PaperRiskPolicy, "_effective_fraction_limits", original)


def test_reconstructed_evaluate_rejects_transitive_fraction_limit_code_retarget() -> None:
    """In-place helper code mutation must not preserve reconstructed evaluate authority."""

    reconstructed = _reconstruct_pre_wrapper_evaluate()
    policy = _bounded_policy()
    book = PaperBook("100")
    amount = Decimal("50")

    assert reconstructed(policy, book, amount).allowed is False

    helper = vars(PaperRiskPolicy)["_effective_fraction_limits"]
    assert type(helper) is FunctionType
    original_code = helper.__code__
    helper.__code__ = _permissive_fraction_limits.__code__
    try:
        decision = reconstructed(policy, book, amount)
        assert decision.allowed is False, (
            "reconstructed evaluate accepted a stake after transitive risk-helper "
            "in-place code retargeting"
        )
    finally:
        helper.__code__ = original_code


def test_reconstructed_evaluate_rejects_transitive_concentration_helper_before_dispatch() -> None:
    """First-level proposal witness must cover its nested concentration authority."""

    reconstructed = _reconstruct_pre_wrapper_evaluate()
    policy, book, context = _concentration_policy_and_context()
    amount = Decimal("10")

    baseline = reconstructed(policy, book, amount, context=context)
    assert baseline.allowed is False
    assert baseline.reason == "owner event concentration limit exceeded"

    descriptor = vars(PaperRiskPolicy)["_identity_concentration_decision"]
    assert type(descriptor) is classmethod
    hostile_calls = 0

    def hostile_identity_concentration_decision(
        cls,
        book,
        amount,
        context,
        *,
        dimension,
        limit,
    ):
        nonlocal hostile_calls
        del cls, book, amount, context, dimension, limit
        hostile_calls += 1
        return None

    type.__setattr__(
        PaperRiskPolicy,
        "_identity_concentration_decision",
        classmethod(hostile_identity_concentration_decision),
    )
    try:
        decision = reconstructed(policy, book, amount, context=context)
        assert decision.allowed is False
        assert hostile_calls == 0, (
            "reconstructed evaluate dispatched a retargeted transitive "
            "concentration helper"
        )
    finally:
        type.__setattr__(
            PaperRiskPolicy,
            "_identity_concentration_decision",
            descriptor,
        )
