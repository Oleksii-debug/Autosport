from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal, localcontext

import pytest

import autosport.execution_capital_at_risk as capital_risk_module
from autosport.execution_capital_at_risk import (
    CapitalRiskTruth,
    ExecutionCapitalAtRiskError,
    ExecutionCapitalAtRiskStale,
    ExecutionCapitalAtRiskUnsupported,
    _subtract,
    resolve_execution_capital_at_risk,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    ExecutionLedgerIntegrityError,
    ExecutionStateError,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


CREATED_AT = "2026-09-22T07:00:00+00:00"
RESERVED_AT = "2026-09-22T07:00:01+00:00"
SUBMITTED_AT = "2026-09-22T07:00:02+00:00"
UNKNOWN_AT = "2026-09-22T07:00:03+00:00"
RECONCILED_AT = "2026-09-22T07:00:04+00:00"
ACKNOWLEDGED_AT = "2026-09-22T07:00:05+00:00"
EXPIRES_AT = "2026-09-22T08:00:00+00:00"


def _action(
    *,
    action_id: str = "action-1",
    bookmaker_id: str = "betfair",
    account_id: str = "acct-1",
    side: str = "BACK",
    odds: str = "2",
    stake: str = "10",
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id=bookmaker_id,
        account_id=account_id,
        event_id="event-1",
        market_id="1.234",
        selection_id="10",
        side=side,
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=CREATED_AT,
        expires_at=EXPIRES_AT,
    )


def _plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-risk",
        bookmaker_profile_version="betfair-profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=CREATED_AT,
        actions=tuple(actions),
    )


def _ledger(tmp_path, action: ExecutionAction) -> tuple[RealExecutionLedger, ExecutionPlan]:
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    plan = _plan(action)
    ledger.reserve_plan(plan)
    return ledger, plan


def _attempt(
    ledger: RealExecutionLedger,
    plan: ExecutionPlan,
    *,
    attempt_id: str = "attempt-1",
    submit: bool = True,
) -> None:
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=plan.actions[0].action_id,
        attempt_id=attempt_id,
        reserved_at=RESERVED_AT,
    )
    if submit:
        ledger.mark_submitted(attempt_id, submitted_at=SUBMITTED_AT)


def _ack(
    ledger: RealExecutionLedger,
    *,
    attempt_id: str = "attempt-1",
    status: AcknowledgementStatus,
    accepted_stake: str | None = None,
    accepted_odds: str | None = None,
) -> None:
    acknowledgement = ExternalAcknowledgement(
        attempt_id=attempt_id,
        external_receipt_id=f"receipt-{attempt_id}",
        status=status,
        acknowledged_at=ACKNOWLEDGED_AT,
        accepted_stake=accepted_stake,
        accepted_odds=accepted_odds,
    )
    evidence_id = hashlib.sha256(
        (
            f"{attempt_id}:{status.value}:"
            f"{accepted_stake!r}:{accepted_odds!r}"
        ).encode("utf-8")
    ).hexdigest()
    ledger._bind_provider_acknowledgement_evidence(
        attempt_id=attempt_id,
        evidence_id=evidence_id,
        observed_at=ACKNOWLEDGED_AT,
        source="test-provider-immediate-response",
        acknowledgement=acknowledgement,
    )
    ledger.acknowledge(acknowledgement)


def test_unknown_back_keeps_full_requested_capital_contingent(tmp_path) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    _attempt(ledger, plan)
    ledger.mark_unknown(
        "attempt-1",
        reason="transport_timeout",
        observed_at=UNKNOWN_AT,
    )

    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    assert evidence.truth is CapitalRiskTruth.CONSERVATIVE_BOUND
    assert evidence.confirmed_open_capital == Decimal("0")
    assert evidence.contingent_unknown_capital == Decimal("10")
    assert evidence.confirmed_released_capital == Decimal("0")
    assert evidence.max_plausible_capital_at_risk == Decimal("10")
    assert evidence.execution_authority is False
    assert evidence.capital_release_authority is False
    assert evidence.residual_capacity_authority is False
    evidence.assert_issued_current(ledger)


