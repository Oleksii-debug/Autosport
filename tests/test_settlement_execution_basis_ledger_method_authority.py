from pathlib import Path

import pytest

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)
from autosport.settlement_execution_basis import (
    SettlementExecutionBasisError,
    derive_settlement_execution_basis,
    verify_settlement_execution_basis,
)


T0 = "2026-09-21T09:00:00+00:00"
T1 = "2026-09-21T09:00:01+00:00"
T2 = "2026-09-21T09:00:02+00:00"
EXP = "2026-09-21T09:10:00+00:00"


def _donor_ledger(path: Path) -> RealExecutionLedger:
    ledger = RealExecutionLedger(path)
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="book-a",
        account_id="account-a",
        event_id="event-a",
        market_id="market-a",
        selection_id="selection-a",
        side="BACK",
        requested_odds="2.00",
        requested_stake="10",
        quote_id="quote-a",
        quote_observed_at=T0,
        expires_at=EXP,
    )
    ledger.reserve_plan(
        ExecutionPlan(
            plan_id="plan-1",
            bookmaker_profile_version="profile-v1",
            decision_id="decision-1",
            approval_id="approval-1",
            created_at=T0,
            actions=(action,),
        )
    )
    ledger.begin_attempt(
        plan_id="plan-1",
        action_id="action-1",
        attempt_id="attempt-1",
        reserved_at=T0,
    )
    ledger.mark_submitted("attempt-1", submitted_at=T1)
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-1",
            external_receipt_id="receipt-1",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at=T2,
            accepted_odds="2.20",
            accepted_stake="6",
        )
    )
    return ledger


def _rebound_empty_target(tmp_path):
    donor = _donor_ledger(tmp_path / "donor.jsonl")
    donor_snapshot = donor.verified_snapshot()
    target_path = tmp_path / "empty-target.jsonl"
    target = RealExecutionLedger(target_path)

    def rebound_snapshot():
        return donor_snapshot

    # Refusing per-instance method replacement is itself a valid fail-closed
    # repair. Otherwise the rebound bytes must not become authority for target.
    try:
        target.verified_snapshot = rebound_snapshot  # type: ignore[method-assign]
    except (AttributeError, TypeError):
        return None, target_path, donor

    assert type(target) is RealExecutionLedger
    assert not target_path.exists()
    return target, target_path, donor


def test_empty_exact_ledger_cannot_derive_basis_from_rebound_snapshot(tmp_path) -> None:
    target, target_path, _ = _rebound_empty_target(tmp_path)
    if target is None:
        return

    with pytest.raises((SettlementExecutionBasisError, TypeError)):
        derive_settlement_execution_basis(target, attempt_id="attempt-1")

    assert not target_path.exists()


def test_empty_exact_ledger_cannot_verify_donor_basis_via_rebound_snapshot(
    tmp_path,
) -> None:
    target, target_path, donor = _rebound_empty_target(tmp_path)
    if target is None:
        return
    donor_basis = derive_settlement_execution_basis(donor, attempt_id="attempt-1")

    with pytest.raises((SettlementExecutionBasisError, TypeError)):
        verify_settlement_execution_basis(target, donor_basis)

    assert not target_path.exists()


def test_exact_ledger_parse_shadow_is_rejected_before_fake_parser(tmp_path) -> None:
    target = RealExecutionLedger(tmp_path / "empty-target.jsonl")
    calls: list[str] = []

    def forged_parse(_raw: bytes):
        calls.append("forged-parse")
        return []

    target._parse = forged_parse  # type: ignore[method-assign]

    with pytest.raises(
        SettlementExecutionBasisError,
        match="instance read authority was rebound",
    ):
        derive_settlement_execution_basis(target, attempt_id="attempt-1")

    assert calls == []


@pytest.mark.parametrize("method_name", ["verified_snapshot", "_parse"])
def test_runtime_ledger_class_read_rebind_is_rejected_before_fake_read(
    tmp_path,
    monkeypatch,
    method_name: str,
) -> None:
    target = RealExecutionLedger(tmp_path / "empty-target.jsonl")
    calls: list[str] = []

    if method_name == "verified_snapshot":
        def forged(_self):
            calls.append("forged-snapshot")
            raise AssertionError("forged snapshot must not run")
    else:
        def forged(_cls, _raw):
            calls.append("forged-parse")
            raise AssertionError("forged parser must not run")

    monkeypatch.setattr(RealExecutionLedger, method_name, forged)

    with pytest.raises(
        SettlementExecutionBasisError,
        match="executable read authority was rebound",
    ):
        derive_settlement_execution_basis(target, attempt_id="attempt-1")

    assert calls == []
