from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.campaign_provider_scope_authority as scope

from autosport.betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_routing import VenueQuote
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.opportunity import (
    Opportunity,
    OpportunityDecision,
    QuoteRef,
    StrategyClass,
)
from autosport.paper import PaperBook
from autosport.portfolio_plan import (
    OpportunityEvidence,
    OpportunityIntent,
    PortfolioDependencyGraph,
    build_portfolio_plan,
    persist_portfolio_plan_decision,
)
from autosport.real_execution_ledger import ExecutionAction
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext
from autosport.supervised_execution import (
    ExecutionLegConstraint,
    SupervisedApproval,
    build_supervised_execution_plan,
    supervised_execution_terms_sha256,
)
from autosport.campaign_provider_scope_authority import (
    CampaignProviderScopeError,
    CampaignProviderScopeProjection,
    VerifiedBetfairProviderScopeCapture,
    assert_campaign_provider_scope_authoritative,
    assert_provider_scope_capture_authoritative,
    capture_betfair_provider_scope,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _capture(**overrides: object) -> VerifiedBetfairProviderScopeCapture:
    values: dict[str, object] = {
        "venue_id": "betfair",
        "client_account_scope": "client-scope",
        "authenticated_account_id": "betfair-account-evidence:" + SHA_A,
        "account_details_sha256": SHA_A,
        "adapter_id": ADAPTER_ID,
        "adapter_version": ADAPTER_VERSION,
        "action_id": "action-1",
        "event_id": "event-1",
        "market_id": "1.23456789",
        "provider_order_ref": "bet-1",
        "provider_evidence_id": "provider-evidence-1",
        "provider_source_sha256": SHA_B,
        "request_scope_sha256": SHA_C,
        "readback_evidence_sha256": SHA_D,
        "quote_observed_at": "2026-09-20T12:00:00Z",
        "account_observed_at": "2026-09-20T12:00:01Z",
        "readback_observed_at": "2026-09-20T12:00:02Z",
        "source_interval_start": "2026-09-20T12:00:00Z",
        "source_interval_end": "2026-09-20T12:00:02Z",
        "available_at": "2026-09-20T12:00:02Z",
    }
    values.update(overrides)
    return VerifiedBetfairProviderScopeCapture(**values)  # type: ignore[arg-type]


def _projection(**overrides: object) -> CampaignProviderScopeProjection:
    values: dict[str, object] = {
        "campaign_id": "campaign-1",
        "campaign_version": 1,
        "campaign_sha256": SHA_A,
        "session_id": "session-1",
        "run_id": "run-1",
        "session_evidence_id": "session-evidence-1",
        "session_evidence_sha256": SHA_B,
        "run_summary_sha256": SHA_C,
        "decision_id": "decision-1",
        "plan_id": "plan-1",
        "plan_fingerprint": SHA_D,
        "action_id": "action-1",
        "provider_capture_sha256": SHA_A,
        "provider_evidence_id": "provider-evidence-1",
        "provider_source_sha256": SHA_B,
        "venue_id": "betfair",
        "authenticated_account_id": "betfair-account-evidence:" + SHA_C,
        "event_id": "event-1",
        "market_id": "1.23456789",
        "source_interval_start": "2026-09-20T12:00:00Z",
        "source_interval_end": "2026-09-20T12:00:02Z",
        "observed_at": "2026-09-20T12:00:02Z",
        "available_at": "2026-09-20T12:00:02Z",
    }
    values.update(overrides)
    return CampaignProviderScopeProjection(**values)  # type: ignore[arg-type]


def test_authenticated_account_identity_must_be_source_derived() -> None:
    with pytest.raises(
        CampaignProviderScopeError,
        match="authenticated account identity is not source-derived",
    ):
        _capture(authenticated_account_id="caller-account-label")


def test_source_interval_is_mechanically_derived() -> None:
    with pytest.raises(
        CampaignProviderScopeError,
        match="provider scope start is not mechanically derived",
    ):
        _capture(source_interval_start="2026-09-20T11:59:59Z")

    with pytest.raises(
        CampaignProviderScopeError,
        match="provider scope end is not mechanically derived",
    ):
        _capture(source_interval_end="2026-09-20T12:00:03Z")

    with pytest.raises(
        CampaignProviderScopeError,
        match="provider scope availability must equal latest source observation",
    ):
        _capture(available_at="2026-09-20T12:00:03Z")


def test_caller_constructed_capture_cannot_mint_positive_authority() -> None:
    forged = _capture()

    with pytest.raises(
        CampaignProviderScopeError,
        match="stable authenticated Betfair account identity is unavailable",
    ):
        assert_provider_scope_capture_authoritative(forged)


def test_capture_identity_binds_authenticated_account_and_external_market() -> None:
    base = _capture()
    changed_account = _capture(
        account_details_sha256=SHA_D,
        authenticated_account_id="betfair-account-evidence:" + SHA_D,
    )
    changed_market = _capture(market_id="1.99999999")
    changed_action = _capture(action_id="action-2")

    assert base.capture_sha256 != changed_account.capture_sha256
    assert base.capture_sha256 != changed_market.capture_sha256
    assert base.capture_sha256 != changed_action.capture_sha256


def test_frozen_capture_copy_is_not_source_authoritative() -> None:
    forged = _capture()
    copied = replace(forged, market_id="1.99999999")

    with pytest.raises(
        CampaignProviderScopeError,
        match="stable authenticated Betfair account identity is unavailable",
    ):
        assert_provider_scope_capture_authoritative(copied)


class _ForbiddenInjectedTransport:
    def __init__(self) -> None:
        self.called = False

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.called = True
        raise AssertionError(
            "fail-closed account identity gate must run before injected transport"
        )


def test_injected_betfair_client_cannot_mint_provider_scope() -> None:
    transport = _ForbiddenInjectedTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=lambda: "2026-09-20T12:00:00Z",
    )
    profile = BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="client-scope",
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BET_READBACK,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at="2026-09-20T11:59:00Z",
        source_ref="test-provider-capability",
        source_payload_sha256=SHA_A,
    )
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="client-scope",
        event_id="event-1",
        market_id="1.23456789",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("1.00"),
        quote_id="quote-1",
        quote_observed_at="2026-09-20T11:59:30Z",
        expires_at="2026-09-20T12:01:00Z",
    )

    with pytest.raises(
        CampaignProviderScopeError,
        match="stable authenticated Betfair account identity is unavailable",
    ):
        capture_betfair_provider_scope(
            client,
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
        )

    assert transport.called is False