def test_partial_back_splits_confirmed_and_unresolved_without_double_count(
    tmp_path,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    _attempt(ledger, plan)
    _ack(
        ledger,
        status=AcknowledgementStatus.PARTIAL,
        accepted_stake="4",
        accepted_odds="2.2",
    )

    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)
    attempt = evidence.attempts[0]

    assert attempt.confirmed_open_capital == Decimal("4")
    assert attempt.contingent_unknown_capital == Decimal("6")
    assert attempt.max_plausible_capital_at_risk == Decimal("10")
    assert evidence.confirmed_open_capital == Decimal("4")
    assert evidence.contingent_unknown_capital == Decimal("6")
    assert evidence.max_plausible_capital_at_risk == Decimal("10")


def test_accepted_back_confirms_full_requested_capital_under_current_writer_contract(
    tmp_path,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    _attempt(ledger, plan)
    _ack(
        ledger,
        status=AcknowledgementStatus.ACCEPTED,
        accepted_stake="10",
        accepted_odds="2",
    )

    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    assert evidence.confirmed_open_capital == Decimal("10")
    assert evidence.contingent_unknown_capital == Decimal("0")
    assert evidence.max_plausible_capital_at_risk == Decimal("10")


def test_generic_rejected_ack_is_not_provider_origin_release_authority(
    tmp_path,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    _attempt(ledger, plan)
    _ack(ledger, status=AcknowledgementStatus.REJECTED)

    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    assert evidence.confirmed_open_capital == Decimal("0")
    assert evidence.confirmed_released_capital == Decimal("0")
    assert evidence.contingent_unknown_capital == Decimal("10")
    assert evidence.max_plausible_capital_at_risk == Decimal("10")


def test_generic_not_found_does_not_release_contingent_capital(tmp_path) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    _attempt(ledger, plan)
    ledger.mark_unknown(
        "attempt-1",
        reason="transport_timeout",
        observed_at=UNKNOWN_AT,
    )
    ledger.reconcile_not_found(
        ReconciliationSnapshot(
            attempt_id="attempt-1",
            evidence_id="caller-asserted-absence",
            observed_at=RECONCILED_AT,
            external_effect_found=False,
            source="generic-readback",
        )
    )

    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    assert evidence.attempts[0].state.value == "RECONCILED_NOT_FOUND"
    assert evidence.confirmed_released_capital == Decimal("0")
    assert evidence.contingent_unknown_capital == Decimal("10")
    assert evidence.max_plausible_capital_at_risk == Decimal("10")


def test_reserved_attempt_has_no_external_effect_capital(tmp_path) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    _attempt(ledger, plan, submit=False)

    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    assert evidence.confirmed_open_capital == Decimal("0")
    assert evidence.contingent_unknown_capital == Decimal("0")
    assert evidence.max_plausible_capital_at_risk == Decimal("0")


def test_generic_not_found_keeps_full_risk_and_does_not_authorize_retry(
    tmp_path,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    _attempt(ledger, plan, attempt_id="attempt-1")
    ledger.mark_unknown(
        "attempt-1",
        reason="transport_timeout",
        observed_at=UNKNOWN_AT,
    )
    ledger.reconcile_not_found(
        ReconciliationSnapshot(
            attempt_id="attempt-1",
            evidence_id="generic-not-found-before-retry",
            observed_at=RECONCILED_AT,
            external_effect_found=False,
            source="generic-readback",
        )
    )

    with pytest.raises(
        ExecutionStateError,
        match="retry requires product-issued no-effect authority",
    ):
        ledger.begin_attempt(
            plan_id=plan.plan_id,
            action_id=plan.actions[0].action_id,
            attempt_id="attempt-2",
            reserved_at="2026-09-22T07:00:05+00:00",
        )

    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    assert len(evidence.attempts) == 1
    assert evidence.confirmed_open_capital == Decimal("0")
    assert evidence.contingent_unknown_capital == Decimal("10")
    assert evidence.max_plausible_capital_at_risk == Decimal("10")


def test_restart_reresolves_same_deterministic_evidence_identity(tmp_path) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    _attempt(ledger, plan)
    ledger.mark_unknown(
        "attempt-1",
        reason="transport_timeout",
        observed_at=UNKNOWN_AT,
    )
    first = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    reopened = RealExecutionLedger(ledger.path)
    second = resolve_execution_capital_at_risk(reopened, plan.plan_id)

    assert second == first
    assert second.evidence_sha256 == first.evidence_sha256
    second.assert_issued_current(reopened)


def test_evidence_becomes_stale_after_any_later_ledger_append(tmp_path) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    before = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    _attempt(ledger, plan, submit=False)

    with pytest.raises(
        ExecutionCapitalAtRiskStale,
        match="changed after capital-at-risk resolution",
    ):
        before.assert_issued_current(ledger)


def test_same_path_complete_valid_rollback_cannot_revalidate_stale_evidence(
    tmp_path,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    s1_bytes = ledger.path.read_bytes()
    s1_evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    _attempt(ledger, plan)
    ledger.mark_unknown(
        "attempt-1",
        reason="transport_timeout",
        observed_at=UNKNOWN_AT,
    )
    s2_bytes = ledger.path.read_bytes()
    s2_evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)
    assert s2_evidence.contingent_unknown_capital == Decimal("10")

    # Restore a complete, parse-valid old prefix at the exact same pathname.
    # Snapshot SHA + pathname identity alone would make s1_evidence look current
    # again. The parent RealExecutionLedger monotonic authority must forbid that.
    ledger.path.write_bytes(s1_bytes)

    with pytest.raises(
        ExecutionCapitalAtRiskStale,
        match="currentness failed during capital-at-risk validation",
    ):
        s1_evidence.assert_issued_current(ledger)

    with pytest.raises(
        ExecutionLedgerIntegrityError,
        match="rollback/monotonic authority check failed",
    ):
        resolve_execution_capital_at_risk(ledger, plan.plan_id)

    # Reinstating the actual committed tip remains restart-resolvable.
    ledger.path.write_bytes(s2_bytes)
    reopened = RealExecutionLedger(ledger.path)
    recovered = resolve_execution_capital_at_risk(reopened, plan.plan_id)
    assert recovered == s2_evidence
    recovered.assert_issued_current(reopened)


def test_byte_identical_clone_cannot_validate_evidence_from_other_ledger_source(
    tmp_path,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    clone_path = tmp_path / "cloned-execution.jsonl"
    clone_path.write_bytes(ledger.path.read_bytes())
    cloned = RealExecutionLedger(clone_path)

    _attempt(ledger, plan, submit=False)

    with pytest.raises(
        ExecutionCapitalAtRiskStale,
        match="changed after capital-at-risk resolution",
    ):
        evidence.assert_issued_current(ledger)

    with pytest.raises(
        ExecutionCapitalAtRiskStale,
        match="different execution ledger source",
    ):
        evidence.assert_issued_current(cloned)


def test_caller_copy_cannot_mint_issued_risk_evidence(tmp_path) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    forged = replace(evidence)

    with pytest.raises(
        ExecutionCapitalAtRiskError,
        match="not canonically issued",
    ):
        forged.assert_issued_current(ledger)



def test_direct_copy_cannot_enable_residual_capacity_authority(tmp_path) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    with pytest.raises(
        ExecutionCapitalAtRiskError,
        match="cannot grant execution, release, or residual-capacity authority",
    ):
        replace(evidence, residual_capacity_authority=True)


def test_direct_copy_cannot_publish_mismatched_aggregate(tmp_path) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    with pytest.raises(
        ExecutionCapitalAtRiskError,
        match="confirmed aggregate does not match attempts",
    ):
        replace(evidence, confirmed_open_capital=Decimal("1"))


def test_generic_non_betfair_provider_fails_closed(tmp_path) -> None:
    ledger, plan = _ledger(
        tmp_path,
        _action(bookmaker_id="other-book", stake="10"),
    )
    _attempt(ledger, plan)

    with pytest.raises(
        ExecutionCapitalAtRiskUnsupported,
        match="generic provider stake",
    ):
        resolve_execution_capital_at_risk(ledger, plan.plan_id)


def test_exact_arithmetic_and_identity_ignore_ambient_decimal_precision(
    tmp_path,
) -> None:
    ledger, plan = _ledger(
        tmp_path,
        _action(stake="987654321.987654321"),
    )
    _attempt(ledger, plan)
    _ack(
        ledger,
        status=AcknowledgementStatus.PARTIAL,
        accepted_stake="123456789.123456789",
        accepted_odds="2.25",
    )

    with localcontext() as context:
        context.prec = 5
        low_precision = resolve_execution_capital_at_risk(
            ledger,
            plan.plan_id,
        )

    with localcontext() as context:
        context.prec = 80
        high_precision = resolve_execution_capital_at_risk(
            ledger,
            plan.plan_id,
        )

    assert low_precision.confirmed_open_capital == high_precision.confirmed_open_capital
    assert (
        low_precision.contingent_unknown_capital
        == high_precision.contingent_unknown_capital
    )
    assert low_precision.evidence_sha256 == high_precision.evidence_sha256


def test_subtraction_preserves_tiny_residual_across_large_exponent_gap() -> None:
    tiny = Decimal("1E-100")
    with localcontext() as context:
        context.prec = 5
        actual = _subtract(Decimal("1"), tiny)

    with localcontext() as context:
        context.prec = 120
        expected = Decimal("1") - tiny

    assert actual == expected
    assert actual != Decimal("1")


def test_extreme_decimal_scale_fails_closed_instead_of_rounding() -> None:
    with pytest.raises(
        ExecutionCapitalAtRiskUnsupported,
        match="scale exceeds exact risk-arithmetic bound",
    ):
        _subtract(Decimal("1E+5000"), Decimal("1"))


def test_multi_action_plan_aggregates_confirmed_and_unknown_gross_commitment(
    tmp_path,
) -> None:
    action_a = _action(action_id="action-a", stake="10")
    action_b = _action(action_id="action-b", stake="20")
    action_c = _action(action_id="action-c", stake="30")
    plan = _plan(action_a, action_b, action_c)
    ledger = RealExecutionLedger(tmp_path / "multi.jsonl")
    ledger.reserve_plan(plan)

    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action_a.action_id,
        attempt_id="attempt-a",
        reserved_at=RESERVED_AT,
    )
    ledger.mark_submitted("attempt-a", submitted_at=SUBMITTED_AT)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action_b.action_id,
        attempt_id="attempt-b",
        reserved_at=RESERVED_AT,
    )
    ledger.mark_submitted("attempt-b", submitted_at=SUBMITTED_AT)

    _ack(
        ledger,
        attempt_id="attempt-a",
        status=AcknowledgementStatus.PARTIAL,
        accepted_stake="4",
        accepted_odds="2",
    )
    ledger.mark_unknown(
        "attempt-b",
        reason="transport_timeout",
        observed_at=UNKNOWN_AT,
    )

    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)

    assert evidence.plan_stale is True
    assert evidence.confirmed_open_capital == Decimal("4")
    assert evidence.contingent_unknown_capital == Decimal("26")
    assert evidence.max_plausible_capital_at_risk == Decimal("30")
    assert {item.action_id for item in evidence.attempts} == {
        "action-a",
        "action-b",
    }
    # action-c was only planned. It never crossed the durable attempt boundary.
    assert all(item.action_id != "action-c" for item in evidence.attempts)


def test_cross_account_attempts_fail_closed_without_common_denomination(
    tmp_path,
) -> None:
    action_a = _action(
        action_id="action-a",
        account_id="account-a",
        stake="10",
    )
    action_b = _action(
        action_id="action-b",
        account_id="account-b",
        stake="20",
    )
    plan = _plan(action_a, action_b)
    ledger = RealExecutionLedger(tmp_path / "cross-account.jsonl")
    ledger.reserve_plan(plan)

    for index, action in enumerate(plan.actions, start=1):
        attempt_id = f"attempt-{index}"
        ledger.begin_attempt(
            plan_id=plan.plan_id,
            action_id=action.action_id,
            attempt_id=attempt_id,
            reserved_at=RESERVED_AT,
        )
        ledger.mark_submitted(attempt_id, submitted_at=SUBMITTED_AT)

    with pytest.raises(
        ExecutionCapitalAtRiskUnsupported,
        match="multiple account scopes|common-denomination",
    ):
        resolve_execution_capital_at_risk(ledger, plan.plan_id)


def test_unsupported_side_fails_closed_instead_of_using_stake_as_risk(
    tmp_path,
) -> None:
    ledger, plan = _ledger(
        tmp_path,
        _action(side="EACH_WAY", stake="10"),
    )
    _attempt(ledger, plan)

    with pytest.raises(
        ExecutionCapitalAtRiskUnsupported,
        match="supports Betfair BACK only",
    ):
        resolve_execution_capital_at_risk(ledger, plan.plan_id)



def test_lay_side_fails_closed_until_canonical_order_economics_is_composed(
    tmp_path,
) -> None:
    ledger, plan = _ledger(
        tmp_path,
        _action(side="LAY", odds="4", stake="10"),
    )
    _attempt(ledger, plan)

    with pytest.raises(
        ExecutionCapitalAtRiskUnsupported,
        match="supports Betfair BACK only",
    ):
        resolve_execution_capital_at_risk(ledger, plan.plan_id)

def test_resolver_rejects_execution_view_alias_rebinding_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    called = False

    def fake_view(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("substituted execution view must not run")

    monkeypatch.setattr(
        capital_risk_module,
        "_VERIFIED_EXECUTION_VIEW",
        fake_view,
    )

    with pytest.raises(
        ExecutionCapitalAtRiskError,
        match="canonical execution-ledger read authority changed",
    ):
        resolve_execution_capital_at_risk(ledger, plan.plan_id)

    assert called is False


def test_resolver_rejects_class_execution_view_replacement_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    called = False

    def fake_view(self, plan_id):
        nonlocal called
        called = True
        raise AssertionError("replaced ledger reader must not run")

    monkeypatch.setattr(
        RealExecutionLedger,
        "verified_execution_view",
        fake_view,
    )

    with pytest.raises(
        ExecutionCapitalAtRiskError,
        match="canonical execution-ledger read authority changed",
    ):
        resolve_execution_capital_at_risk(ledger, plan.plan_id)

    assert called is False


def test_currentness_rejects_verified_snapshot_code_mutation_before_dispatch(
    tmp_path,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)
    reader = RealExecutionLedger.verified_snapshot
    original_code = reader.__code__

    def fake_snapshot(self):
        raise AssertionError("mutated snapshot reader must not run")

    try:
        reader.__code__ = fake_snapshot.__code__
        with pytest.raises(
            ExecutionCapitalAtRiskError,
            match="canonical execution-ledger read authority changed",
        ):
            evidence.assert_issued_current(ledger)
    finally:
        reader.__code__ = original_code

def test_resolver_rejects_verified_execution_view_code_mutation_before_dispatch(
    tmp_path,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    reader = RealExecutionLedger.verified_execution_view
    original_code = reader.__code__

    def fake_view(self, plan_id):
        raise AssertionError("mutated execution-view reader must not run")

    try:
        reader.__code__ = fake_view.__code__
        with pytest.raises(
            ExecutionCapitalAtRiskError,
            match="canonical execution-ledger read authority changed",
        ):
            resolve_execution_capital_at_risk(ledger, plan.plan_id)
    finally:
        reader.__code__ = original_code


def test_currentness_rejects_snapshot_alias_rebinding_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)
    called = False

    def fake_snapshot(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("substituted snapshot alias must not run")

    monkeypatch.setattr(
        capital_risk_module,
        "_VERIFIED_SNAPSHOT",
        fake_snapshot,
    )

    with pytest.raises(
        ExecutionCapitalAtRiskError,
        match="canonical execution-ledger read authority changed",
    ):
        evidence.assert_issued_current(ledger)

    assert called is False


def test_currentness_rejects_snapshot_class_replacement_before_dispatch(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, plan = _ledger(tmp_path, _action(stake="10"))
    evidence = resolve_execution_capital_at_risk(ledger, plan.plan_id)
    called = False

    def fake_snapshot(self):
        nonlocal called
        called = True
        raise AssertionError("replaced snapshot reader must not run")

    monkeypatch.setattr(
        RealExecutionLedger,
        "verified_snapshot",
        fake_snapshot,
    )

    with pytest.raises(
        ExecutionCapitalAtRiskError,
        match="canonical execution-ledger read authority changed",
    ):
        evidence.assert_issued_current(ledger)

    assert called is False

