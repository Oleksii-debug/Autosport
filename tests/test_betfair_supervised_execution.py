from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionError,
    BetfairSupervisedExecutionGate,
    BetfairSupervisedPlaceOrdersClient,
    PlaceOrdersOutcome,
    execute_betfair_supervised_action,
    read_betfair_supervised_action_readback,
)
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_routing import VenueQuote
from autosport.bookmaker_routing_plan import plan_equal_split_residual
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
)
from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionStateError,
    RealExecutionLedger,
)
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext
from autosport.supervised_execution import (
    ExecutionLegConstraint,
    SupervisedApproval,
    build_supervised_execution_plan,
    reconcile_provider_not_found,
    reserve_supervised_plan,
    supervised_execution_terms_sha256,
    verify_betfair_provider_state,
)

DECISION_TS = "2026-09-19T08:00:00+00:00"
APPROVED_AT = "2026-09-19T08:00:01+00:00"
CREATED_AT = "2026-09-19T08:00:02+00:00"
RESERVED_AT = "2026-09-19T08:00:03+00:00"
SUBMITTED_AT = "2026-09-19T08:00:04+00:00"
READBACK_AT = "2026-09-19T08:00:05+00:00"
QUOTE_EXPIRES_AT = "2026-09-19T08:01:00+00:00"
APPROVAL_EXPIRES_AT = "2026-09-19T08:05:00+00:00"


@pytest.fixture(autouse=True)
def _fixed_supervised_clock(monkeypatch) -> None:
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: RESERVED_AT,
    )


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=7,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BET_READBACK,
                BookmakerCapabilityState.SUPPORTED,
            ),
            BookmakerCapabilityFact(
                BookmakerCapability.PLACE_BET,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=DECISION_TS,
        source_ref="betfair://supervised-write-capability/test",
        source_payload_sha256="b" * 64,
    )


def _bound(profile: BookmakerCapabilityProfile):
    goal = EconomicGoalContract(
        goal_id="goal-betfair-placeorders",
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
    market_event = MarketEvent(
        event_id=leg.event_id,
        market_id=leg.market_id,
        selection_id=leg.selection_id,
        decimal_odds=leg.locked_odds,
        observed_ts="2026-09-19T07:59:59+00:00",
        source_id="betfair",
        sequence=1,
        source_ts="2026-09-19T07:59:59+00:00",
        ingest_ts="2026-09-19T07:59:59+00:00",
        sport="soccer",
    )
    risk_context = ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(market_event,),
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
        proposal_ts=DECISION_TS,
    )
    quote = QuoteRef.from_market_event(
        market_event,
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
        intent_id="intent-betfair-placeorders",
        opportunity=opportunity,
        evidence=OpportunityEvidence(
            evidence_id="evidence-betfair-placeorders",
            observed_at="2026-09-19T07:59:59+00:00",
            causal_cutoff="2026-09-19T07:59:58+00:00",
            reproducibility_sha256="a" * 64,
        ),
        risk_context=risk_context,
        signal_strength=Decimal("0.05"),
        strategy_id="strategy-live-1",
        config_sha256="e" * 64,
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
    venue = VenueQuote(
        "betfair",
        "acct-1",
        quote,
        Decimal("1000"),
    )
    route = plan_equal_split_residual(
        portfolio.stakes[0],
        (venue,),
        routing_request_id="route-betfair-placeorders",
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
        approval_id="approval-betfair-placeorders",
        portfolio_plan_sha256=portfolio.plan_sha256,
        intent_id=intent.intent_id,
        routing_request_id=route.routing_request_id,
        execution_terms_sha256=supervised_execution_terms_sha256(
            route,
            (constraint,),
        ),
        approved_at=APPROVED_AT,
        expires_at=APPROVAL_EXPIRES_AT,
        evidence_sha256="c" * 64,
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
    return bound, approval


class _Transport:
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body.decode("utf-8"))
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "request": request,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.responder(request)


class _TimeoutTransport(_Transport):
    def __init__(self):
        super().__init__(lambda _: b"")

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body.decode("utf-8"))
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "request": request,
                "timeout_seconds": timeout_seconds,
            }
        )
        raise BetfairReadOnlyError("simulated timeout")