def test_projection_digest_binds_campaign_and_provider_scope() -> None:
    base = _projection()

    assert base.applicability_digest != _projection(
        campaign_id="campaign-2"
    ).applicability_digest
    assert base.applicability_digest != _projection(
        authenticated_account_id="betfair-account-evidence:" + SHA_D
    ).applicability_digest
    assert base.applicability_digest != _projection(
        market_id="1.99999999"
    ).applicability_digest
    assert base.applicability_digest != _projection(
        event_id="event-2"
    ).applicability_digest


def test_projection_provider_key_accepts_only_exact_source_scope() -> None:
    projection = _projection()
    projection.assert_provider_key(
        venue_id="betfair",
        authenticated_account_id="betfair-account-evidence:" + SHA_C,
        market_id="1.23456789",
        event_id="event-1",
    )

    for kwargs in (
        {
            "venue_id": "other",
            "authenticated_account_id": "betfair-account-evidence:" + SHA_C,
            "market_id": "1.23456789",
            "event_id": "event-1",
        },
        {
            "venue_id": "betfair",
            "authenticated_account_id": "betfair-account-evidence:" + SHA_D,
            "market_id": "1.23456789",
            "event_id": "event-1",
        },
        {
            "venue_id": "betfair",
            "authenticated_account_id": "betfair-account-evidence:" + SHA_C,
            "market_id": "1.99999999",
            "event_id": "event-1",
        },
        {
            "venue_id": "betfair",
            "authenticated_account_id": "betfair-account-evidence:" + SHA_C,
            "market_id": "1.23456789",
            "event_id": "event-2",
        },
    ):
        with pytest.raises(
            CampaignProviderScopeError,
            match="is not applicable to campaign",
        ):
            projection.assert_provider_key(**kwargs)


