from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    ExecutionAction,
    ExecutionIdentityConflict,
    ExecutionPlan,
    ExecutionStateError,
    ExternalAcknowledgement,
    ExternalEffectReconciliation,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


TS = "2026-09-21T08:00:00+00:00"
RESERVED_AT = "2026-09-21T08:00:10+00:00"
SUBMITTED_AT = "2026-09-21T08:00:20+00:00"
UNKNOWN_AT = "2026-09-21T08:00:30+00:00"
RECONCILED_AT = "2026-09-21T08:00:40+00:00"
SECOND_RECONCILED_AT = "2026-09-21T08:00:50+00:00"
RETRY_RESERVED_AT = "2026-09-21T08:01:00+00:00"
EXPIRES_AT = "2026-09-21T08:05:00+00:00"


def _action(action_id: str = "a1") -> ExecutionAction:
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
        expires_at=EXPIRES_AT,
    )


def _plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="p1",
        bookmaker_profile_version="profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=TS,
        actions=tuple(actions or (_action(),)),
    )


def _ack(
    status: AcknowledgementStatus,
    *,
    attempt_id: str = "try-1",
    receipt_id: str = "receipt-1",
    acknowledged_at: str = RECONCILED_AT,
    reconciliation_evidence_id: str | None = None,
) -> ExternalAcknowledgement:
    accepted_odds = None
    accepted_stake = None
    if status is AcknowledgementStatus.ACCEPTED:
        accepted_odds = "2.40"
        accepted_stake = "10.00"
    elif status is AcknowledgementStatus.PARTIAL:
        accepted_odds = "2.40"
        accepted_stake = "5.00"
    return ExternalAcknowledgement(
        attempt_id=attempt_id,
        external_receipt_id=receipt_id,
        status=status,
        acknowledged_at=acknowledged_at,
        accepted_odds=accepted_odds,
        accepted_stake=accepted_stake,
        reconciliation_evidence_id=reconciliation_evidence_id,
    )


