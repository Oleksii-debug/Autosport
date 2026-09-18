from __future__ import annotations

import tempfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    BookmakerPositionObservation,
    BookmakerPositionState,
)
from autosport.bookmaker_routing import VenueQuote
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.opportunity import Opportunity, OpportunityDecision, QuoteRef, StrategyClass
from autosport.paper import PaperBook
from autosport.portfolio_plan import (
    OpportunityEvidence,
    OpportunityIntent,
    PortfolioDependencyGraph,
    build_portfolio_plan,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    RealExecutionLedger,
)
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext
from autosport.supervised_execution import (
    ApprovalState,
    ExecutionLegConstraint,
    ProviderNotFoundReadback,
    ProviderReadback,
    ReadbackOutcome,
    SupervisedApproval,
    SupervisedExecutionError,
    begin_supervised_attempt,
    build_supervised_execution_plan,
    reconcile_account_snapshot,
    reconcile_provider_not_found,
    reconcile_provider_readback,
    reserve_supervised_plan,
    supervised_execution_terms_sha256,
)


DECISION_TS = "2026-09-18T13:20:00+00:00"
APPROVED_AT = "2026-09-18T13:20:01+00:00"
CREATED_AT = "2026-09-18T13:20:02+00:00"
RESERVED_AT = "2026-09-18T13:20:03+00:00"
SUBMITTED_AT = "2026-09-18T13:20:04+00:00"
UNKNOWN_AT = "2026-09-18T13:20:05+00:00"
READBACK_AT = "2026-09-18T13:20:06+00:00"
QUOTE_EXPIRES_AT = "2026-09-18T13:21:00+00:00"
APPROVAL_EXPIRES_AT = "2026-09-18T13:25:00+00:00"


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-supervised-execution",
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


def _intent() -> tuple[OpportunityIntent, PaperRiskPolicy, PaperBook]:
    goal = _goal()
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )
    leg = TicketLeg(
        "event-1",
        "winner",
        "home",
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
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
        proposal_ts=DECISION_TS,
    )
    quote = QuoteRef.from_market_event(quote_event, market_snapshot_hash="9" * 64)
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
            reproducibility_sha256="a" * 64,
        ),
        risk_context=context,
        signal_strength=Decimal("0.05"),
        strategy_id="strategy-live-1",
        config_sha256="e" * 64,
    )
    return intent, policy, PaperBook("1000")


def _profile(*, observed_at: str = DECISION_TS) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.OPEN_POSITIONS_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
            BookmakerCapabilityFact(
                BookmakerCapability.SETTLED_POSITIONS_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
            BookmakerCapabilityFact(
                BookmakerCapability.BET_READBACK,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=observed_at,
        source_ref="betfair://profile/test",
        source_payload_sha256="b" * 64,
    )


def _bound():
    intent, policy, book = _intent()
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
        routing_request_id="route-supervised-1",
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
        approval_id="approval-1",
        portfolio_plan_sha256=portfolio.plan_sha256,
        intent_id=intent.intent_id,
        routing_request_id=route.routing_request_id,
        execution_terms_sha256=supervised_execution_terms_sha256(route, (constraint,)),
        approved_at=APPROVED_AT,
        expires_at=APPROVAL_EXPIRES_AT,
        evidence_sha256="c" * 64,
    )
    bound = build_supervised_execution_plan(
        portfolio,
        (intent,),
        route,
        (_profile(),),
        approval,
        (constraint,),
        created_at=CREATED_AT,
    )
    return bound, approval, portfolio, intent


def _ledger_with_unknown(path: Path):
    bound, approval, portfolio, intent = _bound()
    ledger = RealExecutionLedger(path)
    reserve_supervised_plan(ledger, bound, approval, reserved_at=RESERVED_AT)
    action = bound.execution_plan.actions[0]
    begin_supervised_attempt(
        ledger,
        bound,
        approval,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at=RESERVED_AT,
    )
    ledger.mark_submitted("attempt-1", submitted_at=SUBMITTED_AT)
    ledger.mark_unknown("attempt-1", reason="ambiguous_external_effect", observed_at=UNKNOWN_AT)
    return ledger, bound, approval, action, portfolio, intent