DECISION_TS = "2026-09-18T13:20:00+00:00"
APPROVED_AT = "2026-09-18T13:20:01+00:00"
CREATED_AT = "2026-09-18T13:20:02+00:00"
QUOTE_EXPIRES_AT = "2026-09-18T13:21:00+00:00"
APPROVAL_EXPIRES_AT = "2026-09-18T13:25:00+00:00"


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-provider-scope-test",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("0.10"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1000"),
        max_risk_of_ruin=Decimal("1"),
        max_execution_slippage_fraction=Decimal("0.05"),
        max_quote_age_seconds=Decimal("3600"),
        max_concurrent_positions=10,
    )


def _canonical_bound_fixture():
    goal = _goal()
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )
    leg = TicketLeg(
        "event-1",
        "1.23456789",
        "42",
        Decimal("2.00"),
        sport="soccer",
    )
    quote_event = MarketEvent(
        event_id=leg.event_id,
        market_id=leg.market_id,
        selection_id=leg.selection_id,
        decimal_odds=leg.locked_odds,
        observed_ts="2026-09-18T13:19:59+00:00",
        source_id="betfair",
        sequence=1,
        source_ts="2026-09-18T13:19:59+00:00",
        ingest_ts="2026-09-18T13:19:59+00:00",
        sport="soccer",
    )
    context = ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(quote_event,),
        provider_accounts=(("betfair", "acct-1"),),
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
        proposal_ts=DECISION_TS,
    )
    quote = QuoteRef.from_market_event(
        quote_event,
        market_snapshot_hash="9" * 64,
    )
    opportunity = Opportunity(
        strategy_class=StrategyClass.LIVE_PRICE_MOVEMENT,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(quote,),
        claims_probability_edge=False,
        forecasts=(),
    )
    intent = OpportunityIntent(
        intent_id="intent-live-1",
        opportunity=opportunity,
        evidence=OpportunityEvidence(
            evidence_id="evidence-live-1",
            observed_at="2026-09-18T13:19:59+00:00",
            causal_cutoff="2026-09-18T13:19:58+00:00",
            reproducibility_sha256="8" * 64,
        ),
        risk_context=context,
        signal_strength=Decimal("0.05"),
        strategy_id="strategy-live-1",
        config_sha256="7" * 64,
    )
    book = PaperBook("1000")
    graph = PortfolioDependencyGraph.for_inputs(book, (intent,))
    portfolio = build_portfolio_plan(
        book,
        (intent,),
        policy,
        DECISION_TS,
        dependency_graph=graph,
    )
    assert portfolio.stakes[0] > 0

    venue = VenueQuote(
        "betfair",
        "acct-1",
        intent.opportunity.quotes[0],
        Decimal("1000"),
    )
    route = plan_equal_split_residual(
        portfolio.stakes[0],
        (venue,),
        routing_request_id="route-provider-scope-test",
        parent_plan_id=portfolio.plan_sha256,
        stake_quantum=Decimal("0.01"),
    )
    constraint = ExecutionLegConstraint(
        leg_id=route.legs[0].leg_id,
        side="BACK",
        quote_expires_at=QUOTE_EXPIRES_AT,
        max_slippage_fraction=Decimal("0.05"),
    )
    approval = SupervisedApproval(
        approval_id="approval-provider-scope-test",
        portfolio_plan_sha256=portfolio.plan_sha256,
        intent_id=intent.intent_id,
        routing_request_id=route.routing_request_id,
        execution_terms_sha256=supervised_execution_terms_sha256(
            route,
            (constraint,),
        ),
        approved_at=APPROVED_AT,
        expires_at=APPROVAL_EXPIRES_AT,
        evidence_sha256="6" * 64,
    )
    profile = BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BET_READBACK,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=DECISION_TS,
        source_ref="betfair://profile/provider-scope-test",
        source_payload_sha256="5" * 64,
    )
    bound = build_supervised_execution_plan(
        portfolio,
        (intent,),
        route,
        (profile,),
        approval,
        (constraint,),
        created_at=CREATED_AT,
    )
    return bound, portfolio, intent, policy


