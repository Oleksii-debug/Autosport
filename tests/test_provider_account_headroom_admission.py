from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

import autosport.betfair_account_readonly as betfair_readonly
import autosport.provider_account_headroom_admission as headroom_module
from autosport.account_snapshot_acquisition import (
    AuthoritativeAccountSnapshot,
    BetfairAccountSnapshotAcquirer,
)
from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_routing import VenueQuote
from autosport.bookmaker_routing_plan import plan_equal_split_residual
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.economic_goal_store import EconomicGoalStore
from autosport.domain import MarketEvent, TicketLeg
from autosport.portfolio_plan import (
    EvidenceTruth,
    PortfolioAction,
    PortfolioDependencyGraph,
    PortfolioPlan,
    Opportunity,
    OpportunityDecision,
    OpportunityEvidence,
    OpportunityIntent,
    QuoteRef,
    StrategyClass,
)
from autosport.paper import PaperBook
from autosport.risk import ProposedTicketRiskContext
import autosport.supervised_execution as supervised_execution
from autosport.supervised_execution import BoundSupervisedExecutionPlan
from autosport.provider_account_headroom_admission import (
    HeadroomDecision,
    ProductInternalHeadroomReservation,
    ProviderAccountHeadroomError,
    ProviderAccountHeadroomStale,
    ProviderAccountHeadroomUnsupported,
    assess_provider_account_headroom,
    reserve_observed_provider_headroom,
)
from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


_DEVELOPER_APPS = _response(
    [
        {
            "appId": 12345,
            "appVersions": [
                {
                    "versionId": 67890,
                    "version": "1.0",
                    "applicationKey": "DEVAPP-SECRET-SENTINEL",
                    "ownerManaged": False,
                }
            ],
        }
    ],
    1,
)
_DETAILS = _response(
    {
        "currencyCode": "GBP",
        "localeCode": "en",
        "region": "GBR",
        "timezone": "Europe/London",
    },
    2,
)


def _funds(available: str, *, exposure: str = "-12.34") -> bytes:
    return _response(
        {
            "availableToBetBalance": float(available),
            "exposure": float(exposure),
            "retainedCommission": 0.05,
            "exposureLimit": -5000.00,
        },
        3,
    )


def _install_transport(monkeypatch, responses: list[bytes]) -> list[dict[str, object]]:
    queue = list(responses)
    calls: list[dict[str, object]] = []

    def post(self, url, *, headers, body, timeout_seconds):
        calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not queue:
            raise AssertionError("unexpected provider call")
        return queue.pop(0)

    monkeypatch.setattr(
        betfair_readonly.UrllibBetfairHttpTransport,
        "post",
        post,
    )
    return calls


def _credentials() -> BetfairSessionCredentials:
    return BetfairSessionCredentials(
        "APP-SECRET-SENTINEL",
        "SESSION-SECRET-SENTINEL",
    )


def _acquire_balance(monkeypatch, tmp_path, available: str = "100.10"):
    calls = _install_transport(
        monkeypatch,
        [_DEVELOPER_APPS, _DETAILS, _funds(available)],
    )
    acquired = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials(),
        account_id="acct-1",
    ).acquire(
        frozenset({BookmakerCapability.BALANCE_READ}),
        acquisition_id=f"headroom-{available}",
    )
    assert acquired.snapshot.balance is not None
    return acquired, calls


def _action(
    action_id: str,
    stake: str,
    *,
    account_id: str = "acct-1",
) -> ExecutionAction:
    now = datetime.now(timezone.utc)
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="betfair",
        account_id=account_id,
        event_id=f"event-{action_id}",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side="BACK",
        requested_odds="2.50",
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=(now - timedelta(seconds=2)).isoformat(),
        expires_at=(now + timedelta(minutes=2)).isoformat(),
    )


_HEADROOM_GOAL = EconomicGoalContract(
    goal_id="headroom-test-goal",
    revision=1,
    bankroll_id="headroom-test-bankroll",
    currency="GBP",
)
_HEADROOM_GOAL_SHA256 = provenance_for(_HEADROOM_GOAL).contract_sha256


def _intent_for_action(
    action: ExecutionAction,
    intent_id: str,
    *,
    bankroll_id: str = _HEADROOM_GOAL.bankroll_id,
    currency: str = _HEADROOM_GOAL.currency,
    provider_account_id: str | None = None,
) -> OpportunityIntent:
    odds = Decimal(str(action.requested_odds))
    leg = TicketLeg(
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        locked_odds=odds,
    )
    quote_event = MarketEvent(
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        decimal_odds=odds,
        observed_ts=action.quote_observed_at,
        source_id=action.bookmaker_id,
        sequence=1,
        source_ts=action.quote_observed_at,
        ingest_ts=action.quote_observed_at,
    )
    context = ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(quote_event,),
        provider_accounts=(
            (
                action.bookmaker_id,
                action.account_id if provider_account_id is None else provider_account_id,
            ),
        ),
        bankroll_id=bankroll_id,
        currency=currency,
        proposal_ts=action.quote_observed_at,
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
    return OpportunityIntent(
        intent_id=intent_id,
        opportunity=opportunity,
        evidence=OpportunityEvidence(
            evidence_id=f"headroom-evidence-{intent_id}",
            observed_at=action.quote_observed_at,
            causal_cutoff=action.quote_observed_at,
            reproducibility_sha256="a" * 64,
        ),
        risk_context=context,
        signal_strength=Decimal("0.05"),
        strategy_id=f"headroom-strategy-{intent_id}",
        config_sha256="e" * 64,
    )


