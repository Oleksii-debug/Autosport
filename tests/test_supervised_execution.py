from __future__ import annotations

import gc
import json
import tempfile
import weakref
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

import autosport.supervised_execution as supervised_execution
import autosport.supervised_provider_evidence as provider_evidence
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
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
    BoundSupervisedExecutionPlan,
    ExecutionLegConstraint,
    ProviderNotFoundReadback,
    ProviderReadback,
    ReadbackOutcome,
    SupervisedApproval,
    SupervisedExecutionError,
    assert_bound_supervised_execution_plan_authoritative,
    begin_supervised_attempt,
    build_supervised_execution_plan,
    reconcile_account_snapshot,
    reconcile_provider_not_found,
    reconcile_provider_readback,
    reserve_supervised_plan,
    revoke_supervised_approval,
    supervised_execution_terms_sha256,
    verify_betfair_provider_state,
)
from autosport.supervised_provider_evidence import (
    ProviderEvidenceError,
    VerifiedProviderAbsenceEvidence,
    VerifiedProviderEffectEvidence,
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


@pytest.fixture(autouse=True)
def _fixed_trusted_execution_clock(monkeypatch) -> None:
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: RESERVED_AT,
    )


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


def _bound(*, created_at: str = CREATED_AT):
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
        created_at=created_at,
    )
    return bound, approval, portfolio, intent