def _snapshot(
    action,
    *,
    observed_at: str = READBACK_AT,
    amount: Decimal | None = None,
    odds: Decimal | None = None,
    receipt_id: str = "bet-1",
    include_open: bool = True,
    include_settled_capability: bool = True,
) -> BookmakerAccountSnapshot:
    observed = {BookmakerCapability.OPEN_POSITIONS_READ}
    if include_settled_capability:
        observed.add(BookmakerCapability.SETTLED_POSITIONS_READ)
    positions = ()
    if include_open:
        positions = (
            BookmakerPositionObservation(
                venue_id="betfair",
                account_id="acct-1",
                adapter_id="betfair-exchange-jsonrpc-readonly",
                observation_id=f"open:{receipt_id}:d",
                external_position_id=receipt_id,
                state=BookmakerPositionState.OPEN,
                currency="USD",
                observed_at=observed_at,
                source_payload_sha256="d" * 64,
                provider_amount=amount if amount is not None else action.requested_stake,
                provider_amount_semantics="betfair_size_matched",
                provider_side="BACK",
                decimal_odds=odds if odds is not None else action.requested_odds,
            ),
        )
    return BookmakerAccountSnapshot(
        profile=_profile(observed_at=DECISION_TS),
        observed_capabilities=frozenset(observed),
        observed_at=observed_at,
        balance=None,
        open_positions=positions,
        settled_positions=(),
    )


def test_build_binds_portfolio_intent_approval_profiles_and_quote_constraints() -> None:
    bound, approval, portfolio, intent = _bound()
    execution = bound.execution_plan
    action = execution.actions[0]

    assert portfolio.plan_sha256 in execution.decision_id
    assert intent.intent_sha256 in execution.decision_id
    assert execution.approval_id == approval.ledger_identity
    assert execution.plan_id.startswith("supervised-v1-")
    assert execution.bookmaker_profile_version.startswith("profile-set-v1-")
    assert action.bookmaker_id == "betfair"
    assert action.account_id == "acct-1"
    assert action.event_id == "event-1"
    assert action.market_id == "winner"
    assert action.selection_id == "home"
    assert action.side == "BACK"
    assert action.requested_stake == portfolio.stakes[0]
    assert action.expires_at == QUOTE_EXPIRES_AT
    assert len(action.quote_id) == 64


def test_approval_binds_exact_route_and_slippage_terms() -> None:
    intent, policy, book = _intent()
    graph = PortfolioDependencyGraph.for_inputs(book, (intent,))
    portfolio = build_portfolio_plan(
        book, (intent,), policy, DECISION_TS, dependency_graph=graph
    )
    venue = VenueQuote("betfair", "acct-1", intent.opportunity.quotes[0], Decimal("1000"))
    route = plan_equal_split_residual(
        portfolio.stakes[0],
        (venue,),
        routing_request_id="route-terms-1",
        parent_plan_id=portfolio.plan_sha256,
        stake_quantum=Decimal("0.01"),
    )
    approved_constraint = ExecutionLegConstraint(
        route.legs[0].leg_id, "BACK", QUOTE_EXPIRES_AT, Decimal("0.01")
    )
    approval = SupervisedApproval(
        approval_id="approval-terms",
        portfolio_plan_sha256=portfolio.plan_sha256,
        intent_id=intent.intent_id,
        routing_request_id=route.routing_request_id,
        execution_terms_sha256=supervised_execution_terms_sha256(
            route, (approved_constraint,)
        ),
        approved_at=APPROVED_AT,
        expires_at=APPROVAL_EXPIRES_AT,
        evidence_sha256="7" * 64,
    )
    changed_constraint = replace(
        approved_constraint, max_slippage_fraction=Decimal("0.05")
    )
    with pytest.raises(SupervisedExecutionError, match="exact execution terms"):
        build_supervised_execution_plan(
            portfolio,
            (intent,),
            route,
            (_profile(),),
            approval,
            (changed_constraint,),
            created_at=CREATED_AT,
        )


def test_revoked_approval_cannot_begin_attempt_after_plan_reservation() -> None:
    bound, approval, _, _ = _bound()
    revoked = replace(approval, state=ApprovalState.REVOKED)
    with tempfile.TemporaryDirectory() as tmp:
        ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
        reserve_supervised_plan(ledger, bound, approval, reserved_at=RESERVED_AT)
        with pytest.raises(SupervisedExecutionError, match="not APPROVED|changed or was revoked"):
            begin_supervised_attempt(
                ledger,
                bound,
                revoked,
                action_id=bound.execution_plan.actions[0].action_id,
                attempt_id="attempt-revoked",
                reserved_at=RESERVED_AT,
            )


