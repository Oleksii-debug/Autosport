import json
import tempfile
import unittest
from pathlib import Path

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    ExecutionAction,
    ExecutionIdentityConflict,
    ExecutionLedgerBusyError,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    ExecutionStateError,
    ExternalAcknowledgement,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


TS = "2026-09-17T19:28:00+00:00"


def action(action_id: str = "a1") -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="market-1",
        selection_id=f"selection-{action_id}",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id=f"quote-{action_id}",
        quote_observed_at=TS,
        expires_at="2026-09-17T19:29:00+00:00",
    )


def plan(*actions: ExecutionAction, plan_id: str = "p1") -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        bookmaker_profile_version="profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=TS,
        actions=tuple(actions or (action(),)),
    )


class RealExecutionLedgerTests(unittest.TestCase):
    def test_exact_plan_reservation_is_idempotent_but_conflict_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            first = plan(action())
            self.assertEqual(ledger.reserve_plan(first), first.fingerprint)
            self.assertEqual(ledger.reserve_plan(first), first.fingerprint)
            self.assertEqual(ledger.verify_integrity(), 1)

            conflict = ExecutionPlan(
                plan_id="p1",
                bookmaker_profile_version="profile-v2",
                decision_id="decision-1",
                approval_id="approval-1",
                created_at=TS,
                actions=(action(),),
            )
            with self.assertRaises(ExecutionIdentityConflict):
                ledger.reserve_plan(conflict)

    def test_reserve_before_act_survives_restart_as_unknown_and_blocks_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(plan_id="p1", action_id="a1", attempt_id="try-1")
            ledger.mark_submitted("try-1")
            restarted = RealExecutionLedger(path)

            self.assertEqual(restarted.recover_uncertain(), ("try-1",))
            self.assertEqual(restarted.attempt_state("try-1"), AttemptState.UNKNOWN)
            self.assertFalse(restarted.can_retry_action(plan_id="p1", action_id="a1"))
            with self.assertRaises(ExecutionStateError):
                restarted.begin_attempt(plan_id="p1", action_id="a1", attempt_id="try-2")

    def test_unknown_can_retry_only_after_external_not_found_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(plan_id="p1", action_id="a1", attempt_id="try-1")
            ledger.mark_unknown("try-1", reason="timeout")

            with self.assertRaises(ExecutionStateError):
                ledger.reconcile_not_found(
                    ReconciliationSnapshot(
                        attempt_id="missing",
                        evidence_id="readback-0",
                        observed_at=TS,
                        external_effect_found=False,
                        source="provider-open-and-cleared-orders",
                    )
                )

            ledger.reconcile_not_found(
                ReconciliationSnapshot(
                    attempt_id="try-1",
                    evidence_id="readback-1",
                    observed_at=TS,
                    external_effect_found=False,
                    source="provider-open-and-cleared-orders",
                )
            )
            self.assertEqual(
                ledger.attempt_state("try-1"), AttemptState.RECONCILED_NOT_FOUND
            )
            self.assertTrue(ledger.can_retry_action(plan_id="p1", action_id="a1"))
            second = ledger.begin_attempt(
                plan_id="p1", action_id="a1", attempt_id="try-2"
            )
            self.assertEqual(second.attempt_id, "try-2")

    def test_external_receipt_is_globally_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action(), plan_id="p1"))
            ledger.reserve_plan(plan(action(), plan_id="p2"))
            ledger.begin_attempt(plan_id="p1", action_id="a1", attempt_id="try-1")
            ledger.begin_attempt(plan_id="p2", action_id="a1", attempt_id="try-2")
            ledger.acknowledge(
                ExternalAcknowledgement(
                    attempt_id="try-1",
                    external_receipt_id="receipt-1",
                    status=AcknowledgementStatus.ACCEPTED,
                    acknowledged_at=TS,
                    accepted_odds="2.5",
                    accepted_stake="10",
                )
            )
            with self.assertRaises(ExecutionIdentityConflict):
                ledger.acknowledge(
                    ExternalAcknowledgement(
                        attempt_id="try-2",
                        external_receipt_id="receipt-1",
                        status=AcknowledgementStatus.ACCEPTED,
                        acknowledged_at=TS,
                        accepted_odds="2.5",
                        accepted_stake="10",
                    )
                )

    def test_multi_action_ack_invalidates_remaining_old_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action("a1"), action("a2")))
            ledger.begin_attempt(plan_id="p1", action_id="a1", attempt_id="try-1")
            ledger.acknowledge(
                ExternalAcknowledgement(
                    attempt_id="try-1",
                    external_receipt_id="receipt-1",
                    status=AcknowledgementStatus.PARTIAL,
                    acknowledged_at=TS,
                    accepted_odds="2.4",
                    accepted_stake="5",
                )
            )
            self.assertTrue(ledger.plan_is_stale("p1"))
            self.assertFalse(ledger.can_retry_action(plan_id="p1", action_id="a2"))
            with self.assertRaisesRegex(ExecutionStateError, "stale"):
                ledger.begin_attempt(plan_id="p1", action_id="a2", attempt_id="try-2")

    def test_same_ack_is_idempotent_but_conflicting_ack_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(plan_id="p1", action_id="a1", attempt_id="try-1")
            ack = ExternalAcknowledgement(
                attempt_id="try-1",
                external_receipt_id="receipt-1",
                status=AcknowledgementStatus.ACCEPTED,
                acknowledged_at=TS,
                accepted_odds="2.5",
                accepted_stake="10",
            )
            ledger.acknowledge(ack)
            event_count = ledger.verify_integrity()
            ledger.acknowledge(ack)
            self.assertEqual(ledger.verify_integrity(), event_count)

            with self.assertRaises(ExecutionIdentityConflict):
                ledger.acknowledge(
                    ExternalAcknowledgement(
                        attempt_id="try-1",
                        external_receipt_id="receipt-2",
                        status=AcknowledgementStatus.ACCEPTED,
                        acknowledged_at=TS,
                        accepted_odds="2.5",
                        accepted_stake="10",
                    )
                )

    def test_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            envelope = json.loads(path.read_text(encoding="utf-8").strip())
            envelope["event"]["payload"]["plan"]["approval_id"] = "forged"
            path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ExecutionLedgerIntegrityError, "SHA-256 mismatch"):
                ledger.verify_integrity()

    def test_existing_writer_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger._lock_path.write_text("simulated active writer", encoding="utf-8")
            with self.assertRaises(ExecutionLedgerBusyError):
                ledger.reserve_plan(plan(action()))

    def test_rejected_ack_cannot_claim_accepted_money(self):
        with self.assertRaises(ValueError):
            ExternalAcknowledgement(
                attempt_id="try-1",
                external_receipt_id="receipt-1",
                status=AcknowledgementStatus.REJECTED,
                acknowledged_at=TS,
                accepted_odds="2.5",
                accepted_stake="10",
            )


if __name__ == "__main__":
    unittest.main()