def _response(
    request: dict[str, object],
    *,
    execution_status: str = "SUCCESS",
    instruction_status: str = "SUCCESS",
    matched: Decimal | str = "0",
    average: Decimal | str = "0",
    bet_id: str | None = "bet-123",
) -> bytes:
    params = request["params"]
    instruction = params["instructions"][0]
    report: dict[str, object] = {
        "status": instruction_status,
        "instruction": instruction,
        "placedDate": READBACK_AT,
        "averagePriceMatched": str(average),
        "sizeMatched": str(matched),
    }
    result: dict[str, object] = {
        "status": execution_status,
        "marketId": params["marketId"],
        "instructionReports": [report],
    }
    if bet_id is not None:
        report["betId"] = bet_id
    if instruction_status == "FAILURE":
        report["errorCode"] = "BET_TAKEN_OR_LAPSED"
    if execution_status == "FAILURE":
        result["errorCode"] = "BET_ACTION_ERROR"
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": result,
            "id": request["id"],
        }
    ).encode("utf-8")


class _ReadbackTransport:
    def __init__(self, *, provider_order_ref: str, action) -> None:
        self.provider_order_ref = provider_order_ref
        self.action = action
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body.decode("utf-8"))
        self.calls.append(request)
        method = request["method"]
        params = request["params"]
        if method.endswith("listMarketCatalogue"):
            result: object = [
                {
                    "marketId": self.action.market_id,
                    "event": {"id": self.action.event_id},
                }
            ]
        elif method.endswith("listCurrentOrders"):
            assert params["customerOrderRefs"] == [
                self.provider_order_ref
            ]
            result = {
                "currentOrders": [],
                "moreAvailable": False,
            }
        elif method.endswith("listClearedOrders"):
            assert params["customerOrderRefs"] == [
                self.provider_order_ref
            ]
            result = {
                "clearedOrders": [],
                "moreAvailable": False,
            }
        else:
            raise AssertionError(f"unexpected readback method: {method}")
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "result": result,
                "id": request["id"],
            }
        ).encode("utf-8")


def _enabled_client(profile, transport):
    gate = BetfairSupervisedExecutionGate(
        enabled=True,
        bookmaker_id="betfair",
        account_id="acct-1",
        profile_sha256=profile.profile_id,
        authority_ref="owner-supervised-activation:test",
        authority_sha256="d" * 64,
    )
    return BetfairSupervisedPlaceOrdersClient(
        BetfairSessionCredentials("app-key", "session-token"),
        gate=gate,
        transport=transport,
        clock=lambda: READBACK_AT,
    )


def _prepared(tmp: str):
    profile = _profile()
    bound, approval = _bound(profile)
    ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
    reserve_supervised_plan(ledger, bound, approval)
    action = bound.execution_plan.actions[0]
    return profile, bound, approval, ledger, action


def test_default_gate_cannot_reach_transport() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = BetfairSupervisedPlaceOrdersClient(
            BetfairSessionCredentials("app-key", "session-token"),
            transport=transport,
            clock=lambda: READBACK_AT,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="gate is disabled",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-disabled",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(
            bound.execution_plan.plan_id
        ).attempts == {}


def test_full_match_persists_provider_report_and_canonical_ack() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(profile, transport)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-accepted",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.ACCEPTED
        assert result.attempt_state is AttemptState.ACCEPTED
        assert result.external_receipt_id == "bet-123"
        assert result.evidence_id is not None
        binding = ledger.provider_evidence_binding(
            "attempt-accepted"
        )
        assert binding is not None
        assert binding["evidence_id"] == result.evidence_id
        request = transport.calls[0]["request"]
        assert request["method"] == "SportsAPING/v1.0/placeOrders"
        assert request["params"]["async"] is False
        assert len(request["params"]["customerRef"]) == 32
        provider_ref = ledger.provider_order_reference(
            attempt_id="attempt-accepted",
            provider_id="betfair",
        )
        assert provider_ref is not None
        assert len(provider_ref) == 32
        assert (
            request["params"]["instructions"][0][
                "customerOrderRef"
            ]
            == provider_ref
        )
        assert provider_ref != action.action_id
        assert ledger.verify_integrity() > 0


