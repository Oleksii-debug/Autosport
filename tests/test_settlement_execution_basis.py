from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExecutionLedgerIntegrityError,
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


def _ledger(path: Path, *, status=AcknowledgementStatus.ACCEPTED,
            accepted_odds="2.20", accepted_stake="6") -> RealExecutionLedger:
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
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=T0,
        actions=(action,),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at=T0,
    )
    ledger.mark_submitted("attempt-1", submitted_at=T1)
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-1",
            external_receipt_id="receipt-1",
            status=status,
            acknowledged_at=T2,
            accepted_odds=None if status is AcknowledgementStatus.REJECTED else accepted_odds,
            accepted_stake=None if status is AcknowledgementStatus.REJECTED else accepted_stake,
        )
    )
    return ledger


class SettlementExecutionBasisTests(unittest.TestCase):
    def test_uses_accepted_fill_not_requested_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(Path(directory) / "execution.jsonl")
            basis = derive_settlement_execution_basis(ledger, attempt_id="attempt-1")
            self.assertEqual(basis.requested_odds, Decimal("2.00"))
            self.assertEqual(basis.accepted_odds, Decimal("2.20"))
            self.assertEqual(basis.requested_stake, Decimal("10"))
            self.assertEqual(basis.accepted_stake, Decimal("6"))
            self.assertEqual(basis.acknowledgement_status, AcknowledgementStatus.ACCEPTED)
            self.assertFalse(basis.to_dict()["provider_verified"])
            self.assertFalse(basis.to_dict()["real_money_authorized"])
            self.assertFalse(basis.to_dict()["settlement_outcome_authorized"])
            self.assertEqual(verify_settlement_execution_basis(ledger, basis), basis)

    def test_unfinalized_partial_fill_has_no_settlement_basis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(
                Path(directory) / "execution.jsonl",
                status=AcknowledgementStatus.PARTIAL,
                accepted_odds="1.95",
                accepted_stake="3.25",
            )
            with self.assertRaisesRegex(
                SettlementExecutionBasisError,
                "PARTIAL execution requires provider-finalized realization",
            ):
                derive_settlement_execution_basis(
                    ledger,
                    attempt_id="attempt-1",
                )

    def test_rejected_attempt_has_no_settlement_basis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(
                Path(directory) / "execution.jsonl",
                status=AcknowledgementStatus.REJECTED,
            )
            with self.assertRaisesRegex(
                SettlementExecutionBasisError, "rejected execution has no settlement fill basis"
            ):
                derive_settlement_execution_basis(ledger, attempt_id="attempt-1")

    def test_nonterminal_attempt_has_no_settlement_basis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.jsonl"
            ledger = RealExecutionLedger(path)
            action = ExecutionAction(
                action_id="action-1", bookmaker_id="book-a", account_id="account-a",
                event_id="event-a", market_id="market-a", selection_id="selection-a",
                side="BACK", requested_odds="2", requested_stake="10", quote_id="quote-a",
                quote_observed_at=T0, expires_at=EXP,
            )
            ledger.reserve_plan(ExecutionPlan(
                plan_id="plan-1", bookmaker_profile_version="profile-v1",
                decision_id="decision-1", approval_id="approval-1",
                created_at=T0, actions=(action,),
            ))
            ledger.begin_attempt(
                plan_id="plan-1", action_id="action-1",
                attempt_id="attempt-1", reserved_at=T0,
            )
            ledger.mark_submitted("attempt-1", submitted_at=T1)
            with self.assertRaisesRegex(
                SettlementExecutionBasisError, "terminal acknowledgement"
            ):
                derive_settlement_execution_basis(ledger, attempt_id="attempt-1")

    def test_restart_projection_is_identity_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.jsonl"
            first = derive_settlement_execution_basis(_ledger(path), attempt_id="attempt-1")
            restarted = derive_settlement_execution_basis(
                RealExecutionLedger(path), attempt_id="attempt-1"
            )
            self.assertEqual(first, restarted)
            self.assertEqual(first.basis_id, restarted.basis_id)

    def test_basis_identity_survives_unrelated_later_ledger_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.jsonl"
            ledger = _ledger(path)
            first = derive_settlement_execution_basis(ledger, attempt_id="attempt-1")
            later = ExecutionAction(
                action_id="action-2", bookmaker_id="book-b", account_id="account-b",
                event_id="event-b", market_id="market-b", selection_id="selection-b",
                side="BACK", requested_odds="3", requested_stake="4", quote_id="quote-b",
                quote_observed_at=T0, expires_at=EXP,
            )
            ledger.reserve_plan(ExecutionPlan(
                plan_id="plan-2", bookmaker_profile_version="profile-v2",
                decision_id="decision-2", approval_id="approval-2",
                created_at=T0, actions=(later,),
            ))
            after_append = derive_settlement_execution_basis(
                ledger, attempt_id="attempt-1"
            )
            self.assertEqual(first, after_append)
            self.assertEqual(first.basis_id, after_append.basis_id)

    def test_forged_basis_copy_fails_canonical_reverification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = _ledger(Path(directory) / "execution.jsonl")
            basis = derive_settlement_execution_basis(ledger, attempt_id="attempt-1")
            forged = replace(basis, accepted_odds=Decimal("99"))
            with self.assertRaisesRegex(
                SettlementExecutionBasisError,
                "does not match canonical durable execution",
            ):
                verify_settlement_execution_basis(ledger, forged)

    def test_tampered_ledger_fails_before_basis_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.jsonl"
            _ledger(path)
            raw = path.read_text(encoding="utf-8")
            path.write_text(
                raw.replace('"accepted_odds":"2.2"', '"accepted_odds":"9.9"'),
                encoding="utf-8",
            )
            with self.assertRaises(ExecutionLedgerIntegrityError):
                derive_settlement_execution_basis(
                    RealExecutionLedger(path), attempt_id="attempt-1"
                )


if __name__ == "__main__":
    unittest.main()