def test_generic_snapshot_cannot_authorize_positive_effect_but_exact_readback_can() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "execution.jsonl"
        ledger, bound, _, action, _, _ = _ledger_with_unknown(path)
        generic = reconcile_account_snapshot(
            ledger,
            bound,
            attempt_id="attempt-1",
            snapshot=_snapshot(
                action,
                amount=action.requested_stake / Decimal("2"),
                odds=Decimal("1.99"),
            ),
            external_receipt_id="bet-1",
        )

        assert generic.outcome is ReadbackOutcome.UNKNOWN
        assert ledger.attempt_state("attempt-1") is AttemptState.UNKNOWN
        assert ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        ) is False

        exact = reconcile_provider_readback(
            ledger,
            bound,
            attempt_id="attempt-1",
            readback=ProviderReadback(
                bookmaker_id=action.bookmaker_id,
                account_id=action.account_id,
                action_id=action.action_id,
                adapter_id="betfair-exchange-jsonrpc-readonly",
                adapter_version="1",
                profile_version=1,
                event_id=action.event_id,
                market_id=action.market_id,
                selection_id=action.selection_id,
                external_receipt_id="bet-1",
                observed_at=READBACK_AT,
                source_payload_sha256="d" * 64,
                status=AcknowledgementStatus.PARTIAL,
                reconciliation_evidence_required=True,
                accepted_odds=Decimal("1.99"),
                accepted_stake=action.requested_stake / Decimal("2"),
            ),
        )
        assert exact.outcome is ReadbackOutcome.PARTIAL
        assert exact.attempt_state is AttemptState.PARTIAL
        before_replay = ledger.verified_snapshot().event_count
        replay = reconcile_provider_readback(
            ledger,
            bound,
            attempt_id="attempt-1",
            readback=ProviderReadback(
                bookmaker_id=action.bookmaker_id,
                account_id=action.account_id,
                action_id=action.action_id,
                adapter_id="betfair-exchange-jsonrpc-readonly",
                adapter_version="1",
                profile_version=1,
                event_id=action.event_id,
                market_id=action.market_id,
                selection_id=action.selection_id,
                external_receipt_id="bet-1",
                observed_at=READBACK_AT,
                source_payload_sha256="d" * 64,
                status=AcknowledgementStatus.PARTIAL,
                reconciliation_evidence_required=True,
                accepted_odds=Decimal("1.99"),
                accepted_stake=action.requested_stake / Decimal("2"),
            ),
        )
        assert replay == exact
        assert ledger.verified_snapshot().event_count == before_replay

        restarted = RealExecutionLedger(path)
        assert restarted.verify_integrity() > 0
        assert restarted.attempt_state("attempt-1") is AttemptState.PARTIAL
        assert restarted.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        ) is False


def test_generic_absence_does_not_release_retry_but_exact_current_cleared_proof_does() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger, bound, _, action, _, _ = _ledger_with_unknown(Path(tmp) / "execution.jsonl")
        generic = reconcile_account_snapshot(
            ledger,
            bound,
            attempt_id="attempt-1",
            snapshot=_snapshot(
                action,
                observed_at="2026-09-18T13:20:07+00:00",
                include_open=False,
                include_settled_capability=True,
            ),
            external_receipt_id="caller-selected-not-found-id",
        )
        assert generic.outcome is ReadbackOutcome.UNKNOWN
        assert ledger.attempt_state("attempt-1") is AttemptState.UNKNOWN
        assert ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        ) is False

        exact = reconcile_provider_not_found(
            ledger,
            bound,
            attempt_id="attempt-1",
            readback=ProviderNotFoundReadback(
                bookmaker_id=action.bookmaker_id,
                account_id=action.account_id,
                action_id=action.action_id,
                adapter_id="betfair-exchange-jsonrpc-readonly",
                adapter_version="1",
                profile_version=1,
                event_id=action.event_id,
                market_id=action.market_id,
                selection_id=action.selection_id,
                observed_at="2026-09-18T13:20:08+00:00",
                current_source_payload_sha256="3" * 64,
                cleared_source_payload_sha256="4" * 64,
            ),
        )
        assert exact.outcome is ReadbackOutcome.NOT_FOUND
        assert exact.attempt_state is AttemptState.RECONCILED_NOT_FOUND
        assert ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        ) is True


def test_not_found_readback_rejects_cross_market_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger, bound, _, action, _, _ = _ledger_with_unknown(Path(tmp) / "execution.jsonl")
        evidence = ProviderNotFoundReadback(
            bookmaker_id=action.bookmaker_id,
            account_id=action.account_id,
            action_id=action.action_id,
            adapter_id="betfair-exchange-jsonrpc-readonly",
            adapter_version="1",
            profile_version=1,
            event_id=action.event_id,
            market_id="different-market",
            selection_id=action.selection_id,
            observed_at=READBACK_AT,
            current_source_payload_sha256="5" * 64,
            cleared_source_payload_sha256="6" * 64,
        )
        with pytest.raises(SupervisedExecutionError, match="identity mismatches"):
            reconcile_provider_not_found(
                ledger, bound, attempt_id="attempt-1", readback=evidence
            )
        assert ledger.attempt_state("attempt-1") is AttemptState.UNKNOWN