def _plan(
    plan_id: str,
    action: ExecutionAction,
    *,
    economic_goal_contract_sha256: str = _HEADROOM_GOAL_SHA256,
    intent_bankroll_id: str = _HEADROOM_GOAL.bankroll_id,
    intent_currency: str = _HEADROOM_GOAL.currency,
    intent_account_id: str | None = None,
) -> tuple[str, BoundSupervisedExecutionPlan, OpportunityIntent, str]:
    intent_id = f"intent-{plan_id}"
    intent = _intent_for_action(
        action,
        intent_id,
        bankroll_id=intent_bankroll_id,
        currency=intent_currency,
        provider_account_id=intent_account_id,
    )
    book = PaperBook("1000")
    graph = PortfolioDependencyGraph.for_inputs(book, (intent,))
    portfolio = PortfolioPlan(
        decision_ts=action.quote_observed_at,
        action=PortfolioAction.STAKE_VECTOR,
        stakes=(Decimal(str(action.requested_stake)),),
        intent_ids=(intent.intent_id,),
        intent_sha256s=(intent.intent_sha256,),
        opportunity_classes=(intent.opportunity_class.value,),
        portfolio_sha256=graph.portfolio_sha256,
        dependency_graph=graph,
        terminal_economics=None,
        economic_goal_contract_sha256=economic_goal_contract_sha256,
        risk_policy_sha256="2" * 64,
        portfolio_truth=EvidenceTruth.EXACT,
        reason=f"headroom test product plan {plan_id}",
    )
    venue = VenueQuote(
        action.bookmaker_id,
        action.account_id,
        intent.opportunity.quotes[0],
        Decimal(str(action.requested_stake)),
    )
    route = plan_equal_split_residual(
        Decimal(str(action.requested_stake)),
        (venue,),
        routing_request_id=f"route-{plan_id}",
        parent_plan_id=portfolio.plan_sha256,
        stake_quantum=Decimal("0.01"),
    )
    constraint = supervised_execution.ExecutionLegConstraint(
        leg_id=route.legs[0].leg_id,
        side=action.side,
        quote_expires_at=action.expires_at,
        max_slippage_fraction=Decimal("0.05"),
    )
    approval = supervised_execution.SupervisedApproval(
        approval_id=f"approval-{plan_id}",
        portfolio_plan_sha256=portfolio.plan_sha256,
        intent_id=intent.intent_id,
        routing_request_id=route.routing_request_id,
        execution_terms_sha256=supervised_execution.supervised_execution_terms_sha256(
            route,
            (constraint,),
        ),
        approved_at=action.quote_observed_at,
        expires_at=action.expires_at,
        evidence_sha256="3" * 64,
    )
    profile = BookmakerCapabilityProfile(
        venue_id=action.bookmaker_id,
        account_id=action.account_id,
        adapter_id="headroom-test-readback",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BET_READBACK,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=action.quote_observed_at,
        source_ref="autosport://headroom-test/profile",
        source_payload_sha256="4" * 64,
    )
    bound = supervised_execution.build_supervised_execution_plan(
        portfolio,
        (intent,),
        route,
        (profile,),
        approval,
        (constraint,),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return plan_id, bound, intent, action.action_id

def _ledger_with_plans(
    tmp_path,
    *plans: tuple[str, BoundSupervisedExecutionPlan, OpportunityIntent, str],
    economic_goal: EconomicGoalContract = _HEADROOM_GOAL,
) -> RealExecutionLedger:
    store = EconomicGoalStore(tmp_path)
    if not store.path.exists():
        store.initialize_owner(economic_goal)
    ledger = RealExecutionLedger(tmp_path / "real-ledger.jsonl")
    logical_ids: dict[str, str] = {}
    logical_action_ids: dict[tuple[str, str], str] = {}
    bounds: list[BoundSupervisedExecutionPlan] = []
    intents: list[OpportunityIntent] = []
    for logical_id, bound, intent, logical_action_id in plans:
        logical_ids[logical_id] = bound.execution_plan.plan_id
        if len(bound.execution_plan.actions) != 1:
            raise AssertionError("headroom test helper expects one canonical action")
        logical_action_ids[(logical_id, logical_action_id)] = (
            bound.execution_plan.actions[0].action_id
        )
        bounds.append(bound)
        intents.append(intent)
        ledger.reserve_plan(bound.execution_plan)
    ledger._test_logical_plan_ids = logical_ids
    ledger._test_logical_action_ids = logical_action_ids
    ledger._test_bound_plans = tuple(bounds)
    ledger._test_intents = tuple(intents)
    return ledger


def _actual_plan_id(ledger: RealExecutionLedger, logical_plan_id: str) -> str:
    return ledger._test_logical_plan_ids[logical_plan_id]


def _actual_action_id(
    ledger: RealExecutionLedger,
    logical_plan_id: str,
    logical_action_id: str,
) -> str:
    return ledger._test_logical_action_ids[(logical_plan_id, logical_action_id)]


def _assess(
    ledger: RealExecutionLedger,
    acquired: AuthoritativeAccountSnapshot,
    *,
    plan_id: str,
    action_id: str,
):
    return assess_provider_account_headroom(
        ledger,
        acquired,
        plan_id=_actual_plan_id(ledger, plan_id),
        action_id=_actual_action_id(ledger, plan_id, action_id),
        bound_plans=ledger._test_bound_plans,
        intents=ledger._test_intents,
    )


def _reserve(
    ledger: RealExecutionLedger,
    acquired: AuthoritativeAccountSnapshot,
    assessment,
    *,
    attempt_id: str,
):
    return reserve_observed_provider_headroom(
        ledger,
        acquired,
        assessment,
        attempt_id=attempt_id,
        bound_plans=ledger._test_bound_plans,
        intents=ledger._test_intents,
    )


def test_liability_plan_enumeration_reads_canonical_ledger_envelopes(tmp_path) -> None:
    first = _action("a1", "10")
    second = _action("a2", "20")
    ledger = _ledger_with_plans(
        tmp_path,
        _plan("p2", second),
        _plan("p1", first),
    )
    snapshot = ledger.verified_snapshot()

    assert headroom_module._ledger_plan_ids(snapshot.payload) == tuple(
        sorted(
            (
                _actual_plan_id(ledger, "p1"),
                _actual_plan_id(ledger, "p2"),
            )
        )
    )


def test_provider_exposure_is_not_double_subtracted(monkeypatch, tmp_path) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100.10")
    action = _action("a1", "95")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))

    assessment = _assess(
        ledger,
        acquired,
        plan_id="p1",
        action_id="a1",
    )

    assert assessment.provider_available_to_bet == Decimal("100.1")
    assert assessment.definitely_unreflected_product_liability == 0
    assert assessment.unknown_reflection_product_liability == 0
    assert assessment.lower_headroom == Decimal("100.1")
    assert assessment.upper_headroom == Decimal("100.1")
    assert assessment.decision is HeadroomDecision.SUFFICIENT_LOWER_BOUND
    assert assessment.provider_atomicity_proven is False
    assert assessment.provider_balance_generation_cas_proven is False
    assert assessment.execution_authority is False
    assert assessment.real_money_readiness is False


def test_same_snapshot_two_writer_race_only_one_reserves(monkeypatch, tmp_path) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    first_action = _action("a1", "80")
    second_action = _action("a2", "80")
    ledger = _ledger_with_plans(
        tmp_path,
        _plan("p1", first_action),
        _plan("p2", second_action),
    )

    first = _assess(
        ledger, acquired, plan_id="p1", action_id="a1"
    )
    second_from_same_generation = _assess(
        ledger, acquired, plan_id="p2", action_id="a2"
    )
    assert first.ledger_snapshot_sha256 == second_from_same_generation.ledger_snapshot_sha256
    assert first.decision is HeadroomDecision.SUFFICIENT_LOWER_BOUND
    assert second_from_same_generation.decision is HeadroomDecision.SUFFICIENT_LOWER_BOUND

    reserved = _reserve(
        ledger,
        acquired,
        first,
        attempt_id="attempt-1",
    )
    assert reserved.product_internal_reservation_proven is False
    assert reserved.provider_atomicity_proven is False
    assert ledger.attempt_state("attempt-1") is AttemptState.RESERVED

    with pytest.raises(
        ProviderAccountHeadroomStale,
        match="execution ledger changed",
    ):
        _reserve(
            ledger,
            acquired,
            second_from_same_generation,
            attempt_id="attempt-2",
        )

    recomputed = _assess(
        ledger, acquired, plan_id="p2", action_id="a2"
    )
    assert recomputed.definitely_unreflected_product_liability == Decimal("80")
    assert recomputed.unknown_reflection_product_liability == 0
    assert recomputed.lower_headroom == Decimal("20")
    assert recomputed.upper_headroom == Decimal("20")
    assert recomputed.decision is HeadroomDecision.INSUFFICIENT_UPPER_BOUND



def test_newer_balance_generation_stales_assessment_before_new_reservation(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("generation-target", "80")
    ledger = _ledger_with_plans(tmp_path, _plan("target", action))
    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="generation-target",
    )
    assert assessment.decision is HeadroomDecision.SUFFICIENT_LOWER_BOUND

    newer, _ = _acquire_balance(monkeypatch, tmp_path, "10")

    with pytest.raises(
        ProviderAccountHeadroomStale,
        match="balance generation changed",
    ):
        _reserve(
            ledger,
            acquired,
            assessment,
            attempt_id="generation-stale-attempt",
        )
    with pytest.raises(KeyError):
        ledger.attempt_state("generation-stale-attempt")

    with pytest.raises(
        ProviderAccountHeadroomStale,
        match="balance generation changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="generation-target",
        )

    recomputed = _assess(
        ledger,
        newer,
        plan_id="target",
        action_id="generation-target",
    )
    assert recomputed.provider_available_to_bet == Decimal("10")
    assert recomputed.decision is HeadroomDecision.INSUFFICIENT_UPPER_BOUND


