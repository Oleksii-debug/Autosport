from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.betfair_account_readonly as betfair_account_readonly
import autosport.betfair_supervised_execution as betfair_supervised_execution
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
from autosport.economic_goal import AutomationLevel, EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.economic_goal_store import EconomicGoalStore
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
    SupervisedExecutionError,
    begin_supervised_attempt,
    build_supervised_execution_plan,
    reconcile_provider_not_found,
    reconcile_provider_readback,
    reserve_supervised_plan,
    supervised_execution_terms_sha256,
    verify_betfair_provider_state,
)
from autosport.supervised_provider_evidence import ProviderEvidenceError
from autosport.workspace_lock import WorkspaceEconomicLockBusyError

DECISION_TS = "2026-09-19T08:00:00+00:00"
APPROVED_AT = "2026-09-19T08:00:01+00:00"
CREATED_AT = "2026-09-19T08:00:02+00:00"
RESERVED_AT = "2026-09-19T08:00:03+00:00"
SUBMITTED_AT = "2026-09-19T08:00:04+00:00"
READBACK_AT = "2026-09-19T08:00:05+00:00"
QUOTE_EXPIRES_AT = "2026-09-19T08:01:00+00:00"
APPROVAL_EXPIRES_AT = "2026-09-19T08:05:00+00:00"

_ACTIVE_WRITE_TRANSPORT = None


@pytest.fixture(autouse=True)
def _fixed_supervised_clock(monkeypatch) -> None:
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: RESERVED_AT,
    )


@pytest.fixture(autouse=True)
def _canonical_write_network_seam(monkeypatch):
    global _ACTIVE_WRITE_TRANSPORT
    _ACTIVE_WRITE_TRANSPORT = None
    monkeypatch.setattr(
        betfair_account_readonly,
        "urlopen",
        _test_urlopen,
    )
    yield
    _ACTIVE_WRITE_TRANSPORT = None


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


def _goal(**changes) -> EconomicGoalContract:
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
        automation_level=AutomationLevel.SUPERVISED_EXECUTION,
    )
    return replace(goal, **changes) if changes else goal


def _bound(
    profile: BookmakerCapabilityProfile,
    goal: EconomicGoalContract | None = None,
    *,
    selection_id: str = "42",
):
    goal = goal or _goal()
    policy = PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=goal,
    )
    leg = TicketLeg(
        "event-1",
        "1.23456789",
        selection_id,
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
    return bound, approval, goal


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


class _UrlopenResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self, max_bytes: int) -> bytes:
        return self._payload[:max_bytes]