def _frozen_portfolio_record(
    tmp_path: Path,
    portfolio,
    intent,
    policy,
):
    ledger = JsonlDecisionLedger(tmp_path / "decision-ledger.jsonl")
    return persist_portfolio_plan_decision(
        ledger,
        portfolio,
        (intent,),
        policy,
        initialize_ledger=True,
        replay_run_id="run-1",
        material_action_id="deliberately-different-material-action",
    )


def test_campaign_projection_constructor_cannot_mint_authority() -> None:
    forged = _projection()
    with pytest.raises(
        CampaignProviderScopeError,
        match="was not issued by canonical resolver",
    ):
        assert_campaign_provider_scope_authoritative(forged)


def test_frozen_portfolio_join_ignores_unrelated_material_action_label(
    tmp_path: Path,
) -> None:
    bound, portfolio, intent, policy = _canonical_bound_fixture()
    record = _frozen_portfolio_record(
        tmp_path,
        portfolio,
        intent,
        policy,
    )

    decision, restored = scope._portfolio_execution_membership(
        [record],
        bound.execution_plan,
    )

    assert decision.decision_id == record.decision_id
    assert restored.plan_sha256 == portfolio.plan_sha256
    assert (
        bound.execution_plan.decision_id
        == f"portfolio:{portfolio.plan_sha256}:intent:{intent.intent_sha256}"
    )
    assert (
        record.payload["material_action_id"]
        == "deliberately-different-material-action"
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        (
            "market_id",
            "1.99999999",
            "exact frozen Opportunity quote membership",
        ),
        (
            "account_id",
            "acct-other",
            "provider account is outside frozen intent",
        ),
    ),
)
def test_frozen_portfolio_join_rejects_market_or_account_swap(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    bound, portfolio, intent, policy = _canonical_bound_fixture()
    record = _frozen_portfolio_record(
        tmp_path,
        portfolio,
        intent,
        policy,
    )
    action = bound.execution_plan.actions[0]
    changed_action = replace(action, **{field: value})
    changed_plan = replace(
        bound.execution_plan,
        actions=(changed_action,),
    )

    with pytest.raises(CampaignProviderScopeError, match=message):
        scope._portfolio_execution_membership([record], changed_plan)


def test_frozen_portfolio_join_rejects_aggregate_stake_overflow(
    tmp_path: Path,
) -> None:
    bound, portfolio, intent, policy = _canonical_bound_fixture()
    record = _frozen_portfolio_record(
        tmp_path,
        portfolio,
        intent,
        policy,
    )
    action = bound.execution_plan.actions[0]
    overflow = replace(
        action,
        requested_stake=portfolio.stakes[0] + Decimal("0.01"),
    )
    changed_plan = replace(bound.execution_plan, actions=(overflow,))

    with pytest.raises(
        CampaignProviderScopeError,
        match="action vector exceeds frozen PortfolioPlan stake",
    ):
        scope._portfolio_execution_membership([record], changed_plan)


def test_frozen_portfolio_join_rejects_forged_decision_identity(
    tmp_path: Path,
) -> None:
    bound, portfolio, intent, policy = _canonical_bound_fixture()
    record = _frozen_portfolio_record(
        tmp_path,
        portfolio,
        intent,
        policy,
    )
    changed_plan = replace(
        bound.execution_plan,
        decision_id=(
            f"portfolio:{portfolio.plan_sha256}:intent:"
            + "f" * 64
        ),
    )

    with pytest.raises(
        CampaignProviderScopeError,
        match="execution intent is not unique frozen PortfolioPlan membership",
    ):
        scope._portfolio_execution_membership([record], changed_plan)