def test_provider_readback_rejects_adverse_slippage_and_identity_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger, bound, _, action, _, _ = _ledger_with_unknown(Path(tmp) / "execution.jsonl")
        bad_slippage = ProviderReadback(
            bookmaker_id="betfair",
            account_id="acct-1",
            action_id=action.action_id,
            adapter_id="betfair-exchange-jsonrpc-readonly",
            adapter_version="1",
            profile_version=1,
            event_id=action.event_id,
            market_id=action.market_id,
            selection_id=action.selection_id,
            external_receipt_id="bet-slip",
            observed_at=READBACK_AT,
            source_payload_sha256="f" * 64,
            status=AcknowledgementStatus.ACCEPTED,
            reconciliation_evidence_required=True,
            accepted_odds=Decimal("1.80"),
            accepted_stake=action.requested_stake,
        )
        with pytest.raises(SupervisedExecutionError, match="slippage"):
            reconcile_provider_readback(
                ledger,
                bound,
                attempt_id="attempt-1",
                readback=bad_slippage,
            )

        wrong_account = replace(
            bad_slippage,
            accepted_odds=Decimal("2.00"),
            account_id="different-account",
        )
        with pytest.raises(SupervisedExecutionError, match="identity mismatches"):
            reconcile_provider_readback(
                ledger,
                bound,
                attempt_id="attempt-1",
                readback=wrong_account,
            )

        wrong_market = replace(
            bad_slippage,
            accepted_odds=Decimal("2.00"),
            market_id="different-market",
        )
        with pytest.raises(SupervisedExecutionError, match="identity mismatches"):
            reconcile_provider_readback(
                ledger,
                bound,
                attempt_id="attempt-1",
                readback=wrong_market,
            )


def test_explicit_rejected_readback_is_recorded_without_provider_write_surface() -> None:
    bound, approval, _, _ = _bound()
    with tempfile.TemporaryDirectory() as tmp:
        ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
        reserve_supervised_plan(ledger, bound, approval, reserved_at=RESERVED_AT)
        action = bound.execution_plan.actions[0]
        begin_supervised_attempt(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-rejected",
            reserved_at=RESERVED_AT,
        )
        ledger.mark_submitted("attempt-rejected", submitted_at=SUBMITTED_AT)
        readback = ProviderReadback(
            bookmaker_id=action.bookmaker_id,
            account_id=action.account_id,
            action_id=action.action_id,
            adapter_id="betfair-exchange-jsonrpc-readonly",
            adapter_version="1",
            profile_version=1,
            event_id=action.event_id,
            market_id=action.market_id,
            selection_id=action.selection_id,
            external_receipt_id="provider-rejection-1",
            observed_at=READBACK_AT,
            source_payload_sha256="1" * 64,
            status=AcknowledgementStatus.REJECTED,
        )
        result = reconcile_provider_readback(
            ledger,
            bound,
            attempt_id="attempt-rejected",
            readback=readback,
        )
        assert result.outcome is ReadbackOutcome.REJECTED
        assert result.attempt_state is AttemptState.REJECTED


def test_bridge_rejects_caller_asserted_terminal_settlement_exactness() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger, bound, _, action, _, _ = _ledger_with_unknown(Path(tmp) / "execution.jsonl")
        readback = ProviderReadback(
            bookmaker_id=action.bookmaker_id,
            account_id=action.account_id,
            action_id=action.action_id,
            adapter_id="betfair-exchange-jsonrpc-readonly",
            adapter_version="1",
            profile_version=1,
            event_id=action.event_id,
            market_id=action.market_id,
            selection_id=action.selection_id,
            external_receipt_id="bet-settled-1",
            observed_at=READBACK_AT,
            source_payload_sha256="2" * 64,
            status=AcknowledgementStatus.ACCEPTED,
            reconciliation_evidence_required=True,
            accepted_odds=action.requested_odds,
            accepted_stake=action.requested_stake,
            terminal_settlement_exact=True,
        )
        with pytest.raises(SupervisedExecutionError, match="settlement exactness"):
            reconcile_provider_readback(
                ledger,
                bound,
                attempt_id="attempt-1",
                readback=readback,
            )