class RealExecutionAcceptanceStateMatrixTests(unittest.TestCase):
    def _submitted_ledger(
        self, path: Path, *, actions: tuple[ExecutionAction, ...] | None = None
    ) -> RealExecutionLedger:
        ledger = RealExecutionLedger(path)
        ledger.reserve_plan(_plan(*(actions or (_action(),))))
        ledger.begin_attempt(
            plan_id="p1",
            action_id="a1",
            attempt_id="try-1",
            reserved_at=RESERVED_AT,
        )
        ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
        return ledger

    def test_submitted_terminal_ack_matrix_survives_restart_and_blocks_retry(self) -> None:
        matrix = (
            (AcknowledgementStatus.ACCEPTED, AttemptState.ACCEPTED),
            (AcknowledgementStatus.PARTIAL, AttemptState.PARTIAL),
            (AcknowledgementStatus.REJECTED, AttemptState.REJECTED),
        )
        for acknowledgement_status, expected_state in matrix:
            with self.subTest(status=acknowledgement_status.value):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "real.jsonl"
                    ledger = self._submitted_ledger(path)
                    acknowledgement = _ack(acknowledgement_status)

                    ledger.acknowledge(acknowledgement)
                    self.assertEqual(ledger.attempt_state("try-1"), expected_state)
                    self.assertFalse(
                        ledger.can_retry_action(plan_id="p1", action_id="a1")
                    )

                    event_count = ledger.verify_integrity()
                    ledger.acknowledge(acknowledgement)
                    self.assertEqual(ledger.verify_integrity(), event_count)

                    restarted = RealExecutionLedger(path)
                    self.assertEqual(restarted.verify_integrity(), event_count)
                    self.assertEqual(restarted.attempt_state("try-1"), expected_state)
                    self.assertFalse(
                        restarted.can_retry_action(plan_id="p1", action_id="a1")
                    )
                    self.assertEqual(restarted.recover_uncertain(), ())

                    with self.assertRaises(ExecutionStateError):
                        restarted.mark_unknown(
                            "try-1",
                            reason="late-timeout",
                            observed_at=SECOND_RECONCILED_AT,
                        )
                    with self.assertRaises(ExecutionStateError):
                        restarted.mark_submitted(
                            "try-1",
                            submitted_at=SECOND_RECONCILED_AT,
                        )
                    with self.assertRaises(ExecutionStateError):
                        restarted.reconcile_found(
                            ExternalEffectReconciliation(
                                attempt_id="try-1",
                                evidence_id="late-found",
                                external_receipt_id="receipt-1",
                                observed_at=SECOND_RECONCILED_AT,
                                source="provider-readback",
                            )
                        )
                    with self.assertRaises(ExecutionStateError):
                        restarted.reconcile_not_found(
                            ReconciliationSnapshot(
                                attempt_id="try-1",
                                evidence_id="late-not-found",
                                observed_at=SECOND_RECONCILED_AT,
                                external_effect_found=False,
                                source="provider-readback",
                            )
                        )

    def test_terminal_ack_payload_cannot_be_rewritten(self) -> None:
        replacements = {
            AcknowledgementStatus.ACCEPTED: AcknowledgementStatus.PARTIAL,
            AcknowledgementStatus.PARTIAL: AcknowledgementStatus.REJECTED,
            AcknowledgementStatus.REJECTED: AcknowledgementStatus.ACCEPTED,
        }
        for initial_status, replacement_status in replacements.items():
            with self.subTest(
                initial=initial_status.value,
                replacement=replacement_status.value,
            ):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "real.jsonl"
                    ledger = self._submitted_ledger(path)
                    ledger.acknowledge(_ack(initial_status))

                    with self.assertRaises(ExecutionIdentityConflict):
                        ledger.acknowledge(_ack(replacement_status))

                    restarted = RealExecutionLedger(path)
                    self.assertEqual(
                        restarted.attempt_state("try-1"),
                        AttemptState(initial_status.value),
                    )

    def test_unknown_not_found_remains_non_authoritative_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = self._submitted_ledger(path)
            ledger.mark_unknown(
                "try-1",
                reason="provider-timeout",
                observed_at=UNKNOWN_AT,
            )

            self.assertEqual(ledger.attempt_state("try-1"), AttemptState.UNKNOWN)
            self.assertFalse(ledger.can_retry_action(plan_id="p1", action_id="a1"))

            with self.assertRaises(ExecutionStateError):
                ledger.reconcile_not_found(
                    ReconciliationSnapshot(
                        attempt_id="try-1",
                        evidence_id="too-early",
                        observed_at=UNKNOWN_AT,
                        external_effect_found=False,
                        source="provider-readback",
                    )
                )

            ledger.reconcile_not_found(
                ReconciliationSnapshot(
                    attempt_id="try-1",
                    evidence_id="readback-1",
                    observed_at=RECONCILED_AT,
                    external_effect_found=False,
                    source="provider-readback",
                )
            )
            self.assertEqual(
                ledger.attempt_state("try-1"),
                AttemptState.RECONCILED_NOT_FOUND,
            )
            self.assertFalse(ledger.can_retry_action(plan_id="p1", action_id="a1"))

            restarted = RealExecutionLedger(path)
            self.assertEqual(
                restarted.attempt_state("try-1"),
                AttemptState.RECONCILED_NOT_FOUND,
            )
            self.assertFalse(
                restarted.can_retry_action(plan_id="p1", action_id="a1")
            )
            with self.assertRaisesRegex(
                ExecutionStateError, "product-issued no-effect authority"
            ):
                restarted.begin_attempt(
                    plan_id="p1",
                    action_id="a1",
                    attempt_id="try-2",
                    reserved_at=RETRY_RESERVED_AT,
                )

    def test_unknown_positive_reconciliation_requires_matching_evidence_before_terminal_ack(
        self,
    ) -> None:
        matrix = (
            (AcknowledgementStatus.ACCEPTED, AttemptState.ACCEPTED),
            (AcknowledgementStatus.PARTIAL, AttemptState.PARTIAL),
            (AcknowledgementStatus.REJECTED, AttemptState.REJECTED),
        )
        for acknowledgement_status, expected_state in matrix:
            with self.subTest(status=acknowledgement_status.value):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "real.jsonl"
                    ledger = self._submitted_ledger(path)
                    ledger.mark_unknown(
                        "try-1",
                        reason="provider-timeout",
                        observed_at=UNKNOWN_AT,
                    )

                    with self.assertRaises(ExecutionStateError):
                        ledger.acknowledge(
                            _ack(
                                acknowledgement_status,
                                acknowledged_at=SECOND_RECONCILED_AT,
                            )
                        )

                    ledger.reconcile_found(
                        ExternalEffectReconciliation(
                            attempt_id="try-1",
                            evidence_id="readback-1",
                            external_receipt_id="receipt-1",
                            observed_at=RECONCILED_AT,
                            source="provider-readback",
                        )
                    )
                    ledger.acknowledge(
                        _ack(
                            acknowledgement_status,
                            acknowledged_at=SECOND_RECONCILED_AT,
                            reconciliation_evidence_id="readback-1",
                        )
                    )

                    restarted = RealExecutionLedger(path)
                    self.assertEqual(
                        restarted.attempt_state("try-1"),
                        expected_state,
                    )
                    self.assertFalse(
                        restarted.can_retry_action(plan_id="p1", action_id="a1")
                    )

    def test_any_terminal_ack_stales_multi_action_plan_and_blocks_remaining_action(
        self,
    ) -> None:
        matrix = (
            AcknowledgementStatus.ACCEPTED,
            AcknowledgementStatus.PARTIAL,
            AcknowledgementStatus.REJECTED,
        )
        for acknowledgement_status in matrix:
            with self.subTest(status=acknowledgement_status.value):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "real.jsonl"
                    ledger = self._submitted_ledger(
                        path,
                        actions=(_action("a1"), _action("a2")),
                    )
                    ledger.acknowledge(_ack(acknowledgement_status))

                    self.assertTrue(ledger.plan_is_stale("p1"))
                    self.assertFalse(
                        ledger.can_retry_action(plan_id="p1", action_id="a2")
                    )
                    with self.assertRaisesRegex(ExecutionStateError, "stale"):
                        ledger.begin_attempt(
                            plan_id="p1",
                            action_id="a2",
                            attempt_id="try-2",
                            reserved_at=RETRY_RESERVED_AT,
                        )

                    restarted = RealExecutionLedger(path)
                    self.assertTrue(restarted.plan_is_stale("p1"))
                    self.assertFalse(
                        restarted.can_retry_action(plan_id="p1", action_id="a2")
                    )


if __name__ == "__main__":
    unittest.main()
