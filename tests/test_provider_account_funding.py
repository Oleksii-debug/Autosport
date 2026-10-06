from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from autosport.bookmaker_account_reconciliation import ReconciledAccountState
from autosport.bookmaker_capability import (
    BookmakerBalanceObservation,
    BookmakerCapability,
)
from autosport.execution_scope_admission import (
    ExecutionDataMode,
    ExecutionScopeDescriptor,
    ExecutionScopePreAdmission,
)
from autosport.provider_account_funding import (
    AccountFundingStatus,
    ProviderAccountFundingError,
    assess_provider_account_funding,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan
from autosport.supervised_execution import (
    BoundSupervisedExecutionPlan,
    ProfileBinding,
    _bound_binding_sha256,
)


T_BALANCE = "2026-10-06T10:00:00+00:00"
T_PLAN = "2026-10-06T10:01:00+00:00"
T_CUTOFF = "2026-10-06T10:02:00+00:00"
T_FUTURE = "2026-10-06T10:03:00+00:00"
H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64


def _profile_id(venue: str, account: str) -> str:
    return hashlib.sha256(f"profile:{venue}:{account}".encode()).hexdigest()


def _action(
    action_id: str,
    venue: str,
    account: str,
    stake: str,
    *,
    side: str = "BACK",
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id=venue,
        account_id=account,
        event_id="event-1",
        market_id="market-1",
        selection_id=f"selection-{action_id}",
        side=side,
        requested_odds=Decimal("2.20"),
        requested_stake=Decimal(stake),
        quote_id=f"quote-{action_id}",
        quote_observed_at=T_BALANCE,
        expires_at="2026-10-06T10:10:00+00:00",
    )


def _bound(*actions: ExecutionAction) -> BoundSupervisedExecutionPlan:
    profiles = tuple(
        ProfileBinding(
            venue_id=venue,
            account_id=account,
            adapter_id=f"adapter-{venue}-{account}",
            adapter_version="1",
            profile_version=1,
            profile_sha256=_profile_id(venue, account),
        )
        for venue, account in sorted(
            {(action.bookmaker_id, action.account_id) for action in actions}
        )
    )
    provisional = ExecutionPlan(
        plan_id="pending",
        bookmaker_profile_version="profile-set-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=T_PLAN,
        actions=tuple(actions),
    )
    digest = _bound_binding_sha256(
        provisional,
        H1,
        H2,
        "intent-1",
        H3,
        H4,
        profiles,
        (),
    )
    plan = ExecutionPlan(
        plan_id=f"supervised-v2-{digest}",
        bookmaker_profile_version=provisional.bookmaker_profile_version,
        decision_id=provisional.decision_id,
        approval_id=provisional.approval_id,
        created_at=provisional.created_at,
        actions=provisional.actions,
    )
    return BoundSupervisedExecutionPlan(
        execution_plan=plan,
        portfolio_plan_sha256=H1,
        economic_goal_contract_sha256=H2,
        intent_id="intent-1",
        intent_sha256=H3,
        approval_fingerprint=H4,
        profile_bindings=profiles,
        constraints=(),
    )


def _scope(
    bound: BoundSupervisedExecutionPlan,
    action: ExecutionAction,
    *,
    currency: str = "EUR",
    cutoff: str = T_CUTOFF,
    supervised_plan_id: str | None = None,
) -> ExecutionScopePreAdmission:
    descriptor = ExecutionScopeDescriptor(
        action_id=action.action_id,
        provider_product_domain="TEST_EXCHANGE",
        environment="production",
        jurisdiction="SK",
        sport="soccer",
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        market_semantics_id="soccer:match-odds:v1",
        settlement_rule_id="soccer:match-odds:rules:v1",
        data_mode=ExecutionDataMode.LIVE,
        action_kind="PLACE_BET",
        order_type="LIMIT",
        side=action.side,
        currency=currency,
        strategy_id="strategy-1",
        purpose="supervised_live_execution",
        evidence_cutoff=cutoff,
        attempt_id=f"attempt-{action.action_id}",
    )
    return ExecutionScopePreAdmission(
        scope=descriptor,
        scope_sha256=descriptor.descriptor_sha256,
        profile_id=bound.profile_for(
            action.bookmaker_id,
            action.account_id,
        ).profile_sha256,
        governance_evidence_id="governance-1",
        integration_evidence_id="integration-1",
        supervised_plan_id=supervised_plan_id or bound.execution_plan.plan_id,
        provider_action_documented=True,
        governance_automation_permitted=False,
        integration_channel_bound=True,
        supervised_action_binding_structurally_valid=True,
        blocking_reasons=(),
    )


def _state(
    venue: str,
    account: str,
    amount: str,
    *,
    currency: str = "EUR",
    observed_at: str = T_BALANCE,
    balance_observed_at: str | None = None,
    balance_read: bool = True,
    include_balance: bool = True,
    profile_id: str | None = None,
    adapter_id: str | None = None,
) -> ReconciledAccountState:
    adapter = adapter_id or f"adapter-{venue}-{account}"
    balance = (
        BookmakerBalanceObservation(
            venue_id=venue,
            account_id=account,
            adapter_id=adapter,
            observation_id=f"balance-{venue}-{account}-{amount}",
            currency=currency,
            available_balance=Decimal(amount),
            observed_at=balance_observed_at or observed_at,
            source_payload_sha256="a" * 64,
        )
        if include_balance
        else None
    )
    return ReconciledAccountState(
        snapshot_id=f"snapshot-{venue}-{account}-{observed_at}",
        venue_id=venue,
        account_id=account,
        adapter_id=adapter,
        profile_id=profile_id or _profile_id(venue, account),
        observed_at=observed_at,
        observed_capabilities=(
            frozenset({BookmakerCapability.BALANCE_READ})
            if balance_read
            else frozenset()
        ),
        positions=(),
        latest_balance_observation=balance,
        unexplained_balance_delta=None,
    )


def _assess(
    bound: BoundSupervisedExecutionPlan,
    states: tuple[ReconciledAccountState, ...],
    *,
    currencies: dict[str, str] | None = None,
):
    currencies = currencies or {}
    scopes = tuple(
        _scope(
            bound,
            action,
            currency=currencies.get(action.action_id, "EUR"),
        )
        for action in bound.execution_plan.actions
    )
    return assess_provider_account_funding(
        bound_plan=bound,
        scope_assessments=scopes,
        account_states=states,
    )


def test_global_nominal_sum_cannot_rescue_locally_underfunded_account() -> None:
    a = _action("a", "provider-a", "acct-a", "500")
    b = _action("b", "provider-b", "acct-b", "500")
    bound = _bound(a, b)

    result = _assess(
        bound,
        (
            _state("provider-a", "acct-a", "1000"),
            _state("provider-b", "acct-b", "0"),
        ),
    )

    assert result.local_balance_sufficient is False
    by_account = {(row.venue_id, row.account_id): row for row in result.rows}
    assert by_account[("provider-a", "acct-a")].status is AccountFundingStatus.FUNDED_AT_REQUIRED_ACCOUNT
    assert by_account[("provider-b", "acct-b")].status is AccountFundingStatus.UNDERFUNDED_AT_REQUIRED_ACCOUNT


def test_exact_local_balances_can_prove_sufficiency_but_never_execution_authority() -> None:
    a = _action("a", "provider-a", "acct-a", "500")
    b = _action("b", "provider-b", "acct-b", "500")
    bound = _bound(a, b)

    result = _assess(
        bound,
        (
            _state("provider-a", "acct-a", "500"),
            _state("provider-b", "acct-b", "500"),
        ),
    )

    assert result.local_balance_sufficient is True
    assert result.causal_balance_evidence_complete is True
    assert result.reservation_boundary_proven is False
    assert result.freshness_policy_proven is False
    assert result.execution_funding_proven is False
    assert result.grants_transfer_authority is False
    assert result.grants_execution_authority is False


def test_same_account_actions_are_aggregated_before_funding_decision() -> None:
    first = _action("a", "provider-a", "acct-a", "300")
    second = _action("b", "provider-a", "acct-a", "300")
    bound = _bound(first, second)

    result = _assess(bound, (_state("provider-a", "acct-a", "500"),))

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.action_ids == ("a", "b")
    assert row.required_capital == Decimal("600")
    assert row.available_balance == Decimal("500")
    assert row.status is AccountFundingStatus.UNDERFUNDED_AT_REQUIRED_ACCOUNT
    assert result.local_balance_sufficient is False


def test_missing_required_account_is_explicit_and_unrelated_extra_account_does_not_rescue() -> None:
    action = _action("b", "provider-b", "acct-b", "10")
    bound = _bound(action)

    result = _assess(bound, (_state("provider-a", "acct-a", "100000"),))

    assert result.rows[0].status is AccountFundingStatus.ACCOUNT_STATE_MISSING
    assert result.rows[0].available_balance is None
    assert result.local_balance_sufficient is False
    assert result.causal_balance_evidence_complete is False


def test_current_snapshot_without_balance_read_is_partial_not_fresh_money() -> None:
    action = _action("a", "provider-a", "acct-a", "10")
    bound = _bound(action)
    state = _state(
        "provider-a",
        "acct-a",
        "100",
        balance_read=False,
    )

    result = _assess(bound, (state,))

    assert result.rows[0].status is AccountFundingStatus.ACCOUNT_STATE_PARTIAL
    assert result.local_balance_sufficient is False


def test_balance_capability_without_balance_evidence_fails_closed() -> None:
    action = _action("a", "provider-a", "acct-a", "10")
    bound = _bound(action)
    state = _state(
        "provider-a",
        "acct-a",
        "100",
        include_balance=False,
        balance_read=True,
    )

    result = _assess(bound, (state,))

    assert result.rows[0].status is AccountFundingStatus.BALANCE_EVIDENCE_MISSING
    assert result.causal_balance_evidence_complete is False


def test_cross_currency_nominal_amount_cannot_fund_action() -> None:
    action = _action("a", "provider-a", "acct-a", "10")
    bound = _bound(action)

    result = _assess(
        bound,
        (_state("provider-a", "acct-a", "100", currency="GBP"),),
        currencies={"a": "EUR"},
    )

    assert result.rows[0].status is AccountFundingStatus.CURRENCY_MISMATCH
    assert result.local_balance_sufficient is False


def test_one_account_cannot_silently_combine_two_requested_currencies() -> None:
    first = _action("a", "provider-a", "acct-a", "10")
    second = _action("b", "provider-a", "acct-a", "10")
    bound = _bound(first, second)

    result = _assess(
        bound,
        (_state("provider-a", "acct-a", "100", currency="EUR"),),
        currencies={"a": "EUR", "b": "GBP"},
    )

    row = result.rows[0]
    assert row.status is AccountFundingStatus.CURRENCY_CONFLICT
    assert row.requested_currency is None
    assert result.local_balance_sufficient is False


def test_later_account_evidence_cannot_backfill_earlier_causal_cut() -> None:
    action = _action("a", "provider-a", "acct-a", "10")
    bound = _bound(action)

    result = _assess(
        bound,
        (
            _state(
                "provider-a",
                "acct-a",
                "100",
                observed_at=T_FUTURE,
            ),
        ),
    )

    assert result.rows[0].status is AccountFundingStatus.BALANCE_EVIDENCE_FUTURE
    assert result.local_balance_sufficient is False


def test_balance_observation_after_cutoff_is_future_even_if_snapshot_time_is_not() -> None:
    action = _action("a", "provider-a", "acct-a", "10")
    bound = _bound(action)

    result = _assess(
        bound,
        (
            _state(
                "provider-a",
                "acct-a",
                "100",
                observed_at=T_BALANCE,
                balance_observed_at=T_FUTURE,
            ),
        ),
    )

    assert result.rows[0].status is AccountFundingStatus.BALANCE_EVIDENCE_FUTURE


def test_lay_is_not_scalarized_as_back_stake() -> None:
    action = _action(
        "lay",
        "provider-a",
        "acct-a",
        "10",
        side="LAY",
    )
    bound = _bound(action)

    result = _assess(bound, (_state("provider-a", "acct-a", "100"),))

    row = result.rows[0]
    assert row.status is AccountFundingStatus.SIDE_UNSUPPORTED
    assert row.required_capital is None
    assert result.local_balance_sufficient is False


def test_profile_binding_mismatch_is_conflicting_evidence_not_a_soft_shortfall() -> None:
    action = _action("a", "provider-a", "acct-a", "10")
    bound = _bound(action)
    state = _state(
        "provider-a",
        "acct-a",
        "100",
        profile_id="f" * 64,
    )

    with pytest.raises(
        ProviderAccountFundingError,
        match="profile binding",
    ):
        _assess(bound, (state,))


def test_balance_identity_mismatch_is_rejected() -> None:
    action = _action("a", "provider-a", "acct-a", "10")
    bound = _bound(action)
    state = _state(
        "provider-a",
        "acct-a",
        "100",
        adapter_id="wrong-adapter",
        profile_id=_profile_id("provider-a", "acct-a"),
    )

    with pytest.raises(
        ProviderAccountFundingError,
        match="profile binding",
    ):
        _assess(bound, (state,))


def test_duplicate_account_state_cannot_create_double_capital() -> None:
    action = _action("a", "provider-a", "acct-a", "10")
    bound = _bound(action)
    state = _state("provider-a", "acct-a", "100")

    with pytest.raises(
        ProviderAccountFundingError,
        match="duplicated",
    ):
        _assess(bound, (state, state))


def test_scope_vector_must_exactly_cover_plan() -> None:
    first = _action("a", "provider-a", "acct-a", "10")
    second = _action("b", "provider-b", "acct-b", "10")
    bound = _bound(first, second)

    with pytest.raises(
        ProviderAccountFundingError,
        match="exactly cover",
    ):
        assess_provider_account_funding(
            bound_plan=bound,
            scope_assessments=(_scope(bound, first),),
            account_states=(
                _state("provider-a", "acct-a", "100"),
                _state("provider-b", "acct-b", "100"),
            ),
        )


def test_scope_cannot_be_rebound_to_another_supervised_plan_id() -> None:
    action = _action("a", "provider-a", "acct-a", "10")
    bound = _bound(action)

    with pytest.raises(
        ProviderAccountFundingError,
        match="plan identity mismatch",
    ):
        assess_provider_account_funding(
            bound_plan=bound,
            scope_assessments=(
                _scope(bound, action, supervised_plan_id="other-plan"),
            ),
            account_states=(_state("provider-a", "acct-a", "100"),),
        )


def test_result_is_deterministic_across_account_and_scope_input_order() -> None:
    first = _action("a", "provider-a", "acct-a", "10.00")
    second = _action("b", "provider-b", "acct-b", "20.0")
    bound = _bound(first, second)
    a = _state("provider-a", "acct-a", "100.00")
    b = _state("provider-b", "acct-b", "200.0")
    scopes = (_scope(bound, first), _scope(bound, second))

    forward = assess_provider_account_funding(
        bound_plan=bound,
        scope_assessments=scopes,
        account_states=(a, b),
    )
    reverse = assess_provider_account_funding(
        bound_plan=bound,
        scope_assessments=tuple(reversed(scopes)),
        account_states=(b, a),
    )

    assert forward.rows == reverse.rows
    assert forward.assessment_sha256 == reverse.assessment_sha256


def test_pathological_provider_balance_exponent_fails_before_fixed_point_allocation() -> None:
    action = _action("a", "provider-a", "acct-a", "10")
    bound = _bound(action)
    state = _state("provider-a", "acct-a", "1e1000000")

    with pytest.raises(
        ProviderAccountFundingError,
        match="representation exceeds resource limit",
    ):
        _assess(bound, (state,))


def test_unrelated_account_state_does_not_change_exact_plan_projection() -> None:
    action = _action("a", "provider-a", "acct-a", "10")
    bound = _bound(action)
    required = _state("provider-a", "acct-a", "100")
    unrelated = _state("provider-x", "acct-x", "999999")

    minimal = _assess(bound, (required,))
    with_extra = _assess(bound, (unrelated, required))

    assert minimal.rows == with_extra.rows
    assert minimal.assessment_sha256 == with_extra.assessment_sha256