def test_processed_with_errors_single_success_maps_partial_exactly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                execution_status="PROCESSED_WITH_ERRORS",
                matched=(
                    action.requested_stake
                    / Decimal("2")
                ),
                average=action.requested_odds,
            )
        )
        client = _enabled_client(profile, transport)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-partial",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.PARTIAL
        assert result.attempt_state is AttemptState.PARTIAL
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_provider_failure_report_is_rejected_not_inferred_from_absence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                execution_status="FAILURE",
                instruction_status="FAILURE",
                matched="0",
                average="0",
                bet_id=None,
            )
        )
        client = _enabled_client(profile, transport)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-rejected",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.REJECTED
        assert result.attempt_state is AttemptState.REJECTED
        assert result.evidence_id is not None
        provider_ref = ledger.provider_order_reference(
            attempt_id="attempt-rejected",
            provider_id="betfair",
        )
        assert provider_ref is not None
        assert result.external_receipt_id == provider_ref


def test_transport_timeout_becomes_unknown_and_blocks_retry_after_restart() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action = _prepared(tmp)
        transport = _TimeoutTransport()
        client = _enabled_client(profile, transport)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-timeout",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        restarted = RealExecutionLedger(
            Path(tmp) / "real.jsonl"
        )
        assert (
            restarted.attempt_state("attempt-timeout")
            is AttemptState.UNKNOWN
        )
        assert not restarted.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )
        provider_ref = restarted.provider_order_reference(
            attempt_id="attempt-timeout",
            provider_id="betfair",
        )
        assert provider_ref is not None
        assert len(provider_ref) == 32

        read_transport = _ReadbackTransport(
            provider_order_ref=provider_ref,
            action=action,
        )
        read_client = BetfairReadOnlyClient(
            BetfairSessionCredentials("app-key", "session-token"),
            transport=read_transport,
            clock=lambda: datetime.fromisoformat(READBACK_AT),
            venue_id="betfair",
            account_id="acct-1",
        )
        envelope = read_betfair_supervised_action_readback(
            read_client,
            restarted,
            bound,
            attempt_id="attempt-timeout",
        )
        assert envelope.action_id == action.action_id
        assert envelope.provider_order_ref == provider_ref
        assert all(
            call["params"].get("customerOrderRefs")
            in (None, [provider_ref])
            for call in read_transport.calls
        )
        verified_absence = verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=envelope,
        )
        reconciliation = reconcile_provider_not_found(
            restarted,
            bound,
            attempt_id="attempt-timeout",
            readback=verified_absence,
        )
        assert reconciliation.attempt_state is AttemptState.RECONCILED_NOT_FOUND
        assert restarted.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )

        first_customer_ref = transport.calls[0]["request"]["params"][
            "customerRef"
        ]
        retry_transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        retry_client = _enabled_client(profile, retry_transport)
        retry_result = execute_betfair_supervised_action(
            restarted,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-timeout-retry",
            profile=profile,
            client=retry_client,
            clock=lambda: "2026-09-19T08:00:09+00:00",
        )
        assert retry_result.outcome is PlaceOrdersOutcome.ACCEPTED
        retry_request = retry_transport.calls[0]["request"]
        assert retry_request["params"]["customerRef"] != first_customer_ref
        assert (
            retry_request["params"]["instructions"][0]["customerOrderRef"]
            != provider_ref
        )


def test_unmatched_success_is_unknown_until_readback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched="0",
                average="0",
                bet_id="bet-unmatched",
            )
        )
        client = _enabled_client(profile, transport)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-unmatched",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.external_receipt_id == "bet-unmatched"
        assert result.evidence_id is not None
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_duplicate_provider_json_key_is_unknown_not_terminal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action = _prepared(tmp)

        def duplicate_status_response(request: dict[str, object]) -> bytes:
            payload = _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            ).decode("utf-8")
            duplicate = payload.replace(
                '"status": "SUCCESS"',
                '"status": "FAILURE", "status": "SUCCESS"',
                1,
            )
            assert duplicate != payload
            return duplicate.encode("utf-8")

        transport = _Transport(duplicate_status_response)
        client = _enabled_client(profile, transport)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-duplicate-json",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is None
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )
        assert len(transport.calls) == 1


def test_duplicate_terminal_attempt_never_calls_placeorders_twice() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(profile, transport)

        execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-once",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )
        with pytest.raises(ExecutionStateError):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-once",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert len(transport.calls) == 1


def test_client_repr_never_exposes_session_credentials() -> None:
    credentials = BetfairSessionCredentials(
        "super-secret-app",
        "super-secret-session",
    )
    client = BetfairSupervisedPlaceOrdersClient(
        credentials
    )
    rendered = repr(client)
    assert "super-secret-app" not in rendered
    assert "super-secret-session" not in rendered
    assert "enabled=False" in rendered