def test_generation_lock_helper_rebinding_cannot_bypass_new_reservation(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("generation-helper-target", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("target", action))
    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="generation-helper-target",
    )
    hostile_calls = []

    @contextmanager
    def hostile_guard(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        yield

    monkeypatch.setattr(
        headroom_module,
        "_current_balance_generation_lock",
        hostile_guard,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="generation lock authority changed",
    ):
        _reserve(
            ledger,
            acquired,
            assessment,
            attempt_id="generation-helper-attempt",
        )
    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.attempt_state("generation-helper-attempt")


def test_generation_lock_wrapped_kwdefault_rebind_fails_before_execution(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("generation-kwdefault-target", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("target", action))
    wrapped = headroom_module._current_balance_generation_lock.__wrapped__
    original = dict(wrapped.__kwdefaults__ or {})
    hostile_calls = []

    def hostile_lock(_workspace):
        hostile_calls.append("executed")
        raise AssertionError("hostile economic lock executed")

    try:
        wrapped.__kwdefaults__["_economic_lock"] = hostile_lock
        with pytest.raises(
            ProviderAccountHeadroomError,
            match="generation lock authority changed",
        ):
            _assess(
                ledger,
                acquired,
                plan_id="target",
                action_id="generation-kwdefault-target",
            )
    finally:
        wrapped.__kwdefaults__.clear()
        wrapped.__kwdefaults__.update(original)

    assert hostile_calls == []


def test_generation_lock_wrapped_code_rebind_fails_before_execution(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("generation-wrapped-target", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("target", action))
    wrapped = headroom_module._current_balance_generation_lock.__wrapped__
    original_code = wrapped.__code__
    hostile_calls = []

    def forged(
        workspace,
        acquired,
        *,
        _account_authority=None,
        _account_authority_code=None,
        _economic_lock=None,
        _economic_lock_code=None,
    ):
        del (
            workspace,
            acquired,
            _account_authority,
            _account_authority_code,
            _economic_lock,
            _economic_lock_code,
        )
        hostile_calls.append("executed")
        yield

    assert len(forged.__code__.co_freevars) == len(original_code.co_freevars)
    try:
        wrapped.__code__ = forged.__code__
        with pytest.raises(
            ProviderAccountHeadroomError,
            match="generation lock authority changed",
        ):
            _assess(
                ledger,
                acquired,
                plan_id="target",
                action_id="generation-wrapped-target",
            )
    finally:
        wrapped.__code__ = original_code

    assert hostile_calls == []


def test_generation_lock_helper_rebinding_cannot_bypass_assessment(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("generation-assess-helper-target", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("target", action))
    hostile_calls = []

    @contextmanager
    def hostile_guard(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        yield

    monkeypatch.setattr(
        headroom_module,
        "_current_balance_generation_lock",
        hostile_guard,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="generation lock authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="generation-assess-helper-target",
        )
    assert hostile_calls == []


def test_exact_existing_attempt_replay_does_not_require_current_balance_generation(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("replay-target", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("target", action))
    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="replay-target",
    )
    first = _reserve(
        ledger,
        acquired,
        assessment,
        attempt_id="generation-replay-attempt",
    )

    _acquire_balance(monkeypatch, tmp_path, "1")

    replay = _reserve(
        ledger,
        acquired,
        assessment,
        attempt_id="generation-replay-attempt",
    )
    assert replay == first
    assert ledger.attempt_state("generation-replay-attempt") is AttemptState.RESERVED


@pytest.mark.parametrize(
    "dependency_name",
    ("_canonical_account_snapshot_authority", "_canonical_economic_lock"),
)
def test_generation_lock_rejects_transitive_dependency_rebinding(
    monkeypatch,
    dependency_name,
) -> None:
    hostile_calls = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile generation dependency executed")

    monkeypatch.setattr(headroom_module, dependency_name, hostile)

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="generation lock dependency authority changed",
    ):
        with headroom_module._current_balance_generation_lock(
            object(),
            object(),  # type: ignore[arg-type]
        ):
            raise AssertionError("generation body must not execute")

    assert hostile_calls == []


def test_current_generation_guard_alias_rebinding_cannot_admit_stale_balance(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("guard-alias-target", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("target", action))
    hostile_calls = []

    def hostile_guard(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile generation guard executed")

    monkeypatch.setattr(
        headroom_module,
        "hold_current_account_snapshot_acquisition",
        hostile_guard,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="account snapshot headroom authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="guard-alias-target",
        )

    assert hostile_calls == []


def test_current_generation_guard_module_rebinding_fails_before_hostile_code(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("guard-module-target", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("target", action))
    hostile_calls = []

    def hostile_guard(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile module generation guard executed")

    monkeypatch.setattr(
        headroom_module._account_acquisition,
        "hold_current_account_snapshot_acquisition",
        hostile_guard,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="account snapshot headroom authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="guard-module-target",
        )

    assert hostile_calls == []


def test_current_generation_boundary_dispatch_rebinding_fails_closed(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("guard-boundary-target", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("target", action))
    hostile_calls = []

    def hostile_hold(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile boundary generation guard executed")

    boundary_type = headroom_module._ACCOUNT_SNAPSHOT_AUTHORITY_BOUNDARY_TYPE
    assert boundary_type is not None
    monkeypatch.setattr(boundary_type, "hold_current", hostile_hold)

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="account snapshot headroom authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="guard-boundary-target",
        )

    assert hostile_calls == []


@pytest.mark.parametrize(
    "surface",
    ("resolver", "issued_current"),
)
def test_capital_risk_authority_rebinding_cannot_erase_existing_liability(
    monkeypatch,
    tmp_path,
    surface,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    first_action = _action("a1", "80")
    second_action = _action("a2", "30")
    ledger = _ledger_with_plans(
        tmp_path,
        _plan("p1", first_action),
        _plan("p2", second_action),
    )
    first = _assess(
        ledger, acquired, plan_id="p1", action_id="a1"
    )
    _reserve(
        ledger, acquired, first, attempt_id="attempt-1"
    )
    hostile_calls = []

    if surface == "resolver":
        def hostile_resolver(*args, **kwargs):
            hostile_calls.append((args, kwargs))
            raise AssertionError("hostile capital resolver executed")
        monkeypatch.setattr(
            headroom_module,
            "resolve_execution_capital_at_risk",
            hostile_resolver,
        )
    else:
        def hostile_assert(*args, **kwargs):
            hostile_calls.append((args, kwargs))
            raise AssertionError("hostile capital currentness check executed")
        monkeypatch.setattr(
            headroom_module.ExecutionCapitalAtRiskEvidence,
            "assert_issued_current",
            hostile_assert,
        )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="capital-at-risk headroom authority changed",
    ):
        _assess(
            ledger, acquired, plan_id="p2", action_id="a2"
        )

    assert hostile_calls == []


def test_submitted_liability_with_unknown_balance_coverage_forces_wait(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    first_action = _action("a1", "70")
    second_action = _action("a2", "50")
    ledger = _ledger_with_plans(
        tmp_path,
        _plan("p1", first_action),
        _plan("p2", second_action),
    )
    first = _assess(
        ledger, acquired, plan_id="p1", action_id="a1"
    )
    _reserve(
        ledger, acquired, first, attempt_id="attempt-1"
    )
    ledger.mark_submitted("attempt-1")

    second = _assess(
        ledger, acquired, plan_id="p2", action_id="a2"
    )
    assert second.definitely_unreflected_product_liability == 0
    assert second.unknown_reflection_product_liability == Decimal("70")
    assert second.lower_headroom == Decimal("30")
    assert second.upper_headroom == Decimal("100")
    assert second.decision is HeadroomDecision.WAIT_COVERAGE


def test_live_snapshot_object_mutation_revokes_headroom_authority(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "5")
    assert acquired.snapshot.balance is not None
    forged_balance = replace(
        acquired.snapshot.balance,
        available_balance=Decimal("1000000"),
    )
    forged_snapshot = replace(acquired.snapshot, balance=forged_balance)
    object.__setattr__(acquired, "snapshot", forged_snapshot)

    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="lacks live canonical provider-origin authority",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="p1",
            action_id="a1",
        )


def test_account_authority_alias_rebinding_cannot_forge_available_balance(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "5")
    assert acquired.snapshot.balance is not None
    forged_balance = replace(
        acquired.snapshot.balance,
        available_balance=Decimal("1000000"),
    )
    forged = AuthoritativeAccountSnapshot(
        replace(acquired.snapshot, balance=forged_balance),
        acquired.receipt,
    )
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    hostile_calls = []

    def forged_authority(_acquired):
        hostile_calls.append(True)

    monkeypatch.setattr(
        headroom_module,
        "assert_account_snapshot_acquisition_authoritative",
        forged_authority,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="account snapshot headroom authority changed",
    ):
        _assess(
            ledger,
            forged,
            plan_id="p1",
            action_id="a1",
        )

    assert hostile_calls == []


def test_other_provider_account_cannot_donate_headroom(monkeypatch, tmp_path) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "1000")
    foreign = _action("a1", "20", account_id="acct-2")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", foreign))

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="provider/account mismatches live balance acquisition",
    ):
        _assess(
            ledger, acquired, plan_id="p1", action_id="a1"
        )


def test_exact_reconstructed_assessment_can_reserve_after_source_revalidation(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    assessment = _assess(
        ledger, acquired, plan_id="p1", action_id="a1"
    )
    reconstructed = replace(assessment)

    reservation = _reserve(
        ledger,
        acquired,
        reconstructed,
        attempt_id="attempt-1",
    )

    assert reservation.assessment_sha256 == reconstructed.evidence_sha256
    assert reservation.product_internal_reservation_proven is False
    assert ledger.attempt_state("attempt-1") is AttemptState.RESERVED


def test_module_attempt_state_rebinding_cannot_bypass_headroom_gate(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "5")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    assessment = _assess(
        ledger,
        acquired,
        plan_id="p1",
        action_id="a1",
    )
    assert assessment.decision is HeadroomDecision.INSUFFICIENT_UPPER_BOUND
    hostile_calls = []

    def hostile_attempt_state(_ledger, _attempt_id):
        hostile_calls.append(True)
        return AttemptState.RESERVED

    monkeypatch.setattr(
        headroom_module,
        "_ATTEMPT_STATE",
        hostile_attempt_state,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="execution ledger headroom authority changed",
    ):
        _reserve(
            ledger,
            acquired,
            assessment,
            attempt_id="attempt-1",
        )

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.attempt_state("attempt-1")


def test_caller_constructed_internal_reservation_cannot_claim_product_proof() -> None:
    forged = ProductInternalHeadroomReservation(
        assessment_sha256="a" * 64,
        attempt_id="forged-attempt",
        attempt_fingerprint="b" * 64,
        reserved_at="2026-10-03T10:00:00+00:00",
        post_reservation_ledger_sha256="c" * 64,
        post_reservation_event_count=1,
    )

    assert forged.product_internal_reservation_proven is False
    assert forged.provider_atomicity_proven is False
    assert forged.execution_authority is False
    assert forged.real_money_readiness is False


def test_copied_or_mutated_internal_reservation_loses_product_proof(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    assessment = _assess(
        ledger, acquired, plan_id="p1", action_id="a1"
    )
    issued = _reserve(
        ledger, acquired, assessment, attempt_id="attempt-issued"
    )

    assert issued.product_internal_reservation_proven is False
    copied = replace(issued)
    assert copied.product_internal_reservation_proven is False

    object.__setattr__(
        issued,
        "post_reservation_event_count",
        issued.post_reservation_event_count + 1,
    )
    assert issued.product_internal_reservation_proven is False


def test_exact_attempt_retry_is_idempotent_after_reservation(monkeypatch, tmp_path) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    assessment = _assess(
        ledger, acquired, plan_id="p1", action_id="a1"
    )

    first = _reserve(
        ledger, acquired, assessment, attempt_id="attempt-1"
    )
    second = _reserve(
        ledger, acquired, assessment, attempt_id="attempt-1"
    )

    assert second.attempt_fingerprint == first.attempt_fingerprint
    assert second.reserved_at == first.reserved_at
    assert ledger.attempt_state("attempt-1") is AttemptState.RESERVED


def test_instance_shadow_cannot_bypass_snapshot_or_reservation_cas(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    calls = {"snapshot": 0, "begin": 0}

    def fake_snapshot():
        calls["snapshot"] += 1
        raise AssertionError("instance-shadowed verified_snapshot must not run")

    def fake_begin_attempt(**kwargs):
        calls["begin"] += 1
        raise AssertionError("instance-shadowed begin_attempt must not run")

    ledger.verified_snapshot = fake_snapshot
    ledger.begin_attempt = fake_begin_attempt

    assessment = _assess(
        ledger, acquired, plan_id="p1", action_id="a1"
    )
    reserved = _reserve(
        ledger, acquired, assessment, attempt_id="attempt-1"
    )

    assert calls == {"snapshot": 0, "begin": 0}
    assert reserved.product_internal_reservation_proven is False
    assert ledger.attempt_state("attempt-1") is AttemptState.RESERVED



class _SequencedDateTime(datetime):
    values: list[datetime] = []

    @classmethod
    def now(cls, tz=None):
        if not cls.values:
            raise AssertionError("unexpected provider clock read")
        value = cls.values.pop(0)
        if tz is None:
            return cls(
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
            )
        return cls.fromtimestamp(value.timestamp(), tz=tz)


def test_headroom_clock_rebinding_cannot_mint_fresh_balance(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    hostile_calls = []

    def hostile_clock():
        hostile_calls.append(True)
        assert acquired.snapshot.balance is not None
        return datetime.fromisoformat(acquired.snapshot.balance.observed_at)

    monkeypatch.setattr(headroom_module, "_utc_now", hostile_clock)

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="headroom clock authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="p1",
            action_id="a1",
        )

    assert hostile_calls == []


def test_headroom_clock_kwdefault_mutation_cannot_mint_fresh_balance(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    hostile_calls = []

    def hostile_datetime_now(_tz):
        hostile_calls.append(True)
        assert acquired.snapshot.balance is not None
        return datetime.fromisoformat(acquired.snapshot.balance.observed_at)

    kwdefaults = headroom_module._utc_now.__kwdefaults__
    assert isinstance(kwdefaults, dict)
    monkeypatch.setitem(kwdefaults, "_datetime_now", hostile_datetime_now)

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="headroom clock authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="p1",
            action_id="a1",
        )

    assert hostile_calls == []


def test_freshness_is_bound_to_balance_observation_not_later_snapshot_time(
    monkeypatch,
    tmp_path,
) -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=2)
    # developer-app identity evidence, account-details evidence, account-funds
    # evidence, then the final BookmakerAccountSnapshot observation time.
    _SequencedDateTime.values = [old, old, old, now]
    monkeypatch.setattr(betfair_readonly, "datetime", _SequencedDateTime)

    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    assert acquired.snapshot.balance is not None
    assert acquired.snapshot.balance.observed_at != acquired.receipt.acquired_at

    ledger = _ledger_with_plans(
        tmp_path,
        _plan("p1", _action("a1", "10")),
    )
    with pytest.raises(
        ProviderAccountHeadroomStale,
        match="provider balance observation exceeds",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="p1",
            action_id="a1",
        )


def test_unrelated_provider_attempt_does_not_block_betfair_account_scope(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _action("target", "20")
    foreign = ExecutionAction(
        action_id="foreign",
        bookmaker_id="betdaq",
        account_id="betdaq-account",
        event_id="event-foreign",
        market_id="market-foreign",
        selection_id="selection-foreign",
        side="BACK",
        requested_odds="2.00",
        requested_stake="900",
        quote_id="quote-foreign",
        quote_observed_at=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
    )
    ledger = _ledger_with_plans(
        tmp_path,
        _plan("target-plan", target),
        _plan("foreign-plan", foreign),
    )
    ledger.begin_attempt(
        plan_id=_actual_plan_id(ledger, "foreign-plan"),
        action_id=_actual_action_id(ledger, "foreign-plan", "foreign"),
        attempt_id="foreign-attempt",
    )
    ledger.mark_submitted("foreign-attempt")

    assessment = _assess(
        ledger,
        acquired,
        plan_id="target-plan",
        action_id="target",
    )

    assert assessment.definitely_unreflected_product_liability == 0
    assert assessment.unknown_reflection_product_liability == 0
    assert assessment.lower_headroom == Decimal("100")
    assert assessment.upper_headroom == Decimal("100")
    assert assessment.decision is HeadroomDecision.SUFFICIENT_LOWER_BOUND


def test_recomputed_insufficient_assessment_can_resolve_exact_existing_attempt(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "100")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    initial = _assess(
        ledger,
        acquired,
        plan_id="p1",
        action_id="a1",
    )
    first = _reserve(
        ledger,
        acquired,
        initial,
        attempt_id="attempt-1",
    )

    recomputed = _assess(
        ledger,
        acquired,
        plan_id="p1",
        action_id="a1",
    )
    assert recomputed.decision is HeadroomDecision.INSUFFICIENT_UPPER_BOUND
    assert recomputed.definitely_unreflected_product_liability == Decimal("100")

    replay = _reserve(
        ledger,
        acquired,
        recomputed,
        attempt_id="attempt-1",
    )
    assert replay.attempt_fingerprint == first.attempt_fingerprint
    assert replay.reserved_at == first.reserved_at
    assert ledger.attempt_state("attempt-1") is AttemptState.RESERVED


def test_headroom_rejects_missing_denomination_coverage_for_relevant_plan(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    liability = _plan("liability", _action("liability-action", "20"))
    ledger = _ledger_with_plans(tmp_path, target, liability)
    ledger.begin_attempt(
        plan_id=_actual_plan_id(ledger, "liability"),
        action_id=_actual_action_id(ledger, "liability", "liability-action"),
        attempt_id="liability-attempt",
    )

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="lacks exact supervised denomination binding",
    ):
        assess_provider_account_headroom(
            ledger,
            acquired,
            plan_id=_actual_plan_id(ledger, "target"),
            action_id=_actual_action_id(ledger, "target", "target-action"),
            bound_plans=(ledger._test_bound_plans[0],),
            intents=ledger._test_intents,
        )


def test_headroom_rejects_opaque_bound_intent_without_exact_intent_evidence(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="exact OpportunityIntent denomination evidence is required",
    ):
        assess_provider_account_headroom(
            ledger,
            acquired,
            plan_id=_actual_plan_id(ledger, "target"),
            action_id=_actual_action_id(ledger, "target", "target-action"),
            bound_plans=ledger._test_bound_plans,
            intents=(),
        )


def test_headroom_accepts_restart_reconstructed_structural_denomination_binding(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    issued = ledger._test_bound_plans[0]
    reconstructed = replace(issued)

    reconstructed.verify_binding()
    assessment = assess_provider_account_headroom(
        ledger,
        acquired,
        plan_id=_actual_plan_id(ledger, "target"),
        action_id=_actual_action_id(ledger, "target", "target-action"),
        bound_plans=(reconstructed,),
        intents=ledger._test_intents,
    )

    assert assessment.decision is HeadroomDecision.SUFFICIENT_LOWER_BOUND
    assert assessment.execution_authority is False
    assert assessment.real_money_readiness is False


def test_headroom_rejects_intent_currency_mismatching_current_goal(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan(
        "target",
        _action("target-action", "10"),
        intent_currency="USD",
    )
    ledger = _ledger_with_plans(tmp_path, target)

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="intent denomination does not match current durable economic goal",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )


def test_headroom_rejects_provider_currency_mismatching_durable_goal(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    usd_goal = replace(
        _HEADROOM_GOAL,
        goal_id="headroom-usd-goal",
        currency="USD",
    )
    usd_goal_sha256 = provenance_for(usd_goal).contract_sha256
    target = _plan(
        "target",
        _action("target-action", "10"),
        economic_goal_contract_sha256=usd_goal_sha256,
    )
    ledger = _ledger_with_plans(
        tmp_path,
        target,
        economic_goal=usd_goal,
    )

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="provider balance currency mismatches durable economic-goal currency",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )


def test_headroom_rejects_bound_plan_from_noncurrent_goal_revision(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    stale_goal = replace(
        _HEADROOM_GOAL,
        goal_id="stale-goal",
    )
    stale_sha256 = provenance_for(stale_goal).contract_sha256
    target = _plan(
        "target",
        _action("target-action", "10"),
        economic_goal_contract_sha256=stale_sha256,
    )
    ledger = _ledger_with_plans(tmp_path, target)

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="does not match current durable denomination authority",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )


def test_headroom_rejects_mixed_goal_liability_before_arithmetic(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    foreign_goal = replace(
        _HEADROOM_GOAL,
        goal_id="foreign-goal",
    )
    foreign_sha256 = provenance_for(foreign_goal).contract_sha256
    target = _plan("target", _action("target-action", "10"))
    liability = _plan(
        "liability",
        _action("liability-action", "90"),
        economic_goal_contract_sha256=foreign_sha256,
    )
    ledger = _ledger_with_plans(tmp_path, target, liability)
    ledger.begin_attempt(
        plan_id=_actual_plan_id(ledger, "liability"),
        action_id=_actual_action_id(ledger, "liability", "liability-action"),
        attempt_id="liability-attempt",
    )

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="does not match current durable denomination authority",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )


def test_unrelated_account_plan_needs_no_denomination_coverage(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    foreign = _plan(
        "foreign",
        _action("foreign-action", "90", account_id="acct-2"),
    )
    ledger = _ledger_with_plans(tmp_path, target, foreign)
    ledger.begin_attempt(
        plan_id=_actual_plan_id(ledger, "foreign"),
        action_id=_actual_action_id(ledger, "foreign", "foreign-action"),
        attempt_id="foreign-attempt-denomination",
    )

    assessment = assess_provider_account_headroom(
        ledger,
        acquired,
        plan_id=_actual_plan_id(ledger, "target"),
        action_id="target-action",
        bound_plans=(ledger._test_bound_plans[0],),
        intents=(ledger._test_intents[0],),
    )

    assert assessment.decision is HeadroomDecision.SUFFICIENT_LOWER_BOUND
    assert assessment.definitely_unreflected_product_liability == Decimal("0")
    assert assessment.unknown_reflection_product_liability == Decimal("0")


def test_assessment_binds_current_goal_and_exact_denomination_coverage(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    liability = _plan("liability", _action("liability-action", "20"))
    ledger = _ledger_with_plans(tmp_path, target, liability)
    ledger.begin_attempt(
        plan_id=_actual_plan_id(ledger, "liability"),
        action_id=_actual_action_id(ledger, "liability", "liability-action"),
        attempt_id="liability-attempt",
    )

    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="target-action",
    )

    assert assessment.currency == "GBP"
    assert assessment.economic_goal_contract_sha256 == _HEADROOM_GOAL_SHA256
    assert len(assessment.denomination_authority_sha256) == 64
    assert assessment.definitely_unreflected_product_liability == Decimal("20")
    assert assessment.upper_headroom == Decimal("80")


def test_new_reservation_rejects_missing_bound_plan_coverage_after_assessment(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="target-action",
    )

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="exact bound supervised execution plans are required",
    ):
        reserve_observed_provider_headroom(
            ledger,
            acquired,
            assessment,
            attempt_id="new-attempt",
            bound_plans=(),
        )


def test_goal_revision_drift_revokes_new_headroom_reservation(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="target-action",
    )

    successor = replace(_HEADROOM_GOAL, revision=2)
    EconomicGoalStore(tmp_path).persist_automatic_successor(successor)

    with pytest.raises(
        ProviderAccountHeadroomStale,
        match="economic-goal denomination authority changed",
    ):
        _reserve(
            ledger,
            acquired,
            assessment,
            attempt_id="new-attempt",
        )


def test_existing_attempt_replay_does_not_require_new_denomination_authority(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="target-action",
    )
    first = _reserve(
        ledger,
        acquired,
        assessment,
        attempt_id="replay-attempt",
    )

    replay = reserve_observed_provider_headroom(
        ledger,
        acquired,
        assessment,
        attempt_id="replay-attempt",
        bound_plans=(),
    )

    assert replay.attempt_id == first.attempt_id
    assert replay.attempt_fingerprint == first.attempt_fingerprint
    assert replay.post_reservation_ledger_sha256 == first.post_reservation_ledger_sha256


def test_missing_durable_goal_store_fails_closed_before_headroom_arithmetic(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    EconomicGoalStore(tmp_path).path.unlink()

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="durable economic-goal denomination authority is unavailable",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )


def test_target_plan_without_bound_denomination_coverage_fails_closed(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    foreign = _plan(
        "foreign",
        _action("foreign-action", "20", account_id="acct-2"),
    )
    ledger = _ledger_with_plans(tmp_path, target, foreign)

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="relevant execution plan lacks exact supervised denomination binding",
    ):
        assess_provider_account_headroom(
            ledger,
            acquired,
            plan_id=_actual_plan_id(ledger, "target"),
            action_id=_actual_action_id(ledger, "target", "target-action"),
            bound_plans=(ledger._test_bound_plans[1],),
            intents=ledger._test_intents,
        )


def test_duplicate_bound_plan_denomination_identity_is_rejected(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    bound = ledger._test_bound_plans[0]

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="duplicated execution plan identity",
    ):
        assess_provider_account_headroom(
            ledger,
            acquired,
            plan_id=_actual_plan_id(ledger, "target"),
            action_id=_actual_action_id(ledger, "target", "target-action"),
            bound_plans=(bound, bound),
            intents=ledger._test_intents,
        )


def test_mutated_intent_currency_revokes_bound_denomination_identity(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    intent = ledger._test_intents[0]

    object.__setattr__(intent.risk_context, "currency", "USD")

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="lacks exact OpportunityIntent denomination evidence",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )


def test_intent_hash_payload_rebinding_cannot_forge_denomination(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan(
        "target",
        _action("target-action", "10"),
        intent_currency="USD",
    )
    ledger = _ledger_with_plans(tmp_path, target)
    intent = ledger._test_intents[0]
    original_sha256 = intent.intent_sha256
    object.__setattr__(intent.risk_context, "currency", "GBP")
    hostile_calls: list[object] = []

    def hostile_payload(payload):
        hostile_calls.append(payload)
        return original_sha256

    monkeypatch.setattr(
        headroom_module._portfolio_plan,
        "_sha256_payload",
        hostile_payload,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical opportunity-intent denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_risk_candidate_dispatch_rebinding_cannot_forge_denomination(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan(
        "target",
        _action("target-action", "10"),
        intent_currency="USD",
    )
    ledger = _ledger_with_plans(tmp_path, target)
    intent = ledger._test_intents[0]
    original_candidate_sha256 = intent.candidate_sha256
    object.__setattr__(intent.risk_context, "currency", "GBP")
    hostile_calls: list[object] = []

    def hostile_candidate(_context):
        hostile_calls.append(True)
        return original_candidate_sha256

    monkeypatch.setattr(
        headroom_module._risk.PaperRiskPolicy,
        "risk_of_ruin_candidate_sha256",
        staticmethod(hostile_candidate),
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical opportunity-intent denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_economic_goal_store_alias_rebinding_cannot_forge_denomination(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    class HostileStore:
        def __init__(self, workspace) -> None:
            hostile_calls.append(workspace)

        def load(self):
            raise AssertionError("hostile economic-goal store executed")

    monkeypatch.setattr(headroom_module, "EconomicGoalStore", HostileStore)

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_economic_goal_provenance_alias_rebinding_cannot_forge_denomination(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_provenance(goal):
        hostile_calls.append(goal)
        raise AssertionError("hostile economic-goal provenance executed")

    monkeypatch.setattr(headroom_module, "provenance_for", hostile_provenance)

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_bound_plan_verify_rebinding_cannot_forge_denomination(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_verify(self) -> None:
        hostile_calls.append(self)

    monkeypatch.setattr(
        BoundSupervisedExecutionPlan,
        "verify_binding",
        hostile_verify,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_workspace_lock_alias_rebinding_cannot_bypass_goal_revision_fence(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    class HostileLock:
        def __init__(self, workspace) -> None:
            hostile_calls.append(workspace)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            return None

    monkeypatch.setattr(headroom_module, "WorkspaceEconomicLock", HostileLock)

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_ledger_path_rebinding_cannot_redirect_economic_goal_workspace(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    foreign = tmp_path / "foreign-workspace"
    foreign.mkdir()
    EconomicGoalStore(foreign).initialize_owner(_HEADROOM_GOAL)
    ledger.path = foreign / "real-ledger.jsonl"

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="path no longer matches canonical workspace authority",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )


def test_ledger_workspace_authority_dispatch_rebinding_cannot_redirect_goal(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_authority(self):
        hostile_calls.append(self)
        raise AssertionError("hostile ledger workspace authority executed")

    monkeypatch.setattr(
        RealExecutionLedger,
        "_canonical_monotonic_authority",
        hostile_authority,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical execution-ledger workspace authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []

def test_economic_goal_store_filename_rebinding_cannot_redirect_denomination(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)

    monkeypatch.setattr(EconomicGoalStore, "FILE_NAME", "alternate-goal.json")

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )


def test_economic_goal_store_path_factory_rebinding_cannot_redirect_denomination(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    foreign_workspace = tmp_path / "foreign-economic-goal"
    foreign_workspace.mkdir()

    monkeypatch.setattr(
        headroom_module._economic_goal_store,
        "Path",
        lambda _workspace: foreign_workspace,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical economic-goal store path authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

def test_workspace_lock_acquire_rebinding_cannot_bypass_goal_revision_fence(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_acquire(self) -> None:
        hostile_calls.append(self)

    monkeypatch.setattr(
        headroom_module.WorkspaceEconomicLock,
        "acquire",
        hostile_acquire,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_workspace_lock_init_rebinding_cannot_redirect_goal_fence(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_init(self, workspace) -> None:
        hostile_calls.append(workspace)

    monkeypatch.setattr(
        headroom_module.WorkspaceEconomicLock,
        "__init__",
        hostile_init,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_workspace_lock_path_factory_rebinding_cannot_redirect_goal_fence(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    foreign_workspace = tmp_path / "foreign-lock-workspace"
    foreign_workspace.mkdir()

    monkeypatch.setattr(
        headroom_module._workspace_lock,
        "Path",
        lambda _workspace: foreign_workspace,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

def test_economic_goal_from_json_rebinding_cannot_restore_stale_owner_goal(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_from_json(text):
        hostile_calls.append(text)
        return _HEADROOM_GOAL

    monkeypatch.setattr(
        headroom_module._economic_goal_store,
        "economic_goal_from_json",
        hostile_from_json,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_economic_goal_strict_json_rebinding_cannot_restore_stale_owner_goal(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_strict_json(text):
        hostile_calls.append(text)
        raise AssertionError("hostile economic-goal parser executed")

    monkeypatch.setattr(
        headroom_module._economic_goal_store,
        "strict_json_loads",
        hostile_strict_json,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_economic_goal_from_payload_rebinding_cannot_restore_stale_owner_goal(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_from_payload(payload):
        hostile_calls.append(payload)
        return _HEADROOM_GOAL

    monkeypatch.setattr(
        headroom_module._economic_goal_store,
        "economic_goal_from_payload",
        hostile_from_payload,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []

def test_economic_goal_contract_hash_rebinding_cannot_restore_stale_authority(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_contract_sha(goal):
        hostile_calls.append(goal)
        return _HEADROOM_GOAL_SHA256

    monkeypatch.setattr(
        headroom_module._economic_goal_provenance,
        "contract_sha256",
        hostile_contract_sha,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_economic_goal_canonical_json_rebinding_cannot_forge_provenance(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_json(payload):
        hostile_calls.append(payload)
        return b"{}"

    monkeypatch.setattr(
        headroom_module._economic_goal_provenance,
        "_canonical_json",
        hostile_json,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_economic_goal_provenance_payload_rebinding_cannot_forge_goal_hash(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_payload(goal):
        hostile_calls.append(goal)
        return {"schema": "hostile"}

    monkeypatch.setattr(
        headroom_module._economic_goal_provenance,
        "economic_goal_to_payload",
        hostile_payload,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []

def test_bound_binding_sha_rebinding_cannot_make_goal_binding_tautological(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_binding(execution_plan, *args):
        hostile_calls.append((execution_plan, args))
        return execution_plan.plan_id.removeprefix("supervised-v2-")

    monkeypatch.setattr(
        headroom_module._supervised_execution,
        "_bound_binding_sha256",
        hostile_binding,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_bound_plan_witness_rebinding_cannot_retain_stale_issuance(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_witness(bound):
        hostile_calls.append(bound)
        return bound.execution_plan.plan_id.removeprefix("supervised-v2-")

    monkeypatch.setattr(
        headroom_module._supervised_execution,
        "_bound_plan_witness",
        hostile_witness,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_bound_plan_digest_rebinding_cannot_forge_denomination_identity(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_digest(payload):
        hostile_calls.append(payload)
        return "0" * 64

    monkeypatch.setattr(
        headroom_module._supervised_execution,
        "_digest",
        hostile_digest,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []

def test_economic_goal_store_new_cannot_return_hostile_reader(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    class HostileStore:
        workspace = tmp_path
        path = tmp_path / EconomicGoalStore.FILE_NAME

    def hostile_new(cls, workspace):
        hostile_calls.append(workspace)
        return HostileStore()

    monkeypatch.setattr(
        EconomicGoalStore,
        "__new__",
        staticmethod(hostile_new),
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical economic-goal store path authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == [tmp_path]


def test_workspace_lock_new_cannot_return_hostile_context(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_entries: list[object] = []

    class HostileLock:
        workspace = tmp_path
        path = tmp_path / WorkspaceEconomicLock.FILE_NAME

        def __enter__(self):
            hostile_entries.append(self)
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

    def hostile_new(cls, workspace):
        return HostileLock()

    monkeypatch.setattr(
        WorkspaceEconomicLock,
        "__new__",
        staticmethod(hostile_new),
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical economic lock construction authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_entries == []

def test_target_intent_provider_account_must_cover_execution_account(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan(
        "target",
        _action("target-action", "10", account_id="acct-1"),
        intent_account_id="acct-2",
    )
    ledger = _ledger_with_plans(tmp_path, target)

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="intent provider-account scope does not cover execution account",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )


def test_liability_intent_provider_account_must_cover_execution_account(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    liability = _plan(
        "liability",
        _action("liability-action", "20", account_id="acct-1"),
        intent_account_id="acct-2",
    )
    ledger = _ledger_with_plans(tmp_path, target, liability)
    ledger.begin_attempt(
        plan_id=_actual_plan_id(ledger, "liability"),
        action_id=_actual_action_id(ledger, "liability", "liability-action"),
        attempt_id="liability-account-scope-attempt",
    )

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="intent provider-account scope does not cover execution account",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )


def test_denomination_authority_binds_provider_account_identity(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)

    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="target-action",
    )

    assert assessment.provider_id == "betfair"
    assert assessment.account_id == "acct-1"
    assert len(assessment.denomination_authority_sha256) == 64

def test_headroom_issuance_mutators_and_registries_are_not_module_globals() -> None:
    assert not hasattr(headroom_module, "_issue_assessment")
    assert not hasattr(headroom_module, "_issue_reservation")
    assert not hasattr(headroom_module, "_ISSUED")
    assert not hasattr(headroom_module, "_ISSUED_LOCK")
    assert not hasattr(headroom_module, "_assert_issued")
    assert not hasattr(headroom_module, "_reservation_is_issued")
    assert not hasattr(headroom_module, "_RESERVATION_ISSUED")
    assert not hasattr(headroom_module, "_RESERVATION_ISSUED_LOCK")


def test_headroom_authority_has_no_positive_process_local_issuance_registry() -> None:
    assert not hasattr(headroom_module, "_issue_assessment")
    assert not hasattr(headroom_module, "_issue_reservation")
    assert not hasattr(headroom_module, "_assert_issued")
    assert not hasattr(headroom_module, "_reservation_is_issued")
    assert (
        ProductInternalHeadroomReservation(
            assessment_sha256="a" * 64,
            attempt_id="no-process-proof",
            attempt_fingerprint="b" * 64,
            reserved_at="2026-10-03T10:00:00+00:00",
            post_reservation_ledger_sha256="c" * 64,
            post_reservation_event_count=1,
        ).product_internal_reservation_proven
        is False
    )


def test_digest_valid_but_source_false_assessment_cannot_mint_reservation(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="target-action",
    )
    forged = replace(
        assessment,
        provider_available_to_bet=Decimal("1000"),
        upper_headroom=Decimal("1000"),
        lower_headroom=Decimal("1000"),
        evidence_sha256="0" * 64,
    )
    object.__setattr__(
        forged,
        "evidence_sha256",
        headroom_module._assessment_digest(forged),
    )

    with pytest.raises(
        ProviderAccountHeadroomStale,
        match="does not match current canonical source truth",
    ):
        _reserve(
            ledger,
            acquired,
            forged,
            attempt_id="source-false-assessment",
        )

    with pytest.raises(KeyError):
        ledger.attempt_state("source-false-assessment")


def test_reconstructed_assessment_uses_source_truth_not_hidden_mint_state(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    issued = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="target-action",
    )
    reconstructed = replace(issued)

    assert not hasattr(headroom_module, "_issue_assessment")
    reservation = _reserve(
        ledger,
        acquired,
        reconstructed,
        attempt_id="reconstructed-source-truth",
    )
    assert reservation.assessment_sha256 == reconstructed.evidence_sha256
    assert ledger.attempt_state("reconstructed-source-truth") is AttemptState.RESERVED


def test_reconstructed_reservation_loses_product_internal_proof(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="target-action",
    )
    reservation = _reserve(
        ledger,
        acquired,
        assessment,
        attempt_id="issued-reservation-attempt",
    )
    reconstructed = replace(reservation)

    assert reservation.product_internal_reservation_proven is False
    assert reconstructed.product_internal_reservation_proven is False
    assert not hasattr(headroom_module, "_issue_reservation")

def test_obsolete_issuance_assertion_injection_cannot_change_reservation_authority(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    reconstructed = replace(
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )
    )
    hostile_calls: list[object] = []

    def hostile_assert(value):
        hostile_calls.append(value)
        raise AssertionError("obsolete process-local issuance assertion executed")

    monkeypatch.setattr(
        headroom_module,
        "_assert_issued",
        hostile_assert,
        raising=False,
    )

    reservation = _reserve(
        ledger,
        acquired,
        reconstructed,
        attempt_id="obsolete-assertion-inert",
    )

    assert hostile_calls == []
    assert reservation.product_internal_reservation_proven is False
    assert ledger.attempt_state("obsolete-assertion-inert") is AttemptState.RESERVED


def test_headroom_digest_rebinding_fails_before_assessment(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_digest(payload):
        hostile_calls.append(payload)
        return "0" * 64

    monkeypatch.setattr(
        headroom_module,
        "_canonical_digest",
        hostile_digest,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="digest authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_headroom_json_dispatch_rebinding_fails_before_assessment(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_json(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        return "{}"

    monkeypatch.setattr(
        headroom_module.json,
        "dumps",
        hostile_json,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="digest authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_issued_assessment_exact_state_cannot_be_hidden_by_digest_rebinding(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="target-action",
    )
    original_evidence_sha256 = assessment.evidence_sha256
    object.__setattr__(
        assessment,
        "provider_available_to_bet",
        Decimal("999"),
    )

    monkeypatch.setattr(
        headroom_module,
        "_assessment_digest",
        lambda _value: original_evidence_sha256,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical headroom assessment digest authority changed",
    ):
        _reserve(
            ledger,
            acquired,
            assessment,
            attempt_id="mutated-issued-assessment",
        )
    with pytest.raises(KeyError):
        ledger.attempt_state("mutated-issued-assessment")

def test_economic_goal_path_read_rebinding_cannot_supply_forged_goal(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_read_text(self, *args, **kwargs):
        hostile_calls.append((self, args, kwargs))
        raise AssertionError("hostile economic-goal file read executed")

    monkeypatch.setattr(
        headroom_module._economic_goal_store.Path,
        "read_text",
        hostile_read_text,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []

def test_strict_json_module_loads_rebinding_cannot_forge_owner_goal(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_loads(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(
        headroom_module._json_integrity.json,
        "loads",
        hostile_loads,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_strict_json_validator_rebinding_cannot_forge_owner_goal(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_validate(value):
        hostile_calls.append(value)

    monkeypatch.setattr(
        headroom_module._json_integrity,
        "_validate_strict_json_value",
        hostile_validate,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []

def test_economic_goal_provenance_json_rebinding_cannot_forge_contract_hash(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_dumps(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        return "{}"

    class HostileJson:
        dumps = staticmethod(hostile_dumps)

    monkeypatch.setattr(
        headroom_module._economic_goal_provenance,
        "json",
        HostileJson,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_economic_goal_provenance_sha256_rebinding_cannot_forge_contract_hash(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_sha256(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile provenance sha256 executed")

    class HostileHashlib:
        sha256 = staticmethod(hostile_sha256)

    monkeypatch.setattr(
        headroom_module._economic_goal_provenance,
        "hashlib",
        HostileHashlib,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []

def test_workspace_lock_low_level_lock_rebinding_cannot_remove_fence(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_lock_handle(handle):
        hostile_calls.append(handle)

    monkeypatch.setattr(
        WorkspaceEconomicLock,
        "_lock_handle",
        staticmethod(hostile_lock_handle),
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_workspace_lock_open_handle_rebinding_cannot_redirect_fence(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_open(self):
        hostile_calls.append(self)
        raise AssertionError("hostile lock open executed")

    monkeypatch.setattr(
        WorkspaceEconomicLock,
        "_open_lock_handle",
        hostile_open,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_workspace_lock_verification_descriptor_rebinding_cannot_bypass_fence(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_open_read_only(path):
        hostile_calls.append(path)
        raise AssertionError("hostile verification descriptor executed")

    monkeypatch.setattr(
        headroom_module._workspace_lock,
        "_open_read_only_descriptor",
        hostile_open_read_only,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []

def test_public_reserve_rejects_caller_supplied_issuance_assertion(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    issued = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="target-action",
    )
    reconstructed = replace(issued)
    hostile_calls: list[object] = []

    def hostile_assert(value):
        hostile_calls.append(value)

    with pytest.raises(TypeError, match="_issued_assertion"):
        reserve_observed_provider_headroom(
            ledger,
            acquired,
            reconstructed,
            attempt_id="caller-assertion-bypass",
            bound_plans=ledger._test_bound_plans,
            intents=ledger._test_intents,
            _issued_assertion=hostile_assert,
        )

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.attempt_state("caller-assertion-bypass")

def test_capital_attempt_risk_rebinding_cannot_erase_account_liability(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    liability = _plan("liability", _action("liability-action", "80"))
    ledger = _ledger_with_plans(tmp_path, target, liability)
    ledger.begin_attempt(
        plan_id=_actual_plan_id(ledger, "liability"),
        action_id=_actual_action_id(ledger, "liability", "liability-action"),
        attempt_id="capital-helper-liability",
    )
    hostile_calls: list[object] = []

    def hostile_attempt_risk(value):
        hostile_calls.append(value)
        raise AssertionError("hostile capital-at-risk helper executed")

    monkeypatch.setattr(
        headroom_module._capital_risk,
        "_attempt_risk",
        hostile_attempt_risk,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical capital-at-risk headroom authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_capital_verified_view_alias_rebinding_cannot_erase_liability(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_view(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile capital verified view executed")

    monkeypatch.setattr(
        headroom_module._capital_risk,
        "_VERIFIED_EXECUTION_VIEW",
        hostile_view,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical capital-at-risk headroom authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_capital_evidence_digest_rebinding_cannot_mint_zero_liability(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_digest(value):
        hostile_calls.append(value)
        return "0" * 64

    monkeypatch.setattr(
        headroom_module._capital_risk,
        "_evidence_digest",
        hostile_digest,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical capital-at-risk headroom authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []

def test_capital_attempt_type_rebinding_cannot_forge_zero_liability(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    class HostileAttemptCapital:
        def __init__(self, *args, **kwargs):
            hostile_calls.append((args, kwargs))

    monkeypatch.setattr(
        headroom_module._capital_risk,
        "AttemptCapitalAtRisk",
        HostileAttemptCapital,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical capital-at-risk headroom authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_capital_attempt_state_rebinding_cannot_reclassify_reserved_liability(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)

    class HostileAttemptState:
        RESERVED = object()
        SUBMITTED = object()
        UNKNOWN = object()
        ACCEPTED = object()
        PARTIAL = object()
        REJECTED = object()
        RECONCILED_NOT_FOUND = object()

    monkeypatch.setattr(
        headroom_module._capital_risk,
        "AttemptState",
        HostileAttemptState,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical capital-at-risk headroom authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

def test_account_snapshot_boundary_assert_rebinding_cannot_forge_live_origin(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    boundary = headroom_module._ACCOUNT_SNAPSHOT_AUTHORITY_BOUNDARY
    hostile_calls: list[object] = []

    def hostile_assert(self, value):
        hostile_calls.append((self, value))

    monkeypatch.setattr(
        type(boundary),
        "assert_live",
        hostile_assert,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical account snapshot headroom authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_account_snapshot_fingerprint_rebinding_cannot_preserve_mutated_balance(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    boundary = headroom_module._ACCOUNT_SNAPSHOT_AUTHORITY_BOUNDARY
    hostile_calls: list[object] = []

    def hostile_fingerprint(value):
        hostile_calls.append(value)
        return acquired.receipt.snapshot_sha256

    monkeypatch.setattr(
        type(boundary),
        "_fingerprint",
        staticmethod(hostile_fingerprint),
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical account snapshot headroom authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_account_snapshot_payload_rebinding_cannot_hide_balance_mutation(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_payload(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(
        headroom_module._account_acquisition,
        "_snapshot_payload",
        hostile_payload,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical account snapshot headroom authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []

def test_supervised_binding_json_module_rebinding_cannot_forge_plan_identity(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_dumps(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        return "{}"

    class HostileJson:
        dumps = staticmethod(hostile_dumps)

    monkeypatch.setattr(
        headroom_module._supervised_execution,
        "json",
        HostileJson,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical monetary denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_portfolio_intent_hash_module_rebinding_cannot_forge_denomination(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_sha256(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile portfolio sha256 executed")

    class HostileHashlib:
        sha256 = staticmethod(hostile_sha256)

    monkeypatch.setattr(
        headroom_module._portfolio_plan,
        "hashlib",
        HostileHashlib,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical opportunity-intent denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []


def test_risk_candidate_hash_module_rebinding_cannot_forge_account_scope(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)
    hostile_calls: list[object] = []

    def hostile_dumps(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        return "{}"

    class HostileJson:
        dumps = staticmethod(hostile_dumps)

    monkeypatch.setattr(
        headroom_module._risk,
        "json",
        HostileJson,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="canonical opportunity-intent denomination authority changed",
    ):
        _assess(
            ledger,
            acquired,
            plan_id="target",
            action_id="target-action",
        )

    assert hostile_calls == []

def test_account_bound_headroom_assessment_uses_schema_v3(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _plan("target", _action("target-action", "10"))
    ledger = _ledger_with_plans(tmp_path, target)

    assessment = _assess(
        ledger,
        acquired,
        plan_id="target",
        action_id="target-action",
    )

    assert assessment.schema_version == 3
    with pytest.raises(
        ProviderAccountHeadroomError,
        match="unsupported headroom assessment schema",
    ):
        replace(assessment, schema_version=2)