def _ledger_with_unknown(path: Path):
    bound, approval, portfolio, intent = _bound()
    ledger = RealExecutionLedger(path)
    reserve_supervised_plan(ledger, bound, approval)
    action = bound.execution_plan.actions[0]
    begin_supervised_attempt(
        ledger,
        bound,
        approval,
        action_id=action.action_id,
        attempt_id="attempt-1",
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
    assert execution.plan_id.startswith("supervised-v2-")
    assert execution.bookmaker_profile_version.startswith("profile-set-v1-")
    assert action.bookmaker_id == "betfair"
    assert action.account_id == "acct-1"
    assert action.event_id == "event-1"
    assert action.market_id == "1.23456789"
    assert action.selection_id == "42"
    assert action.side == "BACK"
    assert action.requested_stake == portfolio.stakes[0]
    assert action.expires_at == QUOTE_EXPIRES_AT
    assert len(action.quote_id) == 64


def test_builder_issues_exact_bound_plan_product_authority() -> None:
    bound, _, _, _ = _bound()

    assert_bound_supervised_execution_plan_authoritative(bound)


def test_product_issuance_registry_does_not_retain_dead_plans() -> None:
    bound, _, _, _ = _bound()
    reference = weakref.ref(bound)
    assert reference() is bound

    del bound
    gc.collect()

    assert reference() is None


def test_reconstructed_bound_plan_cannot_mint_product_authority() -> None:
    bound, _, _, _ = _bound()
    reconstructed = BoundSupervisedExecutionPlan(
        execution_plan=bound.execution_plan,
        portfolio_plan_sha256=bound.portfolio_plan_sha256,
        economic_goal_contract_sha256=bound.economic_goal_contract_sha256,
        intent_id=bound.intent_id,
        intent_sha256=bound.intent_sha256,
        approval_fingerprint=bound.approval_fingerprint,
        profile_bindings=bound.profile_bindings,
        constraints=bound.constraints,
    )

    reconstructed.verify_binding()
    with pytest.raises(
        SupervisedExecutionError,
        match="not current canonical product issuance",
    ):
        assert_bound_supervised_execution_plan_authoritative(reconstructed)


def test_copied_bound_plan_cannot_transfer_product_authority() -> None:
    bound, _, _, _ = _bound()
    copied = replace(bound)

    copied.verify_binding()
    with pytest.raises(
        SupervisedExecutionError,
        match="not current canonical product issuance",
    ):
        assert_bound_supervised_execution_plan_authoritative(copied)


def test_mutated_issued_bound_plan_revokes_product_authority() -> None:
    bound, _, _, _ = _bound()
    assert_bound_supervised_execution_plan_authoritative(bound)

    object.__setattr__(bound, "intent_id", "mutated-intent")

    with pytest.raises(
        SupervisedExecutionError,
        match="not current canonical product issuance",
    ):
        assert_bound_supervised_execution_plan_authoritative(bound)


def test_malformed_post_issuance_mutation_fails_closed_as_supervised_error() -> None:
    bound, _, _, _ = _bound()
    assert_bound_supervised_execution_plan_authoritative(bound)

    object.__setattr__(bound, "constraints", (object(),))

    with pytest.raises(
        SupervisedExecutionError,
        match="not current canonical product issuance",
    ):
        assert_bound_supervised_execution_plan_authoritative(bound)


def test_reservation_rejects_structurally_valid_but_unissued_bound_plan(tmp_path) -> None:
    bound, approval, _, _ = _bound()
    reconstructed = BoundSupervisedExecutionPlan(
        execution_plan=bound.execution_plan,
        portfolio_plan_sha256=bound.portfolio_plan_sha256,
        economic_goal_contract_sha256=bound.economic_goal_contract_sha256,
        intent_id=bound.intent_id,
        intent_sha256=bound.intent_sha256,
        approval_fingerprint=bound.approval_fingerprint,
        profile_bindings=bound.profile_bindings,
        constraints=bound.constraints,
    )
    ledger = RealExecutionLedger(tmp_path / "unissued-bound-ledger.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="not current canonical product issuance",
    ):
        reserve_supervised_plan(ledger, reconstructed, approval)

    with pytest.raises(KeyError):
        ledger.saga(reconstructed.execution_plan.plan_id)


def test_product_issuer_is_not_importable_from_module_namespace() -> None:
    assert not hasattr(
        supervised_execution,
        "_issue_bound_supervised_execution_plan",
    )
    assert not hasattr(
        supervised_execution,
        "_install_bound_supervised_execution_plan_authority",
    )


def test_builder_alias_rebinding_revokes_internal_plan_authority(
    monkeypatch,
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    hostile_calls: list[object] = []

    def hostile_build(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        return bound

    monkeypatch.setattr(
        supervised_execution,
        "build_supervised_execution_plan",
        hostile_build,
    )
    ledger = RealExecutionLedger(tmp_path / "builder-rebound-ledger.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical bound supervised execution plan authority changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


def test_assertion_alias_rebinding_cannot_admit_unissued_plan(
    monkeypatch,
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    reconstructed = replace(bound)
    hostile_calls: list[object] = []

    def hostile_assert(value):
        hostile_calls.append(value)

    monkeypatch.setattr(
        supervised_execution,
        "assert_bound_supervised_execution_plan_authoritative",
        hostile_assert,
    )
    ledger = RealExecutionLedger(tmp_path / "assert-rebound-ledger.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical bound supervised execution plan authority changed",
    ):
        reserve_supervised_plan(ledger, reconstructed, approval)

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.saga(reconstructed.execution_plan.plan_id)


def test_product_issuance_closures_do_not_expose_mutable_registry() -> None:
    for function in (
        supervised_execution.build_supervised_execution_plan,
        supervised_execution.assert_bound_supervised_execution_plan_authoritative,
    ):
        cells = tuple(function.__closure__ or ())
        assert not any(isinstance(cell.cell_contents, dict) for cell in cells)


def test_product_issuance_token_cannot_transfer_to_reconstructed_plan() -> None:
    bound, _, _, _ = _bound()
    reconstructed = replace(bound)
    token = object.__getattribute__(bound, "_product_issuance_token")

    object.__setattr__(reconstructed, "_product_issuance_token", token)

    with pytest.raises(
        SupervisedExecutionError,
        match="not current canonical product issuance",
    ):
        assert_bound_supervised_execution_plan_authoritative(reconstructed)


def test_arbitrary_product_issuance_token_cannot_mint_authority() -> None:
    bound, _, _, _ = _bound()
    reconstructed = replace(bound)

    object.__setattr__(reconstructed, "_product_issuance_token", object())

    with pytest.raises(
        SupervisedExecutionError,
        match="not current canonical product issuance",
    ):
        assert_bound_supervised_execution_plan_authoritative(reconstructed)


def test_raw_ledger_plan_reservation_cannot_mint_supervised_issuance(
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    reconstructed = replace(bound)
    ledger = RealExecutionLedger(tmp_path / "raw-reserve-ledger.jsonl")

    ledger.reserve_plan(reconstructed.execution_plan)

    with pytest.raises(
        SupervisedExecutionError,
        match="not current canonical product issuance",
    ):
        reserve_supervised_plan(ledger, reconstructed, approval)

    witness = reconstructed.execution_plan.plan_id.removeprefix("supervised-v2-")
    assert not ledger.supervised_plan_issuance_is_current(
        plan_id=reconstructed.execution_plan.plan_id,
        bound_plan_witness=witness,
        plan_fingerprint=reconstructed.execution_plan.fingerprint,
    )


def test_raw_ledger_plan_reservation_cannot_authorize_attempt_after_restart(
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    reconstructed = replace(bound)
    ledger = RealExecutionLedger(tmp_path / "raw-attempt-ledger.jsonl")
    ledger.reserve_plan(reconstructed.execution_plan)
    action = reconstructed.execution_plan.actions[0]

    with pytest.raises(
        SupervisedExecutionError,
        match="not durably product-issued and reserved",
    ):
        begin_supervised_attempt(
            ledger,
            reconstructed,
            approval,
            action_id=action.action_id,
            attempt_id="raw-reserve-attempt",
        )


def test_live_issued_plan_can_close_crash_window_after_raw_plan_reservation(
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    ledger = RealExecutionLedger(tmp_path / "live-crash-window-ledger.jsonl")
    ledger.reserve_plan(bound.execution_plan)

    fingerprint = reserve_supervised_plan(ledger, bound, approval)
    witness = bound.execution_plan.plan_id.removeprefix("supervised-v2-")

    assert fingerprint == bound.execution_plan.fingerprint
    assert ledger.supervised_plan_issuance_is_current(
        plan_id=bound.execution_plan.plan_id,
        bound_plan_witness=witness,
        plan_fingerprint=fingerprint,
    )


def test_forged_exact_issuance_token_cannot_mint_first_reservation(tmp_path) -> None:
    bound, approval, _, _ = _bound()
    reconstructed = replace(bound)
    legitimate_token = object.__getattribute__(bound, "_product_issuance_token")
    token_type = type(legitimate_token)

    forged_token = object.__new__(token_type)
    object.__setattr__(forged_token, "reference", weakref.ref(reconstructed))
    object.__setattr__(
        forged_token,
        "witness",
        reconstructed.execution_plan.plan_id.removeprefix("supervised-v2-"),
    )
    object.__setattr__(reconstructed, "_product_issuance_token", forged_token)

    ledger = RealExecutionLedger(tmp_path / "forged-token-ledger.jsonl")
    with pytest.raises(
        SupervisedExecutionError,
        match="not current canonical product issuance",
    ):
        reserve_supervised_plan(ledger, reconstructed, approval)

    with pytest.raises(KeyError):
        ledger.saga(reconstructed.execution_plan.plan_id)


def test_reserved_plan_reconstruction_is_restart_idempotent(tmp_path) -> None:
    bound, approval, _, _ = _bound()
    ledger = RealExecutionLedger(tmp_path / "restart-plan-ledger.jsonl")
    fingerprint = reserve_supervised_plan(ledger, bound, approval)
    before = ledger.verify_integrity()
    reconstructed = replace(bound)

    with pytest.raises(
        SupervisedExecutionError,
        match="not current canonical product issuance",
    ):
        assert_bound_supervised_execution_plan_authoritative(reconstructed)

    assert reserve_supervised_plan(ledger, reconstructed, approval) == fingerprint
    assert ledger.verify_integrity() == before

    action = reconstructed.execution_plan.actions[0]
    attempt = begin_supervised_attempt(
        ledger,
        reconstructed,
        approval,
        action_id=action.action_id,
        attempt_id="restart-attempt-1",
    )
    assert attempt.attempt_id == "restart-attempt-1"


def test_unreserved_reconstruction_cannot_use_restart_authority(tmp_path) -> None:
    bound, approval, _, _ = _bound()
    reconstructed = replace(bound)
    ledger = RealExecutionLedger(tmp_path / "unreserved-restart-ledger.jsonl")
    action = reconstructed.execution_plan.actions[0]

    with pytest.raises(
        SupervisedExecutionError,
        match="not durably product-issued and reserved",
    ):
        begin_supervised_attempt(
            ledger,
            reconstructed,
            approval,
            action_id=action.action_id,
            attempt_id="must-not-start",
        )

    with pytest.raises(KeyError):
        ledger.saga(reconstructed.execution_plan.plan_id)


def test_reserved_reconstruction_metadata_mutation_fails_closed(tmp_path) -> None:
    bound, approval, _, _ = _bound()
    ledger = RealExecutionLedger(tmp_path / "mutated-restart-ledger.jsonl")
    reserve_supervised_plan(ledger, bound, approval)
    reconstructed = replace(bound)
    object.__setattr__(reconstructed, "intent_id", "mutated-after-restart")
    action = reconstructed.execution_plan.actions[0]

    with pytest.raises(
        SupervisedExecutionError,
        match="not a canonical structural binding",
    ):
        begin_supervised_attempt(
            ledger,
            reconstructed,
            approval,
            action_id=action.action_id,
            attempt_id="mutated-restart-attempt",
        )


def test_bound_plan_witness_alias_rebinding_fails_before_hostile_dispatch(
    monkeypatch,
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    hostile_calls: list[object] = []

    def hostile_witness(value):
        hostile_calls.append(value)
        return bound.execution_plan.plan_id.removeprefix("supervised-v2-")

    monkeypatch.setattr(
        supervised_execution,
        "_bound_plan_witness",
        hostile_witness,
    )
    ledger = RealExecutionLedger(tmp_path / "witness-alias-ledger.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical bound supervised execution plan witness changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


def test_bound_plan_witness_code_replacement_fails_closed(
    monkeypatch,
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()

    def hostile_witness(value):
        return "0" * 64

    monkeypatch.setattr(
        supervised_execution._bound_plan_witness,
        "__code__",
        hostile_witness.__code__,
    )
    ledger = RealExecutionLedger(tmp_path / "witness-code-ledger.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical bound supervised execution plan witness changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


def test_instance_shadowed_ledger_methods_cannot_reopen_revoked_approval(
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    ledger = RealExecutionLedger(tmp_path / "shadowed-revoked-ledger.jsonl")
    reserve_supervised_plan(ledger, bound, approval)
    revoke_supervised_approval(
        ledger,
        bound,
        approval,
        revocation_evidence_sha256="f" * 64,
    )
    hostile_calls: list[object] = []

    def hostile_begin_attempt(**kwargs):
        hostile_calls.append(kwargs)
        raise AssertionError("instance-shadowed begin_attempt executed")

    ledger.__dict__["supervised_approval_is_active"] = lambda **_kwargs: True
    ledger.__dict__["begin_attempt"] = hostile_begin_attempt
    action = bound.execution_plan.actions[0]

    with pytest.raises(
        SupervisedExecutionError,
        match="durable supervised approval is missing or revoked",
    ):
        begin_supervised_attempt(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="shadowed-attempt",
        )

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.attempt_state("shadowed-attempt")



@pytest.mark.parametrize(
    "method_name",
    (
        "_events",
        "_plan_event",
        "_append",
        "_mutate",
        "_ensure_existing_path_durable",
        "_sync_parent_directory",
        "_validate_event",
        "_parse",
        "_validate_semantics",
    ),
)
def test_class_rebound_ledger_internal_method_fails_before_hostile_dispatch(
    monkeypatch,
    tmp_path,
    method_name,
) -> None:
    bound, approval, _, _ = _bound()
    ledger = RealExecutionLedger(tmp_path / f"internal-class-{method_name}.jsonl")
    hostile_calls: list[object] = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile ledger internal executed")

    monkeypatch.setattr(RealExecutionLedger, method_name, hostile)

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical real execution ledger internal authority changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    assert hostile_calls == []
    assert not ledger.path.exists()


@pytest.mark.parametrize(
    "method_name",
    (
        "_events",
        "_plan_event",
        "_append",
        "_mutate",
        "_ensure_existing_path_durable",
        "_sync_parent_directory",
        "_validate_event",
        "_parse",
        "_validate_semantics",
    ),
)
def test_instance_shadowed_ledger_internal_method_fails_before_hostile_dispatch(
    tmp_path,
    method_name,
) -> None:
    bound, approval, _, _ = _bound()
    ledger = RealExecutionLedger(tmp_path / f"internal-instance-{method_name}.jsonl")
    hostile_calls: list[object] = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("instance-shadowed ledger internal executed")

    ledger.__dict__[method_name] = hostile

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical real execution ledger internal authority changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    assert hostile_calls == []
    assert not ledger.path.exists()


def test_class_rebound_ledger_method_fails_before_hostile_dispatch(
    monkeypatch,
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    ledger = RealExecutionLedger(tmp_path / "class-rebound-ledger.jsonl")
    reserve_supervised_plan(ledger, bound, approval)
    hostile_calls: list[object] = []

    def hostile_begin_attempt(self, **kwargs):
        hostile_calls.append((self, kwargs))
        raise AssertionError("class-rebound begin_attempt executed")

    monkeypatch.setattr(
        RealExecutionLedger,
        "begin_attempt",
        hostile_begin_attempt,
    )
    action = bound.execution_plan.actions[0]

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical real execution ledger authority changed",
    ):
        begin_supervised_attempt(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="class-rebound-attempt",
        )

    assert hostile_calls == []



@pytest.mark.parametrize(
    "helper_name",
    (
        "_canonical_bound_plan_authority_dispatch",
        "_canonical_supervised_ledger_dispatch",
        "_canonical_bound_plan_witness",
        "_digest",
        "_time",
        "_sha",
        "_text",
        "assert_verified_provider_evidence_authoritative",
        "_require_bound_plan_structure",
        "_require_approval",
        "_require_durable_approval",
        "_durable_reserved_plan_fingerprint",
        "_require_reserved",
        "_attempt_action",
        "_require_attempt_provider_order_reference",
        "_validate_slippage",
        "_require_verified_profile",
    ),
)
def test_supervised_composition_helper_alias_rebinding_fails_before_mutation(
    monkeypatch,
    tmp_path,
    helper_name,
) -> None:
    bound, approval, _, _ = _bound()
    hostile_calls: list[object] = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        return None

    monkeypatch.setattr(supervised_execution, helper_name, hostile)
    ledger = RealExecutionLedger(tmp_path / f"helper-{helper_name}.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical supervised execution composition changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


@pytest.mark.parametrize(
    "helper_name",
    (
        "_canonical_bound_plan_authority_dispatch",
        "_canonical_supervised_ledger_dispatch",
        "_durable_reserved_plan_fingerprint",
        "_require_reserved",
    ),
)
def test_supervised_composition_helper_code_replacement_fails_before_mutation(
    monkeypatch,
    tmp_path,
    helper_name,
) -> None:
    bound, approval, _, _ = _bound()
    target = getattr(supervised_execution, helper_name)

    def hostile(*args, **kwargs):
        raise AssertionError("hostile helper code executed")

    monkeypatch.setattr(target, "__code__", hostile.__code__)
    ledger = RealExecutionLedger(tmp_path / f"helper-code-{helper_name}.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical supervised execution composition changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)



@pytest.mark.parametrize(
    ("owner_name", "attribute_name"),
    (
        ("bound_plan", "action_for"),
        ("bound_plan", "constraint_for"),
        ("bound_plan", "profile_for"),
        ("approval", "require_active"),
        ("approval", "fingerprint"),
        ("approval", "ledger_identity"),
        ("execution_plan", "fingerprint"),
    ),
)
def test_supervised_composition_descriptor_rebinding_fails_before_mutation(
    monkeypatch,
    tmp_path,
    owner_name,
    attribute_name,
) -> None:
    bound, approval, _, _ = _bound()
    owners = {
        "bound_plan": type(bound),
        "approval": type(approval),
        "execution_plan": type(bound.execution_plan),
    }

    def hostile(*args, **kwargs):
        raise AssertionError("hostile descriptor executed")

    monkeypatch.setattr(owners[owner_name], attribute_name, hostile)
    ledger = RealExecutionLedger(
        tmp_path / f"descriptor-{owner_name}-{attribute_name}.jsonl"
    )

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical supervised execution composition changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


@pytest.mark.parametrize(
    "object_name",
    (
        "SupervisedApproval",
        "VerifiedProviderEffectEvidence",
        "VerifiedProviderAbsenceEvidence",
        "ExternalAcknowledgement",
        "ExternalEffectReconciliation",
        "ReconciliationSnapshot",
        "AcknowledgementStatus",
        "AttemptState",
    ),
)
def test_supervised_composition_type_alias_rebinding_fails_before_mutation(
    monkeypatch,
    tmp_path,
    object_name,
) -> None:
    bound, approval, _, _ = _bound()
    monkeypatch.setattr(supervised_execution, object_name, object())
    ledger = RealExecutionLedger(tmp_path / f"type-alias-{object_name}.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical supervised execution composition changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


def test_supervised_approval_property_code_replacement_fails_before_mutation(
    monkeypatch,
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    descriptor = vars(type(approval))["fingerprint"]

    def hostile(self):
        raise AssertionError("hostile approval fingerprint executed")

    monkeypatch.setattr(descriptor.fget, "__code__", hostile.__code__)
    ledger = RealExecutionLedger(tmp_path / "approval-property-code.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical supervised execution composition changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


def test_supervised_entrypoint_raw_code_replacement_fails_before_mutation(
    monkeypatch,
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    raw = reserve_supervised_plan.__wrapped__

    def hostile(*args, **kwargs):
        raise AssertionError("hostile reserve code executed")

    monkeypatch.setattr(raw, "__code__", hostile.__code__)
    ledger = RealExecutionLedger(tmp_path / "entrypoint-code-rebound.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical supervised execution entrypoint changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


@pytest.mark.parametrize(
    ("owner_name", "attribute_name"),
    (
        ("execution_plan", "to_dict"),
        ("execution_action", "to_dict"),
        ("constraint", "to_dict"),
        ("bound_plan", "verify_binding"),
    ),
)
def test_bound_plan_transitive_method_rebinding_fails_before_mutation(
    monkeypatch,
    tmp_path,
    owner_name,
    attribute_name,
) -> None:
    bound, approval, _, _ = _bound()
    owners = {
        "execution_plan": type(bound.execution_plan),
        "execution_action": type(bound.execution_plan.actions[0]),
        "constraint": type(bound.constraints[0]),
        "bound_plan": type(bound),
    }
    hostile_calls: list[object] = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile binding method executed")

    monkeypatch.setattr(owners[owner_name], attribute_name, hostile)
    ledger = RealExecutionLedger(
        tmp_path / f"binding-method-{owner_name}-{attribute_name}.jsonl"
    )

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical .*binding.* changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


def test_bound_binding_alias_rebinding_fails_before_hostile_dispatch(
    monkeypatch,
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    hostile_calls: list[object] = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        return "0" * 64

    monkeypatch.setattr(
        supervised_execution,
        "_bound_binding_sha256",
        hostile,
    )
    ledger = RealExecutionLedger(tmp_path / "binding-alias-rebound.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical bound supervised execution plan binding changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


@pytest.mark.parametrize("member_name", ("dumps",))
def test_digest_json_dispatch_rebinding_fails_before_hostile_dispatch(
    monkeypatch,
    tmp_path,
    member_name,
) -> None:
    bound, approval, _, _ = _bound()
    hostile_calls: list[object] = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        return "{}"

    monkeypatch.setattr(supervised_execution.json, member_name, hostile)
    ledger = RealExecutionLedger(tmp_path / "digest-json-rebound.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical supervised execution digest authority changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


def test_digest_hash_dispatch_rebinding_fails_before_hostile_dispatch(
    monkeypatch,
    tmp_path,
) -> None:
    bound, approval, _, _ = _bound()
    hostile_calls: list[object] = []

    def hostile(*args, **kwargs):
        hostile_calls.append((args, kwargs))
        raise AssertionError("hostile digest executed")

    monkeypatch.setattr(supervised_execution.hashlib, "sha256", hostile)
    ledger = RealExecutionLedger(tmp_path / "digest-hash-rebound.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="canonical supervised execution digest authority changed",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.saga(bound.execution_plan.plan_id)


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


class _ExecutionReadbackTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected Betfair readback transport call")
        return self.responses.pop(0)


def _rpc_result(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _provider_capture(
    action,
    *,
    matched_stake: Decimal | None,
    matched_odds: Decimal | None = None,
    customer_order_ref: str | None = None,
    market_id: str | None = None,
    selection_id: int | None = None,
    current_more_available: bool = False,
    account_id: str = "acct-1",
    provider_event_id: str | None = None,
    cleared_status: str | None = None,
    cleared_event_id: str | None = None,
    catalogue_available: bool = True,
):
    event_id = provider_event_id or action.event_id
    current_orders: list[dict[str, object]] = []
    if matched_stake is not None:
        current_orders.append(
            {
                "betId": "bet-1",
                "marketId": market_id or action.market_id,
                "selectionId": (
                    int(action.selection_id)
                    if selection_id is None
                    else selection_id
                ),
                "side": action.side,
                "status": "EXECUTABLE",
                "placedDate": SUBMITTED_AT,
                "priceSize": {
                    "price": float(action.requested_odds),
                    "size": float(action.requested_stake),
                },
                "averagePriceMatched": float(
                    matched_odds
                    if matched_odds is not None
                    else action.requested_odds
                ),
                "sizeMatched": float(matched_stake),
                "sizeRemaining": float(action.requested_stake - matched_stake),
                "customerOrderRef": (
                    action.action_id
                    if customer_order_ref is None
                    else customer_order_ref
                ),
            }
        )

    responses = [
        _rpc_result(
            (
                [
                    {
                        "marketId": action.market_id,
                        "event": {"id": event_id},
                    }
                ]
                if catalogue_available
                else []
            ),
            1,
        ),
        _rpc_result(
            {
                "currentOrders": current_orders,
                "moreAvailable": current_more_available,
            },
            2,
        ),
    ]
    if not current_more_available:
        request_id = 3
        for status in ("SETTLED", "VOIDED", "LAPSED", "CANCELLED"):
            orders: list[dict[str, object]] = []
            if cleared_status == status:
                orders.append(
                    {
                        "betId": "bet-cleared-1",
                        "eventId": cleared_event_id or action.event_id,
                        "marketId": action.market_id,
                        "selectionId": int(action.selection_id),
                        "side": action.side,
                        "placedDate": SUBMITTED_AT,
                        "settledDate": READBACK_AT,
                        "priceRequested": float(action.requested_odds),
                        "priceMatched": (
                            float(action.requested_odds)
                            if status == "SETTLED"
                            else 0.0
                        ),
                        "sizeSettled": 0.0,
                        "profit": 0.0,
                        "customerOrderRef": action.action_id,
                    }
                )
            responses.append(
                _rpc_result(
                    {
                        "clearedOrders": orders,
                        "moreAvailable": False,
                    },
                    request_id,
                )
            )
            request_id += 1

    transport = _ExecutionReadbackTransport(responses)
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: datetime.fromisoformat(READBACK_AT),
        venue_id="betfair",
        account_id=account_id,
    )
    capture = client.read_execution_readback(
        action_id=action.action_id,
        market_id=action.market_id,
    )
    return capture, transport


def _verified_state(bound, action, *, matched_stake: Decimal | None, **kwargs):
    capture, _ = _provider_capture(
        action,
        matched_stake=matched_stake,
        **kwargs,
    )
    binding = bound.profile_for(action.bookmaker_id, action.account_id)
    return verify_betfair_provider_state(
        action,
        _profile(),
        expected_profile_sha256=binding.profile_sha256,
        readback=capture,
    )

def test_lay_is_fail_closed_until_upstream_liability_authority_exists() -> None:
    bound, _, _, _ = _bound()
    constraint = bound.constraints[0]
    with pytest.raises(SupervisedExecutionError, match="BACK only"):
        replace(constraint, side="LAY")


def test_bound_metadata_substitution_is_rejected_by_plan_identity() -> None:
    bound, _, _, _ = _bound()
    forged_constraint = replace(
        bound.constraints[0],
        max_slippage_fraction=Decimal("0.25"),
    )
    with pytest.raises(SupervisedExecutionError, match="durable plan identity"):
        replace(bound, constraints=(forged_constraint,))


def test_durable_revocation_rejects_original_approved_object_after_restart() -> None:
    bound, approval, _, _ = _bound()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "execution.jsonl"
        ledger = RealExecutionLedger(path)
        reserve_supervised_plan(ledger, bound, approval)
        restarted = RealExecutionLedger(path)
        revoke_supervised_approval(
            restarted,
            bound,
            approval,
            revocation_evidence_sha256="8" * 64,
        )
        after_revoke = RealExecutionLedger(path)
        with pytest.raises(SupervisedExecutionError, match="missing or revoked"):
            begin_supervised_attempt(
                after_revoke,
                bound,
                approval,
                action_id=bound.execution_plan.actions[0].action_id,
                attempt_id="attempt-revoked",
            )



def test_reservation_rejects_future_dated_product_issued_plan(tmp_path) -> None:
    bound, approval, _, _ = _bound(
        created_at="2026-09-18T13:20:04+00:00",
    )
    ledger = RealExecutionLedger(tmp_path / "future-created-plan.jsonl")

    with pytest.raises(
        SupervisedExecutionError,
        match="creation is in the future relative to trusted clock",
    ):
        reserve_supervised_plan(ledger, bound, approval)

    assert not ledger.path.exists()


def test_trusted_clock_prevents_backdating_expired_quote(monkeypatch) -> None:
    bound, approval, _, _ = _bound()
    with tempfile.TemporaryDirectory() as tmp:
        ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
        reserve_supervised_plan(ledger, bound, approval)
        monkeypatch.setattr(
            "autosport.supervised_execution._trusted_now",
            lambda: "2026-09-18T13:21:01+00:00",
        )
        with pytest.raises(SupervisedExecutionError, match="quote expiry"):
            begin_supervised_attempt(
                ledger,
                bound,
                approval,
                action_id=bound.execution_plan.actions[0].action_id,
                attempt_id="attempt-too-late",
            )


def test_generic_snapshot_cannot_authorize_positive_but_verified_provider_pages_can() -> None:
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

        verified = _verified_state(
            bound,
            action,
            matched_stake=action.requested_stake / Decimal("2"),
            matched_odds=Decimal("1.99"),
        )
        assert isinstance(verified, VerifiedProviderEffectEvidence)
        result = reconcile_provider_readback(
            ledger,
            bound,
            attempt_id="attempt-1",
            readback=verified,
        )
        assert result.outcome is ReadbackOutcome.PARTIAL
        assert result.attempt_state is AttemptState.PARTIAL
        before = ledger.verified_snapshot().event_count
        replay = reconcile_provider_readback(
            ledger,
            bound,
            attempt_id="attempt-1",
            readback=verified,
        )
        assert replay == result
        assert ledger.verified_snapshot().event_count == before

        restarted = RealExecutionLedger(path)
        assert restarted.verify_integrity() > 0
        assert restarted.attempt_state("attempt-1") is AttemptState.PARTIAL
        assert restarted.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        ) is False


def test_direct_submitted_ack_persists_exact_provider_evidence_across_restart() -> None:
    bound, approval, _, _ = _bound()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "execution.jsonl"
        ledger = RealExecutionLedger(path)
        reserve_supervised_plan(ledger, bound, approval)
        action = bound.execution_plan.actions[0]
        begin_supervised_attempt(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-direct",
        )
        ledger.mark_submitted("attempt-direct", submitted_at=SUBMITTED_AT)
        verified = _verified_state(
            bound,
            action,
            matched_stake=action.requested_stake,
        )
        assert isinstance(verified, VerifiedProviderEffectEvidence)
        result = reconcile_provider_readback(
            ledger,
            bound,
            attempt_id="attempt-direct",
            readback=verified,
        )
        assert result.outcome is ReadbackOutcome.ACCEPTED

        restarted = RealExecutionLedger(path)
        binding = restarted.provider_evidence_binding("attempt-direct")
        assert binding is not None
        assert binding["evidence_id"] == verified.evidence_id
        assert binding["source"] == (
            f"betfair-readonly:{verified.source_payload_sha256}"
        )
        before = restarted.verified_snapshot().event_count
        replay = reconcile_provider_readback(
            restarted,
            bound,
            attempt_id="attempt-direct",
            readback=verified,
        )
        assert replay == result
        assert restarted.verified_snapshot().event_count == before


def test_caller_constructed_positive_readback_cannot_mint_ack() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger, bound, _, action, _, _ = _ledger_with_unknown(
            Path(tmp) / "execution.jsonl"
        )
        forged = ProviderReadback(
            bookmaker_id=action.bookmaker_id,
            account_id=action.account_id,
            action_id=action.action_id,
            adapter_id="betfair-exchange-jsonrpc-readonly",
            adapter_version="1",
            profile_version=1,
            event_id=action.event_id,
            market_id=action.market_id,
            selection_id=action.selection_id,
            external_receipt_id="forged",
            observed_at=READBACK_AT,
            source_payload_sha256="f" * 64,
            status=AcknowledgementStatus.ACCEPTED,
            accepted_odds=action.requested_odds,
            accepted_stake=action.requested_stake,
        )
        with pytest.raises(
            SupervisedExecutionError,
            match="verified canonical provider evidence",
        ):
            reconcile_provider_readback(
                ledger,
                bound,
                attempt_id="attempt-1",
                readback=forged,
            )
        assert ledger.attempt_state("attempt-1") is AttemptState.UNKNOWN


def test_caller_cannot_clone_verified_effect_to_mint_ack() -> None:
    assert not hasattr(provider_evidence, "_SEAL")
    with tempfile.TemporaryDirectory() as tmp:
        ledger, bound, _, action, _, _ = _ledger_with_unknown(
            Path(tmp) / "execution.jsonl"
        )
        genuine = _verified_state(
            bound,
            action,
            matched_stake=action.requested_stake,
        )
        assert isinstance(genuine, VerifiedProviderEffectEvidence)
        forged = replace(genuine)

        with pytest.raises(
            SupervisedExecutionError,
            match="provider evidence is not authoritative",
        ):
            reconcile_provider_readback(
                ledger,
                bound,
                attempt_id="attempt-1",
                readback=forged,
            )
        assert ledger.attempt_state("attempt-1") is AttemptState.UNKNOWN


def test_complete_provider_absence_is_diagnostic_without_retry_authority() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger, bound, _, action, _, _ = _ledger_with_unknown(
            Path(tmp) / "execution.jsonl"
        )
        verified = _verified_state(bound, action, matched_stake=None)
        assert isinstance(verified, VerifiedProviderAbsenceEvidence)
        result = reconcile_provider_not_found(
            ledger,
            bound,
            attempt_id="attempt-1",
            readback=verified,
        )
        assert result.outcome is ReadbackOutcome.NOT_FOUND
        assert ledger.attempt_state("attempt-1") is AttemptState.RECONCILED_NOT_FOUND
        assert ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        ) is False


def test_opaque_not_found_hashes_cannot_release_retry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger, bound, _, action, _, _ = _ledger_with_unknown(
            Path(tmp) / "execution.jsonl"
        )
        forged = ProviderNotFoundReadback(
            bookmaker_id=action.bookmaker_id,
            account_id=action.account_id,
            action_id=action.action_id,
            adapter_id="betfair-exchange-jsonrpc-readonly",
            adapter_version="1",
            profile_version=1,
            event_id=action.event_id,
            market_id=action.market_id,
            selection_id=action.selection_id,
            observed_at=READBACK_AT,
            current_source_payload_sha256="3" * 64,
            cleared_source_payload_sha256="4" * 64,
        )
        with pytest.raises(
            SupervisedExecutionError,
            match="verified complete provider absence",
        ):
            reconcile_provider_not_found(
                ledger,
                bound,
                attempt_id="attempt-1",
                readback=forged,
            )
        assert ledger.attempt_state("attempt-1") is AttemptState.UNKNOWN
        assert ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        ) is False


def test_caller_cannot_clone_verified_absence_to_release_retry() -> None:
    assert not hasattr(provider_evidence, "_SEAL")
    with tempfile.TemporaryDirectory() as tmp:
        ledger, bound, _, action, _, _ = _ledger_with_unknown(
            Path(tmp) / "execution.jsonl"
        )
        genuine = _verified_state(bound, action, matched_stake=None)
        assert isinstance(genuine, VerifiedProviderAbsenceEvidence)
        forged = replace(genuine)

        with pytest.raises(
            SupervisedExecutionError,
            match="provider absence evidence is not authoritative",
        ):
            reconcile_provider_not_found(
                ledger,
                bound,
                attempt_id="attempt-1",
                readback=forged,
            )
        assert ledger.attempt_state("attempt-1") is AttemptState.UNKNOWN
        assert ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        ) is False


def test_incomplete_provider_pagination_cannot_prove_absence() -> None:
    bound, _, _, _ = _bound()
    action = bound.execution_plan.actions[0]
    with pytest.raises(BetfairReadOnlyError, match="cannot advance"):
        _provider_capture(
            action,
            matched_stake=None,
            current_more_available=True,
        )


def test_future_profile_version_cannot_rebind_approved_plan() -> None:
    bound, _, _, _ = _bound()
    action = bound.execution_plan.actions[0]
    capture, _ = _provider_capture(
        action,
        matched_stake=action.requested_stake,
    )
    binding = bound.profile_for(action.bookmaker_id, action.account_id)
    with pytest.raises(ProviderEvidenceError, match="exact bound profile"):
        verify_betfair_provider_state(
            action,
            replace(_profile(), profile_version=2),
            expected_profile_sha256=binding.profile_sha256,
            readback=capture,
        )


@pytest.mark.parametrize(
    "matched_stake",
    (None, Decimal("5.00")),
    ids=("absence", "positive"),
)
def test_unissued_capture_copy_cannot_mint_provider_state(
    matched_stake: Decimal | None,
) -> None:
    bound, _, _, _ = _bound()
    action = bound.execution_plan.actions[0]
    capture, _ = _provider_capture(
        action,
        matched_stake=(
            action.requested_stake
            if matched_stake is not None
            else None
        ),
    )
    forged = replace(capture)
    binding = bound.profile_for(action.bookmaker_id, action.account_id)

    with pytest.raises(
        ProviderEvidenceError,
        match="authoritative canonical readback capture",
    ):
        verify_betfair_provider_state(
            action,
            _profile(),
            expected_profile_sha256=binding.profile_sha256,
            readback=forged,
        )


def test_provider_order_identity_conflict_fails_closed() -> None:
    bound, _, _, _ = _bound()
    action = bound.execution_plan.actions[0]
    capture, _ = _provider_capture(
        action,
        matched_stake=action.requested_stake,
        market_id="different-market",
    )
    binding = bound.profile_for(action.bookmaker_id, action.account_id)
    with pytest.raises(ProviderEvidenceError, match="identity conflicts"):
        verify_betfair_provider_state(
            action,
            _profile(),
            expected_profile_sha256=binding.profile_sha256,
            readback=capture,
        )


def test_cross_account_capture_cannot_authorize_reconciliation() -> None:
    bound, _, _, _ = _bound()
    action = bound.execution_plan.actions[0]
    capture, _ = _provider_capture(
        action,
        matched_stake=None,
        account_id="other-account",
    )
    binding = bound.profile_for(action.bookmaker_id, action.account_id)
    with pytest.raises(ProviderEvidenceError, match="scope conflicts"):
        verify_betfair_provider_state(
            action,
            _profile(),
            expected_profile_sha256=binding.profile_sha256,
            readback=capture,
        )


def test_provider_market_event_identity_conflict_fails_closed() -> None:
    bound, _, _, _ = _bound()
    action = bound.execution_plan.actions[0]
    capture, _ = _provider_capture(
        action,
        matched_stake=action.requested_stake,
        provider_event_id="different-event",
    )
    binding = bound.profile_for(action.bookmaker_id, action.account_id)
    with pytest.raises(ProviderEvidenceError, match="market-to-event identity"):
        verify_betfair_provider_state(
            action,
            _profile(),
            expected_profile_sha256=binding.profile_sha256,
            readback=capture,
        )


def test_non_settled_cleared_order_blocks_absence_retry_release() -> None:
    bound, _, _, _ = _bound()
    action = bound.execution_plan.actions[0]
    capture, _ = _provider_capture(
        action,
        matched_stake=None,
        cleared_status="CANCELLED",
    )
    binding = bound.profile_for(action.bookmaker_id, action.account_id)
    with pytest.raises(ProviderEvidenceError, match="non-settled cleared state"):
        verify_betfair_provider_state(
            action,
            _profile(),
            expected_profile_sha256=binding.profile_sha256,
            readback=capture,
        )


def test_cleared_provider_event_mismatch_fails_closed() -> None:
    bound, _, _, _ = _bound()
    action = bound.execution_plan.actions[0]
    capture, _ = _provider_capture(
        action,
        matched_stake=None,
        cleared_status="CANCELLED",
        cleared_event_id="different-event",
    )
    binding = bound.profile_for(action.bookmaker_id, action.account_id)
    with pytest.raises(ProviderEvidenceError, match="event identity conflicts"):
        verify_betfair_provider_state(
            action,
            _profile(),
            expected_profile_sha256=binding.profile_sha256,
            readback=capture,
        )


def test_closed_market_can_bind_event_from_cleared_bet_without_releasing_retry() -> None:
    bound, _, _, _ = _bound()
    action = bound.execution_plan.actions[0]
    capture, _ = _provider_capture(
        action,
        matched_stake=None,
        cleared_status="CANCELLED",
        catalogue_available=False,
    )
    assert capture.market_event.event_id == action.event_id
    assert capture.market_event.source == "cleared:CANCELLED"
    binding = bound.profile_for(action.bookmaker_id, action.account_id)
    with pytest.raises(ProviderEvidenceError, match="non-settled cleared state"):
        verify_betfair_provider_state(
            action,
            _profile(),
            expected_profile_sha256=binding.profile_sha256,
            readback=capture,
        )


def test_verified_readback_still_enforces_approved_slippage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger, bound, _, action, _, _ = _ledger_with_unknown(
            Path(tmp) / "execution.jsonl"
        )
        verified = _verified_state(
            bound,
            action,
            matched_stake=action.requested_stake,
            matched_odds=Decimal("1.80"),
        )
        assert isinstance(verified, VerifiedProviderEffectEvidence)
        with pytest.raises(SupervisedExecutionError, match="slippage"):
            reconcile_provider_readback(
                ledger,
                bound,
                attempt_id="attempt-1",
                readback=verified,
            )
        assert ledger.attempt_state("attempt-1") is AttemptState.UNKNOWN


def test_bridge_rejects_caller_asserted_terminal_settlement_exactness() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger, bound, _, action, _, _ = _ledger_with_unknown(
            Path(tmp) / "execution.jsonl"
        )
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