def _test_urlopen(request, timeout):
    if _ACTIVE_WRITE_TRANSPORT is None:
        raise AssertionError("canonical Betfair network seam has no test responder")
    payload = _ACTIVE_WRITE_TRANSPORT.post(
        request.full_url,
        headers=dict(request.header_items()),
        body=request.data or b"",
        timeout_seconds=timeout,
    )
    return _UrlopenResponse(payload)


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
    order_status: str | None = None,
) -> bytes:
    params = request["params"]
    instruction = params["instructions"][0]
    request_limit = instruction["limitOrder"]
    response_instruction = dict(instruction)
    response_instruction["limitOrder"] = {
        "size": float(Decimal(str(request_limit["size"]))),
        "price": float(Decimal(str(request_limit["price"]))),
        "persistenceType": request_limit["persistenceType"],
    }
    report: dict[str, object] = {
        "status": instruction_status,
        "instruction": response_instruction,
        "placedDate": READBACK_AT,
        "averagePriceMatched": float(Decimal(str(average))),
        "sizeMatched": float(Decimal(str(matched))),
    }
    result: dict[str, object] = {
        "status": execution_status,
        "marketId": params["marketId"],
        "instructionReports": [report],
    }
    if bet_id is not None:
        report["betId"] = bet_id
    if order_status is not None:
        report["orderStatus"] = order_status
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
    def __init__(
        self,
        *,
        provider_order_ref: str,
        action,
        include_effect: bool = False,
    ) -> None:
        self.provider_order_ref = provider_order_ref
        self.action = action
        self.include_effect = include_effect
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
            current_orders: list[dict[str, object]] = []
            if self.include_effect:
                current_orders.append(
                    {
                        "betId": "bet-readback-effect",
                        "marketId": self.action.market_id,
                        "selectionId": int(self.action.selection_id),
                        "side": self.action.side,
                        "status": "EXECUTABLE",
                        "placedDate": SUBMITTED_AT,
                        "averagePriceMatched": float(self.action.requested_odds),
                        "sizeMatched": float(self.action.requested_stake),
                        "sizeRemaining": 0,
                        "customerOrderRef": self.provider_order_ref,
                    }
                )
            result = {
                "currentOrders": current_orders,
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


def _enabled_client(
    profile,
    transport,
    *,
    store: EconomicGoalStore,
    observed_at: str = READBACK_AT,
):
    global _ACTIVE_WRITE_TRANSPORT
    _ACTIVE_WRITE_TRANSPORT = transport
    gate = BetfairSupervisedExecutionGate.from_economic_goal_store(
        store,
        bookmaker_id="betfair",
        account_id="acct-1",
        profile_sha256=profile.profile_id,
    )
    return BetfairSupervisedPlaceOrdersClient(
        BetfairSessionCredentials("app-key", "session-token"),
        gate=gate,
        clock=lambda: observed_at,
    )


def _prepared(
    tmp: str,
    *,
    goal: EconomicGoalContract | None = None,
    selection_id: str = "42",
):
    profile = _profile()
    bound, approval, goal = _bound(
        profile,
        goal,
        selection_id=selection_id,
    )
    goal_store = EconomicGoalStore(Path(tmp))
    goal_store.initialize_owner(goal)
    ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
    reserve_supervised_plan(ledger, bound, approval)
    action = bound.execution_plan.actions[0]
    return profile, bound, approval, ledger, action, goal_store


def _assert_current_goal_denied_before_effect(
    tmp: str,
    successor: EconomicGoalContract,
    *,
    match: str,
) -> None:
    profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
    transport = _Transport(lambda request: _response(request))
    client = _enabled_client(profile, transport, store=goal_store)
    goal_store.persist_automatic_successor(successor)

    with pytest.raises(BetfairSupervisedExecutionError, match=match):
        execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-owner-denied",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

    assert transport.calls == []
    assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


@pytest.mark.parametrize(
    "selection_id",
    ("selection-x", "042"),
)
def test_invalid_betfair_selection_fails_before_attempt_or_transport(
    selection_id: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(
            tmp,
            selection_id=selection_id,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="selection_id must be canonical positive integer text",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-invalid-selection",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_default_gate_cannot_reach_transport() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
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


def test_enabled_gate_rejects_opaque_authority_without_owner_store() -> None:
    profile = _profile()
    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="canonical EconomicGoalStore",
    ):
        BetfairSupervisedExecutionGate(
            enabled=True,
            bookmaker_id="betfair",
            account_id="acct-1",
            profile_sha256=profile.profile_id,
            authority_ref="caller-minted",
            authority_sha256="d" * 64,
        )


def test_caller_minted_authority_cannot_enable_with_real_owner_store() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = BetfairSupervisedPlaceOrdersClient(
            BetfairSessionCredentials("app-key", "session-token"),
            gate=BetfairSupervisedExecutionGate(
                enabled=True,
                bookmaker_id="betfair",
                account_id="acct-1",
                profile_sha256=profile.profile_id,
                authority_ref="caller-minted",
                authority_sha256="d" * 64,
                economic_goal_store=goal_store,
            ),
            transport=transport,
            clock=lambda: READBACK_AT,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="does not match bound execution plan",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-minted-owner",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_shadow_owner_store_cannot_bypass_canonical_emergency_stop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        canonical_workspace = root / "canonical"
        shadow_workspace = root / "shadow"
        profile, bound, approval, ledger, action, canonical_store = _prepared(
            str(canonical_workspace)
        )
        shadow_store = EconomicGoalStore(shadow_workspace)
        shadow_store.initialize_owner(_goal())
        canonical_store.persist_automatic_successor(
            _goal(revision=2, emergency_stop=True)
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(
            profile,
            transport,
            store=shadow_store,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="trusted execution workspace",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-shadow-owner",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_redirected_store_object_cannot_hide_canonical_emergency_stop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        canonical_workspace = root / "canonical"
        shadow_workspace = root / "shadow"
        profile, bound, approval, ledger, action, goal_store = _prepared(
            str(canonical_workspace)
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(
            profile,
            transport,
            store=goal_store,
        )
        EconomicGoalStore(canonical_workspace).persist_automatic_successor(
            _goal(revision=2, emergency_stop=True)
        )
        shadow_store = EconomicGoalStore(shadow_workspace)
        shadow_store.initialize_owner(_goal())
        goal_store.workspace = shadow_workspace
        goal_store.path = shadow_store.path

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="emergency STOP",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-redirected-owner",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_bound_plan_preserves_exact_owner_goal_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        goal = _goal()
        _, bound, _, _, _, _ = _prepared(tmp, goal=goal)
        assert (
            bound.economic_goal_contract_sha256
            == provenance_for(goal).contract_sha256
        )


def test_lowered_automation_denies_before_attempt_or_transport() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _assert_current_goal_denied_before_effect(
            tmp,
            _goal(
                revision=2,
                automation_level=AutomationLevel.RECOMMENDATION,
            ),
            match="does not permit supervised execution",
        )


def test_emergency_stop_denies_before_attempt_or_transport() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _assert_current_goal_denied_before_effect(
            tmp,
            _goal(revision=2, emergency_stop=True),
            match="emergency STOP",
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"blocked_providers": frozenset({"betfair"})}, "blocks this provider"),
        ({"blocked_markets": frozenset({"1.23456789"})}, "blocks this market"),
    ],
)
def test_owner_deny_lists_stop_execution_before_effect(
    changes: dict[str, object],
    message: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _assert_current_goal_denied_before_effect(
            tmp,
            _goal(revision=2, **changes),
            match=message,
        )


def test_changed_owner_revision_invalidates_frozen_execution_plan() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _assert_current_goal_denied_before_effect(
            tmp,
            _goal(revision=2),
            match="does not match bound execution plan",
        )


def test_tightened_slippage_denies_frozen_plan_before_effect() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _assert_current_goal_denied_before_effect(
            tmp,
            _goal(
                revision=2,
                max_execution_slippage_fraction=Decimal("0.01"),
            ),
            match="slippage exceeds current owner authority",
        )


def test_corrupt_owner_store_denies_before_attempt_or_transport() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        goal_store.path.write_text("{}", encoding="utf-8")

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="cannot load durable owner execution authority",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-corrupt-owner",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_owner_tightening_cannot_commit_before_attempt_or_transport(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        stop_contract = _goal(revision=2, emergency_stop=True)
        begin_interleaving_attempted = False
        transport_interleaving_attempted = False

        def response_while_tightening_attempted(
            request: dict[str, object],
        ) -> bytes:
            nonlocal transport_interleaving_attempted
            transport_interleaving_attempted = True
            with pytest.raises(WorkspaceEconomicLockBusyError):
                goal_store.persist_automatic_successor(stop_contract)
            return _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )

        transport = _Transport(response_while_tightening_attempted)
        client = _enabled_client(profile, transport, store=goal_store)
        original_begin = begin_supervised_attempt

        def begin_after_tightening_attempt(*args, **kwargs):
            nonlocal begin_interleaving_attempted
            begin_interleaving_attempted = True
            with pytest.raises(WorkspaceEconomicLockBusyError):
                goal_store.persist_automatic_successor(stop_contract)
            return original_begin(*args, **kwargs)

        monkeypatch.setattr(
            "autosport.betfair_supervised_execution.begin_supervised_attempt",
            begin_after_tightening_attempt,
        )

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-owner-fenced",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert begin_interleaving_attempted
        assert transport_interleaving_attempted
        assert result.outcome is PlaceOrdersOutcome.ACCEPTED
        assert result.attempt_state is AttemptState.ACCEPTED
        assert goal_store.load().revision == 1
        assert len(transport.calls) == 1

        goal_store.persist_automatic_successor(stop_contract)
        assert goal_store.load().emergency_stop is True


def test_subclassed_write_client_cannot_mint_terminal_provider_truth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        gate = BetfairSupervisedExecutionGate.from_economic_goal_store(
            goal_store,
            bookmaker_id="betfair",
            account_id="acct-1",
            profile_sha256=profile.profile_id,
        )

        class EvilClient(BetfairSupervisedPlaceOrdersClient):
            def place_action(self, *args, **kwargs):
                raise AssertionError("virtual place_action must never be authoritative")

        client = EvilClient(
            BetfairSessionCredentials("app-key", "session-token"),
            gate=gate,
            clock=lambda: READBACK_AT,
        )

        with pytest.raises(TypeError, match="exact BetfairSupervisedPlaceOrdersClient"):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-evil-client",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_injected_write_transport_cannot_mint_terminal_provider_truth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        gate = BetfairSupervisedExecutionGate.from_economic_goal_store(
            goal_store,
            bookmaker_id="betfair",
            account_id="acct-1",
            profile_sha256=profile.profile_id,
        )
        client = BetfairSupervisedPlaceOrdersClient(
            BetfairSessionCredentials("app-key", "session-token"),
            gate=gate,
            transport=transport,
            clock=lambda: READBACK_AT,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical client, transport, and parser authority",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-injected-transport",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_replaced_write_method_cannot_mint_terminal_provider_truth(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)
        monkeypatch.setattr(
            BetfairSupervisedPlaceOrdersClient,
            "place_action",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("replaced place_action must never be authoritative")
            ),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical client, transport, and parser authority",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-replaced-method",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_replaced_response_parser_cannot_mint_terminal_provider_truth(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)
        monkeypatch.setattr(
            betfair_supervised_execution,
            "_parse_place_orders_response",
            lambda *args, **kwargs: object(),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical client, transport, and parser authority",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-replaced-parser",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_full_match_persists_provider_report_and_canonical_ack() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)

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


def test_processed_with_errors_single_success_is_unknown_until_readback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
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
        client = _enabled_client(profile, transport, store=goal_store)

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

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_provider_failure_without_bet_id_stays_unknown_until_readback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                execution_status="FAILURE",
                instruction_status="FAILURE",
                matched="0",
                average="0",
                bet_id=None,
                order_status="EXECUTION_COMPLETE",
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-failure-without-provider-id",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is not None
        assert result.external_receipt_id is None
        provider_ref = ledger.provider_order_reference(
            attempt_id="attempt-failure-without-provider-id",
            provider_id="betfair",
        )
        assert provider_ref is not None
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_partial_with_executable_remainder_stays_unknown_with_provider_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake / Decimal("2"),
                average=action.requested_odds,
                bet_id="bet-partial-live",
                order_status="EXECUTABLE",
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-partial-live-remainder",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is not None
        assert result.external_receipt_id == "bet-partial-live"
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_partial_with_execution_complete_is_terminal_partial() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake / Decimal("2"),
                average=action.requested_odds,
                bet_id="bet-partial-terminal",
                order_status="EXECUTION_COMPLETE",
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-partial-terminal",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.PARTIAL
        assert result.attempt_state is AttemptState.PARTIAL
        assert result.evidence_id is not None
        assert result.external_receipt_id == "bet-partial-terminal"
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_failure_with_positive_match_is_unknown_not_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                execution_status="FAILURE",
                instruction_status="FAILURE",
                matched=action.requested_stake / Decimal("2"),
                average=action.requested_odds,
                bet_id="bet-contradictory-match",
                order_status="EXECUTION_COMPLETE",
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-contradictory-match",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is None
        assert result.external_receipt_id is None
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_failure_with_executable_order_state_is_unknown_not_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                execution_status="FAILURE",
                instruction_status="FAILURE",
                matched="0",
                average="0",
                bet_id="bet-contradictory",
                order_status="EXECUTABLE",
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-contradictory-order-state",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is None
        assert result.external_receipt_id is None
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_submitted_attempt_reentry_never_resubmits_placeorders() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        attempt_id = "attempt-crash-after-submitted"

        begin_supervised_attempt(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id=attempt_id,
        )
        provider_ref = ledger.bind_provider_order_reference(
            attempt_id=attempt_id,
            provider_id=action.bookmaker_id,
        )
        ledger.mark_submitted(
            attempt_id,
            submitted_at=SUBMITTED_AT,
        )
        assert ledger.attempt_state(attempt_id) is AttemptState.SUBMITTED

        restarted = RealExecutionLedger(Path(tmp) / "real.jsonl")
        assert restarted.attempt_state(attempt_id) is AttemptState.SUBMITTED

        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(
            profile,
            transport,
            store=goal_store,
            observed_at=READBACK_AT,
        )

        result = execute_betfair_supervised_action(
            restarted,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id=attempt_id,
            profile=profile,
            client=client,
            clock=lambda: READBACK_AT,
        )

        assert transport.calls == []
        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is None
        assert result.external_receipt_id is None
        assert (
            restarted.provider_order_reference(
                attempt_id=attempt_id,
                provider_id=action.bookmaker_id,
            )
            == provider_ref
        )
        assert not restarted.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_transport_timeout_becomes_unknown_and_blocks_retry_after_restart(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _TimeoutTransport()
        client = _enabled_client(profile, transport, store=goal_store)

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
            expected_provider_order_ref=provider_ref,
        )
        assert verified_absence.provider_order_ref == provider_ref
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
        monkeypatch.setattr(
            "autosport.supervised_execution._trusted_now",
            lambda: "2026-09-19T08:00:06+00:00",
        )
        retry_transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        retry_client = _enabled_client(
            profile,
            retry_transport,
            store=goal_store,
            observed_at="2026-09-19T08:00:10+00:00",
        )
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


def test_foreign_provider_order_ref_cannot_verify_or_reconcile_effect() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        timeout_client = _enabled_client(
            profile, _TimeoutTransport(), store=goal_store
        )
        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-foreign-effect",
            profile=profile,
            client=timeout_client,
            clock=lambda: SUBMITTED_AT,
        )
        assert result.attempt_state is AttemptState.UNKNOWN
        owned_ref = ledger.provider_order_reference(
            attempt_id="attempt-foreign-effect",
            provider_id="betfair",
        )
        assert owned_ref is not None
        foreign_ref = "f" * 32
        assert foreign_ref != owned_ref

        transport = _ReadbackTransport(
            provider_order_ref=foreign_ref,
            action=action,
            include_effect=True,
        )
        client = BetfairReadOnlyClient(
            BetfairSessionCredentials("app-key", "session-token"),
            transport=transport,
            clock=lambda: datetime.fromisoformat(READBACK_AT),
            venue_id="betfair",
            account_id="acct-1",
        )
        envelope = client.read_execution_readback(
            action_id=action.action_id,
            provider_order_ref=foreign_ref,
            market_id=action.market_id,
        )

        with pytest.raises(
            ProviderEvidenceError,
            match="expected durable binding",
        ):
            verify_betfair_provider_state(
                action,
                profile,
                expected_profile_sha256=profile.profile_id,
                readback=envelope,
                expected_provider_order_ref=owned_ref,
            )

        foreign_effect = verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=envelope,
        )
        assert foreign_effect.provider_order_ref == foreign_ref
        with pytest.raises(
            SupervisedExecutionError,
            match="provider order reference mismatches durable attempt binding",
        ):
            reconcile_provider_readback(
                ledger,
                bound,
                attempt_id="attempt-foreign-effect",
                readback=foreign_effect,
            )
        assert ledger.attempt_state("attempt-foreign-effect") is AttemptState.UNKNOWN


def test_foreign_empty_provider_order_ref_cannot_release_retry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        timeout_client = _enabled_client(
            profile, _TimeoutTransport(), store=goal_store
        )
        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-foreign-absence",
            profile=profile,
            client=timeout_client,
            clock=lambda: SUBMITTED_AT,
        )
        assert result.attempt_state is AttemptState.UNKNOWN
        owned_ref = ledger.provider_order_reference(
            attempt_id="attempt-foreign-absence",
            provider_id="betfair",
        )
        assert owned_ref is not None
        foreign_ref = "e" * 32
        assert foreign_ref != owned_ref

        transport = _ReadbackTransport(
            provider_order_ref=foreign_ref,
            action=action,
        )
        client = BetfairReadOnlyClient(
            BetfairSessionCredentials("app-key", "session-token"),
            transport=transport,
            clock=lambda: datetime.fromisoformat(READBACK_AT),
            venue_id="betfair",
            account_id="acct-1",
        )
        envelope = client.read_execution_readback(
            action_id=action.action_id,
            provider_order_ref=foreign_ref,
            market_id=action.market_id,
        )
        foreign_absence = verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=envelope,
        )
        assert foreign_absence.provider_order_ref == foreign_ref

        with pytest.raises(
            SupervisedExecutionError,
            match="provider order reference mismatches durable attempt binding",
        ):
            reconcile_provider_not_found(
                ledger,
                bound,
                attempt_id="attempt-foreign-absence",
                readback=foreign_absence,
            )
        assert ledger.attempt_state("attempt-foreign-absence") is AttemptState.UNKNOWN
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_unmatched_success_preserves_known_order_until_readback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched="0",
                average="0",
                bet_id="bet-unmatched",
                order_status="EXECUTABLE",
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)

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

        assert result.outcome is PlaceOrdersOutcome.PLACED_UNMATCHED
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.external_receipt_id == "bet-unmatched"
        assert result.evidence_id is not None
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


@pytest.mark.parametrize("order_status", (None, "EXECUTION_COMPLETE"))
def test_zero_match_without_live_remainder_proof_stays_unknown_until_readback(
    order_status: str | None,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched="0",
                average="0",
                bet_id="bet-zero-match",
                order_status=order_status,
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-zero-match-nonlive-status",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.external_receipt_id == "bet-zero-match"
        assert result.evidence_id is not None
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_duplicate_provider_json_key_is_unknown_not_terminal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)

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
        client = _enabled_client(profile, transport, store=goal_store)

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
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)

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
