import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    ExecutionAction,
    ExecutionIdentityConflict,
    ExecutionLedgerBusyError,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    ExecutionStateError,
    EventType,
    ExternalAcknowledgement,
    ExternalEffectReconciliation,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


TS = "2026-09-17T19:28:00+00:00"
RESERVED_AT = "2026-09-17T19:28:10+00:00"
SUBMITTED_AT = "2026-09-17T19:28:15+00:00"
UNKNOWN_AT = "2026-09-17T19:28:20+00:00"
RECONCILED_AT = "2026-09-17T19:28:30+00:00"
SECOND_RECONCILED_AT = "2026-09-17T19:28:35+00:00"
RETRY_RESERVED_AT = "2026-09-17T19:28:40+00:00"
EXPIRES_AT = "2026-09-17T19:29:00+00:00"


def action(
    action_id: str = "a1",
    bookmaker_id: str = "betfair",
    account_id: str = "acct-1",
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id=bookmaker_id,
        account_id=account_id,
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
    def test_first_create_orders_file_sync_before_path_publication_barrier(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            current = plan(action())
            calls: list[str] = []
            real_fsync = os.fsync

            def tracked_file_sync(fd: int) -> None:
                calls.append("file")
                real_fsync(fd)

            def tracked_directory_sync() -> None:
                self.assertTrue(path.exists())
                calls.append("directory")

            with (
                patch(
                    "autosport.real_execution_ledger.os.fsync",
                    side_effect=tracked_file_sync,
                ),
                patch.object(
                    RealExecutionLedger,
                    "_sync_parent_directory",
                    side_effect=tracked_directory_sync,
                ),
            ):
                self.assertEqual(
                    ledger.reserve_plan(current),
                    current.fingerprint,
                )

            self.assertEqual(calls, ["file", "directory"])

    def test_failed_first_create_publish_barrier_never_returns_reservation_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            current = plan(action())

            with patch.object(
                RealExecutionLedger,
                "_sync_parent_directory",
                side_effect=(OSError("injected directory fsync failure"), None),
            ) as sync_parent:
                with self.assertRaisesRegex(
                    ExecutionLedgerIntegrityError,
                    "durability barrier failed",
                ):
                    ledger.reserve_plan(current)

                # The first envelope may be visible after a failed durability
                # barrier, but no reservation authority was returned. A retry
                # must re-establish the barrier before treating it as durable.
                self.assertTrue(path.exists())
                self.assertEqual(
                    ledger.reserve_plan(current),
                    current.fingerprint,
                )
                self.assertEqual(sync_parent.call_count, 2)

            self.assertEqual(ledger.verify_integrity(), 1)

    def test_exact_plan_reservation_idempotent_but_conflict_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            current = plan(action())
            self.assertEqual(ledger.reserve_plan(current), current.fingerprint)
            self.assertEqual(ledger.reserve_plan(current), current.fingerprint)
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

    def test_reserved_attempt_cannot_be_acknowledged_without_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )

            with self.assertRaisesRegex(
                ExecutionStateError, "durable submission"
            ):
                ledger.acknowledge(
                    ExternalAcknowledgement(
                        attempt_id="try-1",
                        external_receipt_id="r1",
                        status=AcknowledgementStatus.ACCEPTED,
                        acknowledged_at=RECONCILED_AT,
                        accepted_odds="2.5",
                        accepted_stake="5",
                    )
                )
            self.assertEqual(
                ledger.attempt_state("try-1"),
                AttemptState.RESERVED,
            )

    def test_restart_rejects_hash_valid_reserved_to_ack_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.acknowledge(
                ExternalAcknowledgement(
                    attempt_id="try-1",
                    external_receipt_id="r1",
                    status=AcknowledgementStatus.ACCEPTED,
                    acknowledged_at=RECONCILED_AT,
                    accepted_odds="2.5",
                    accepted_stake="5",
                )
            )

            lines = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            lines = [
                envelope
                for envelope in lines
                if envelope["event"]["event_type"]
                != EventType.ATTEMPT_SUBMITTED.value
            ]
            path.write_text(
                "\n".join(
                    json.dumps(
                        envelope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    for envelope in lines
                )
                + "\n",
                encoding="utf-8",
            )

            restarted = RealExecutionLedger(path)
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "acknowledgement from invalid state",
            ):
                restarted.verify_integrity()

    def test_restart_promotes_unresolved_attempt_to_unknown_and_blocks_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)

            restarted = RealExecutionLedger(path)
            self.assertEqual(restarted.recover_uncertain(), ("try-1",))
            self.assertEqual(restarted.attempt_state("try-1"), AttemptState.UNKNOWN)
            self.assertFalse(restarted.can_retry_action(plan_id="p1", action_id="a1"))
            with self.assertRaises(ExecutionStateError):
                restarted.begin_attempt(
                    plan_id="p1",
                    action_id="a1",
                    attempt_id="try-2",
                    reserved_at=RETRY_RESERVED_AT,
                )

    def test_recover_uncertain_clock_rollback_before_reservation_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            before = path.read_bytes()

            with patch(
                "autosport.real_execution_ledger._now",
                return_value=TS,
            ):
                with self.assertRaisesRegex(
                    ExecutionStateError,
                    "recovery clock precedes attempt causal boundary",
                ):
                    ledger.recover_uncertain()

            self.assertEqual(path.read_bytes(), before)
            restarted = RealExecutionLedger(path)
            self.assertEqual(restarted.verify_integrity(), 2)
            self.assertEqual(
                restarted.attempt_state("try-1"),
                AttemptState.RESERVED,
            )

    def test_recover_uncertain_clock_rollback_before_submission_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            before = path.read_bytes()

            with patch(
                "autosport.real_execution_ledger._now",
                return_value=RESERVED_AT,
            ):
                with self.assertRaisesRegex(
                    ExecutionStateError,
                    "recovery clock precedes attempt causal boundary",
                ):
                    ledger.recover_uncertain()

            self.assertEqual(path.read_bytes(), before)
            restarted = RealExecutionLedger(path)
            self.assertEqual(restarted.verify_integrity(), 3)
            self.assertEqual(
                restarted.attempt_state("try-1"),
                AttemptState.SUBMITTED,
            )

    def test_mark_unknown_cannot_precede_bound_provider_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.bind_provider_evidence(
                attempt_id="try-1",
                evidence_id="e" * 64,
                observed_at=UNKNOWN_AT,
                source="provider-readback",
            )
            before = path.read_bytes()

            with self.assertRaisesRegex(
                ExecutionStateError,
                "UNKNOWN observed_at cannot precede attempt causal boundary",
            ):
                ledger.mark_unknown(
                    "try-1",
                    reason="provider-timeout",
                    observed_at="2026-09-17T19:28:17+00:00",
                )

            self.assertEqual(path.read_bytes(), before)
            restarted = RealExecutionLedger(path)
            self.assertEqual(restarted.verify_integrity(), 4)
            self.assertEqual(
                restarted.attempt_state("try-1"),
                AttemptState.SUBMITTED,
            )
            self.assertEqual(
                restarted.provider_evidence_binding("try-1"),
                {
                    "evidence_id": "e" * 64,
                    "observed_at": UNKNOWN_AT,
                    "source": "provider-readback",
                },
            )

    def test_recover_uncertain_clock_rollback_before_provider_evidence_does_not_mutate(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.bind_provider_evidence(
                attempt_id="try-1",
                evidence_id="f" * 64,
                observed_at=UNKNOWN_AT,
                source="provider-readback",
            )
            before = path.read_bytes()

            with patch(
                "autosport.real_execution_ledger._now",
                return_value="2026-09-17T19:28:17+00:00",
            ):
                with self.assertRaisesRegex(
                    ExecutionStateError,
                    "recovery clock precedes attempt causal boundary",
                ):
                    ledger.recover_uncertain()

            self.assertEqual(path.read_bytes(), before)
            restarted = RealExecutionLedger(path)
            self.assertEqual(restarted.verify_integrity(), 4)
            self.assertEqual(
                restarted.attempt_state("try-1"),
                AttemptState.SUBMITTED,
            )
            self.assertEqual(
                restarted.provider_evidence_binding("try-1"),
                {
                    "evidence_id": "f" * 64,
                    "observed_at": UNKNOWN_AT,
                    "source": "provider-readback",
                },
            )

    def test_provider_evidence_blocks_backdated_found_reconciliation_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )
            ledger.bind_provider_evidence(
                attempt_id="try-1",
                evidence_id="e" * 64,
                observed_at=SECOND_RECONCILED_AT,
                source="provider-readback",
            )
            reconciliation = ExternalEffectReconciliation(
                attempt_id="try-1",
                evidence_id="found-before-provider-evidence",
                external_receipt_id="r1",
                observed_at=RECONCILED_AT,
                source="provider-readback",
            )
            before = path.read_bytes()

            with self.assertRaisesRegex(
                ExecutionStateError,
                "positive reconciliation evidence must be newer",
            ):
                ledger.reconcile_found(reconciliation)

            self.assertEqual(path.read_bytes(), before)
            ledger._append(
                EventType.RECONCILED_FOUND,
                "p1",
                "a1",
                "try-1",
                reconciliation.to_dict(),
            )
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "found reconciliation is not newer than attempt causal boundary",
            ):
                RealExecutionLedger(path).verify_integrity()

    def test_provider_evidence_blocks_backdated_not_found_reconciliation_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )
            ledger.bind_provider_evidence(
                attempt_id="try-1",
                evidence_id="e" * 64,
                observed_at=SECOND_RECONCILED_AT,
                source="provider-readback",
            )
            snapshot = ReconciliationSnapshot(
                attempt_id="try-1",
                evidence_id="absence-before-provider-evidence",
                observed_at=RECONCILED_AT,
                external_effect_found=False,
                source="provider-readback",
            )
            before = path.read_bytes()

            with self.assertRaisesRegex(
                ExecutionStateError,
                "external not-found evidence must be newer",
            ):
                ledger.reconcile_not_found(snapshot)

            self.assertEqual(path.read_bytes(), before)
            ledger._append(
                EventType.RECONCILED_NOT_FOUND,
                "p1",
                "a1",
                "try-1",
                snapshot.to_dict(),
            )
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "not-found reconciliation is not newer than attempt causal boundary",
            ):
                RealExecutionLedger(path).verify_integrity()

    def test_ack_after_later_provider_evidence_preserves_valid_prior_found_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )
            reconciliation = ExternalEffectReconciliation(
                attempt_id="try-1",
                evidence_id="found-before-later-provider-evidence",
                external_receipt_id="r1",
                observed_at=RECONCILED_AT,
                source="provider-readback",
            )
            ledger.reconcile_found(reconciliation)
            ledger.bind_provider_evidence(
                attempt_id="try-1",
                evidence_id="e" * 64,
                observed_at=SECOND_RECONCILED_AT,
                source="provider-readback-later",
            )
            ledger.acknowledge(
                ExternalAcknowledgement(
                    attempt_id="try-1",
                    external_receipt_id="r1",
                    status=AcknowledgementStatus.ACCEPTED,
                    acknowledged_at=RETRY_RESERVED_AT,
                    accepted_odds="2.5",
                    accepted_stake="5",
                    reconciliation_evidence_id=reconciliation.evidence_id,
                )
            )

            restarted = RealExecutionLedger(path)
            self.assertEqual(restarted.verify_integrity(), 7)
            self.assertEqual(
                restarted.attempt_state("try-1"),
                AttemptState.ACCEPTED,
            )

    def test_provider_evidence_blocks_backdated_acknowledgement_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.bind_provider_evidence(
                attempt_id="try-1",
                evidence_id="e" * 64,
                observed_at=SECOND_RECONCILED_AT,
                source="provider-readback",
            )
            acknowledgement = ExternalAcknowledgement(
                attempt_id="try-1",
                external_receipt_id="r1",
                status=AcknowledgementStatus.ACCEPTED,
                acknowledged_at=RECONCILED_AT,
                accepted_odds="2.5",
                accepted_stake="5",
            )
            before = path.read_bytes()

            with self.assertRaisesRegex(
                ExecutionStateError,
                "acknowledgement precedes attempt causal boundary",
            ):
                ledger.acknowledge(acknowledgement)

            self.assertEqual(path.read_bytes(), before)
            ledger._append(
                EventType.EXTERNAL_ACKNOWLEDGEMENT,
                "p1",
                "a1",
                "try-1",
                acknowledgement.to_dict(),
            )
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "acknowledgement precedes attempt causal boundary",
            ):
                RealExecutionLedger(path).verify_integrity()

    def test_restart_rejects_multiple_provider_evidence_bindings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.bind_provider_evidence(
                attempt_id="try-1",
                evidence_id="e" * 64,
                observed_at=UNKNOWN_AT,
                source="provider-readback",
            )
            ledger._append(
                EventType.PROVIDER_EVIDENCE_BOUND,
                "p1",
                "a1",
                "try-1",
                {
                    "evidence_id": "f" * 64,
                    "observed_at": RECONCILED_AT,
                    "source": "provider-readback-later",
                },
            )

            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "multiple provider evidence bindings",
            ):
                RealExecutionLedger(path).verify_integrity()

    def test_recover_uncertain_clock_rollback_preflights_all_attempts_before_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action("a1"), action("a2")))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a2",
                attempt_id="try-2",
                reserved_at=RETRY_RESERVED_AT,
            )
            before = path.read_bytes()

            with patch(
                "autosport.real_execution_ledger._now",
                return_value=UNKNOWN_AT,
            ):
                with self.assertRaisesRegex(
                    ExecutionStateError,
                    "recovery clock precedes attempt causal boundary",
                ):
                    ledger.recover_uncertain()

            self.assertEqual(path.read_bytes(), before)
            restarted = RealExecutionLedger(path)
            self.assertEqual(restarted.verify_integrity(), 4)
            self.assertEqual(
                restarted.attempt_state("try-1"),
                AttemptState.SUBMITTED,
            )
            self.assertEqual(
                restarted.attempt_state("try-2"),
                AttemptState.RESERVED,
            )

    def test_recover_uncertain_multi_attempt_valid_clock_promotes_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action("a1"), action("a2")))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a2",
                attempt_id="try-2",
                reserved_at=RESERVED_AT,
            )

            with patch(
                "autosport.real_execution_ledger._now",
                return_value=UNKNOWN_AT,
            ):
                self.assertEqual(
                    ledger.recover_uncertain(),
                    ("try-1", "try-2"),
                )

            restarted = RealExecutionLedger(path)
            self.assertEqual(restarted.verify_integrity(), 6)
            self.assertEqual(
                restarted.attempt_state("try-1"),
                AttemptState.UNKNOWN,
            )
            self.assertEqual(
                restarted.attempt_state("try-2"),
                AttemptState.UNKNOWN,
            )

    def test_unknown_ack_requires_durable_positive_reconciliation_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )

            with self.assertRaisesRegex(ExecutionStateError, "reconciliation evidence"):
                ledger.acknowledge(
                    ExternalAcknowledgement(
                        attempt_id="try-1",
                        external_receipt_id="r1",
                        status=AcknowledgementStatus.ACCEPTED,
                        acknowledged_at=RECONCILED_AT,
                        accepted_odds="2.5",
                        accepted_stake="10",
                    )
                )

            with self.assertRaisesRegex(
                ExecutionStateError, "durable positive reconciliation evidence"
            ):
                ledger.acknowledge(
                    ExternalAcknowledgement(
                        attempt_id="try-1",
                        external_receipt_id="r1",
                        status=AcknowledgementStatus.ACCEPTED,
                        acknowledged_at=RECONCILED_AT,
                        accepted_odds="2.5",
                        accepted_stake="10",
                        reconciliation_evidence_id="fake-readback",
                    )
                )

            ledger.reconcile_found(
                ExternalEffectReconciliation(
                    attempt_id="try-1",
                    evidence_id="readback-1",
                    external_receipt_id="r1",
                    observed_at=RECONCILED_AT,
                    source="provider-readback",
                )
            )

            with self.assertRaisesRegex(
                ExecutionStateError, "precedes attempt causal boundary"
            ):
                ledger.acknowledge(
                    ExternalAcknowledgement(
                        attempt_id="try-1",
                        external_receipt_id="r1",
                        status=AcknowledgementStatus.ACCEPTED,
                        acknowledged_at=SUBMITTED_AT,
                        accepted_odds="2.5",
                        accepted_stake="10",
                        reconciliation_evidence_id="readback-1",
                    )
                )

            ledger.acknowledge(
                ExternalAcknowledgement(
                    attempt_id="try-1",
                    external_receipt_id="r1",
                    status=AcknowledgementStatus.ACCEPTED,
                    acknowledged_at=RECONCILED_AT,
                    accepted_odds="2.5",
                    accepted_stake="10",
                    reconciliation_evidence_id="readback-1",
                )
            )
            self.assertEqual(ledger.attempt_state("try-1"), AttemptState.ACCEPTED)

    def test_unknown_ack_rejects_mismatched_reconciliation_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )
            ledger.reconcile_found(
                ExternalEffectReconciliation(
                    attempt_id="try-1",
                    evidence_id="readback-1",
                    external_receipt_id="different-receipt",
                    observed_at=RECONCILED_AT,
                    source="provider-readback",
                )
            )

            with self.assertRaisesRegex(
                ExecutionStateError, "receipt mismatches reconciliation evidence"
            ):
                ledger.acknowledge(
                    ExternalAcknowledgement(
                        attempt_id="try-1",
                        external_receipt_id="r1",
                        status=AcknowledgementStatus.ACCEPTED,
                        acknowledged_at=RECONCILED_AT,
                        accepted_odds="2.5",
                        accepted_stake="10",
                        reconciliation_evidence_id="readback-1",
                    )
                )

    def test_positive_reconciliation_requires_one_receipt_identity_per_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )
            ledger.reconcile_found(
                ExternalEffectReconciliation(
                    attempt_id="try-1",
                    evidence_id="readback-1",
                    external_receipt_id="r1",
                    observed_at=RECONCILED_AT,
                    source="provider-readback",
                )
            )
            ledger.reconcile_found(
                ExternalEffectReconciliation(
                    attempt_id="try-1",
                    evidence_id="readback-2",
                    external_receipt_id="r1",
                    observed_at=SECOND_RECONCILED_AT,
                    source="provider-readback-second-observation",
                )
            )

            with self.assertRaisesRegex(
                ExecutionIdentityConflict, "receipt identity"
            ):
                ledger.reconcile_found(
                    ExternalEffectReconciliation(
                        attempt_id="try-1",
                        evidence_id="readback-3",
                        external_receipt_id="r2",
                        observed_at=RETRY_RESERVED_AT,
                        source="provider-readback-conflict",
                    )
                )
            self.assertEqual(
                ledger.attempt_state("try-1"), AttemptState.UNKNOWN
            )

    def test_positive_reconciliation_receipt_is_owned_across_attempts_same_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action("a1"), action("a2")))
            for action_id, attempt_id in (("a1", "try-1"), ("a2", "try-2")):
                ledger.begin_attempt(
                    plan_id="p1",
                    action_id=action_id,
                    attempt_id=attempt_id,
                    reserved_at=RESERVED_AT,
                )
                ledger.mark_unknown(
                    attempt_id, reason="timeout", observed_at=UNKNOWN_AT
                )

            ledger.reconcile_found(
                ExternalEffectReconciliation(
                    attempt_id="try-1",
                    evidence_id="readback-1",
                    external_receipt_id="shared-receipt",
                    observed_at=RECONCILED_AT,
                    source="provider-readback",
                )
            )
            with self.assertRaisesRegex(
                ExecutionIdentityConflict, "already belongs to another attempt"
            ):
                ledger.reconcile_found(
                    ExternalEffectReconciliation(
                        attempt_id="try-2",
                        evidence_id="readback-2",
                        external_receipt_id="shared-receipt",
                        observed_at=SECOND_RECONCILED_AT,
                        source="provider-readback",
                    )
                )
            self.assertEqual(ledger.attempt_state("try-2"), AttemptState.UNKNOWN)

    def test_restart_rejects_hash_valid_cross_attempt_found_receipt_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action("a1"), action("a2")))
            for action_id, attempt_id in (("a1", "try-1"), ("a2", "try-2")):
                ledger.begin_attempt(
                    plan_id="p1",
                    action_id=action_id,
                    attempt_id=attempt_id,
                    reserved_at=RESERVED_AT,
                )
                ledger.mark_unknown(
                    attempt_id, reason="timeout", observed_at=UNKNOWN_AT
                )

            ledger.reconcile_found(
                ExternalEffectReconciliation(
                    attempt_id="try-1",
                    evidence_id="readback-1",
                    external_receipt_id="shared-receipt",
                    observed_at=RECONCILED_AT,
                    source="provider-readback",
                )
            )
            ledger._append(
                EventType.RECONCILED_FOUND,
                "p1",
                "a2",
                "try-2",
                ExternalEffectReconciliation(
                    attempt_id="try-2",
                    evidence_id="tampered-readback",
                    external_receipt_id="shared-receipt",
                    observed_at=SECOND_RECONCILED_AT,
                    source="provider-readback-tamper",
                ).to_dict(),
            )

            restarted = RealExecutionLedger(path)
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError, "belongs to multiple attempts"
            ):
                restarted.verify_integrity()

    def test_positive_reconciliation_same_native_receipt_allowed_across_account_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(
                plan(
                    action("a1", bookmaker_id="betfair", account_id="acct-1"),
                    action("a2", bookmaker_id="betfair", account_id="acct-2"),
                )
            )
            for action_id, attempt_id in (("a1", "try-1"), ("a2", "try-2")):
                ledger.begin_attempt(
                    plan_id="p1",
                    action_id=action_id,
                    attempt_id=attempt_id,
                    reserved_at=RESERVED_AT,
                )
                ledger.mark_unknown(
                    attempt_id, reason="timeout", observed_at=UNKNOWN_AT
                )

            ledger.reconcile_found(
                ExternalEffectReconciliation(
                    attempt_id="try-1",
                    evidence_id="readback-1",
                    external_receipt_id="provider-local-receipt",
                    observed_at=RECONCILED_AT,
                    source="provider-readback",
                )
            )
            ledger.reconcile_found(
                ExternalEffectReconciliation(
                    attempt_id="try-2",
                    evidence_id="readback-2",
                    external_receipt_id="provider-local-receipt",
                    observed_at=SECOND_RECONCILED_AT,
                    source="provider-readback",
                )
            )

            self.assertEqual(ledger.verify_integrity(), 7)
            self.assertEqual(ledger.attempt_state("try-1"), AttemptState.UNKNOWN)
            self.assertEqual(ledger.attempt_state("try-2"), AttemptState.UNKNOWN)

    def test_positive_reconciliation_blocks_not_found_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )
            ledger.reconcile_found(
                ExternalEffectReconciliation(
                    attempt_id="try-1",
                    evidence_id="found-readback",
                    external_receipt_id="r1",
                    observed_at=RECONCILED_AT,
                    source="provider-readback",
                )
            )

            with self.assertRaisesRegex(
                ExecutionStateError, "positive reconciliation evidence"
            ):
                ledger.reconcile_not_found(
                    ReconciliationSnapshot(
                        attempt_id="try-1",
                        evidence_id="not-found-readback",
                        observed_at=RETRY_RESERVED_AT,
                        external_effect_found=False,
                        source="provider-readback",
                    )
                )
            self.assertEqual(
                ledger.attempt_state("try-1"), AttemptState.UNKNOWN
            )
            self.assertFalse(
                ledger.can_retry_action(plan_id="p1", action_id="a1")
            )

    def test_unknown_retry_only_after_not_found_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
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
                ledger.attempt_state("try-1"), AttemptState.RECONCILED_NOT_FOUND
            )
            self.assertTrue(ledger.can_retry_action(plan_id="p1", action_id="a1"))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-2",
                reserved_at=RETRY_RESERVED_AT,
            )

    def test_stale_not_found_evidence_cannot_authorize_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )

            with self.assertRaisesRegex(
                ExecutionStateError, "newer than attempt uncertainty boundary"
            ):
                ledger.reconcile_not_found(
                    ReconciliationSnapshot(
                        attempt_id="try-1",
                        evidence_id="stale-readback",
                        observed_at=RESERVED_AT,
                        external_effect_found=False,
                        source="provider-readback",
                    )
                )

            self.assertEqual(ledger.attempt_state("try-1"), AttemptState.UNKNOWN)
            self.assertFalse(
                ledger.can_retry_action(plan_id="p1", action_id="a1")
            )

    def test_expired_quote_cannot_start_or_retry_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))

            with self.assertRaisesRegex(
                ExecutionStateError, "persisted quote expiry"
            ):
                ledger.begin_attempt(
                    plan_id="p1",
                    action_id="a1",
                    attempt_id="expired-first",
                    reserved_at=EXPIRES_AT,
                )

            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )
            ledger.reconcile_not_found(
                ReconciliationSnapshot(
                    attempt_id="try-1",
                    evidence_id="fresh-readback",
                    observed_at=RECONCILED_AT,
                    external_effect_found=False,
                    source="provider-readback",
                )
            )
            with self.assertRaisesRegex(
                ExecutionStateError, "persisted quote expiry"
            ):
                ledger.begin_attempt(
                    plan_id="p1",
                    action_id="a1",
                    attempt_id="expired-retry",
                    reserved_at=EXPIRES_AT,
                )

    def test_submit_cannot_precede_reservation_or_reach_quote_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )

            with self.assertRaisesRegex(
                ExecutionStateError, "precede attempt reservation"
            ):
                ledger.mark_submitted("try-1", submitted_at=TS)
            self.assertEqual(
                ledger.attempt_state("try-1"), AttemptState.RESERVED
            )

            with self.assertRaisesRegex(
                ExecutionStateError, "persisted quote expiry"
            ):
                ledger.mark_submitted("try-1", submitted_at=EXPIRES_AT)
            self.assertEqual(
                ledger.attempt_state("try-1"), AttemptState.RESERVED
            )

    def test_unknown_timestamp_cannot_precede_reservation_or_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )

            with self.assertRaisesRegex(
                ExecutionStateError, "precede attempt reservation"
            ):
                ledger.mark_unknown(
                    "try-1", reason="timeout", observed_at=TS
                )
            self.assertEqual(
                ledger.attempt_state("try-1"), AttemptState.RESERVED
            )

            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            with self.assertRaisesRegex(
                ExecutionStateError, "precede attempt submission"
            ):
                ledger.mark_unknown(
                    "try-1", reason="timeout", observed_at=RESERVED_AT
                )
            self.assertEqual(
                ledger.attempt_state("try-1"), AttemptState.SUBMITTED
            )

    def test_not_found_before_reservation_cannot_authorize_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )

            with self.assertRaisesRegex(
                ExecutionStateError, "newer than attempt uncertainty boundary"
            ):
                ledger.reconcile_not_found(
                    ReconciliationSnapshot(
                        attempt_id="try-1",
                        evidence_id="pre-reservation-readback",
                        observed_at=TS,
                        external_effect_found=False,
                        source="provider-readback",
                    )
                )
            self.assertEqual(
                ledger.attempt_state("try-1"), AttemptState.UNKNOWN
            )
            self.assertFalse(
                ledger.can_retry_action(plan_id="p1", action_id="a1")
            )

    def test_restart_rejects_hash_valid_backdated_unknown_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger._append(
                EventType.ATTEMPT_UNKNOWN,
                "p1",
                "a1",
                "try-1",
                {"reason": "legacy", "observed_at": TS},
            )

            restarted = RealExecutionLedger(path)
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "UNKNOWN observation precedes attempt reservation",
            ):
                restarted.verify_integrity()

    def test_restart_rejects_hash_valid_conflicting_found_receipt_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )
            ledger.reconcile_found(
                ExternalEffectReconciliation(
                    attempt_id="try-1",
                    evidence_id="readback-1",
                    external_receipt_id="r1",
                    observed_at=RECONCILED_AT,
                    source="provider-readback",
                )
            )
            ledger._append(
                EventType.RECONCILED_FOUND,
                "p1",
                "a1",
                "try-1",
                ExternalEffectReconciliation(
                    attempt_id="try-1",
                    evidence_id="readback-2",
                    external_receipt_id="r2",
                    observed_at=SECOND_RECONCILED_AT,
                    source="provider-readback-tamper",
                ).to_dict(),
            )

            restarted = RealExecutionLedger(path)
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "receipt identity",
            ):
                restarted.verify_integrity()

    def test_acknowledgement_cannot_exceed_requested_stake(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)

            with self.assertRaisesRegex(
                ExecutionStateError, "exceeds requested action stake"
            ):
                ledger.acknowledge(
                    ExternalAcknowledgement(
                        attempt_id="try-1",
                        external_receipt_id="oversized",
                        status=AcknowledgementStatus.ACCEPTED,
                        acknowledged_at=RECONCILED_AT,
                        accepted_odds="2.5",
                        accepted_stake="10.01",
                    )
                )
            self.assertEqual(ledger.attempt_state("try-1"), AttemptState.SUBMITTED)


    def test_restart_rejects_hash_valid_unknown_ack_evidence_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
            )
            ledger.reconcile_found(
                ExternalEffectReconciliation(
                    attempt_id="try-1",
                    evidence_id="readback-1",
                    external_receipt_id="r1",
                    observed_at=RECONCILED_AT,
                    source="provider-readback",
                )
            )
            ledger.acknowledge(
                ExternalAcknowledgement(
                    attempt_id="try-1",
                    external_receipt_id="r1",
                    status=AcknowledgementStatus.ACCEPTED,
                    acknowledged_at=RECONCILED_AT,
                    accepted_odds="2.5",
                    accepted_stake="5",
                    reconciliation_evidence_id="readback-1",
                )
            )

            lines = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            acknowledgement = next(
                envelope
                for envelope in lines
                if envelope["event"]["event_type"]
                == EventType.EXTERNAL_ACKNOWLEDGEMENT.value
            )
            acknowledgement["event"]["payload"][
                "reconciliation_evidence_id"
            ] = "fabricated-readback"

            import hashlib

            body = json.dumps(
                acknowledgement["event"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            acknowledgement["sha256"] = hashlib.sha256(
                body.encode()
            ).hexdigest()
            path.write_text(
                "\n".join(
                    json.dumps(
                        envelope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    for envelope in lines
                )
                + "\n",
                encoding="utf-8",
            )

            restarted = RealExecutionLedger(path)
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "missing reconciliation evidence",
            ):
                restarted.verify_integrity()

    def test_restart_rejects_hash_valid_acknowledgement_stake_escalation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.acknowledge(
                ExternalAcknowledgement(
                    attempt_id="try-1",
                    external_receipt_id="r1",
                    status=AcknowledgementStatus.ACCEPTED,
                    acknowledged_at=RECONCILED_AT,
                    accepted_odds="2.5",
                    accepted_stake="5",
                )
            )

            lines = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            acknowledgement = next(
                envelope
                for envelope in lines
                if envelope["event"]["event_type"]
                == EventType.EXTERNAL_ACKNOWLEDGEMENT.value
            )
            acknowledgement["event"]["payload"]["accepted_stake"] = "10.01"

            import hashlib

            body = json.dumps(
                acknowledgement["event"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            acknowledgement["sha256"] = hashlib.sha256(
                body.encode()
            ).hexdigest()
            path.write_text(
                "\n".join(
                    json.dumps(
                        envelope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    for envelope in lines
                )
                + "\n",
                encoding="utf-8",
            )

            restarted = RealExecutionLedger(path)
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "acknowledgement stake exceeds requested action stake",
            ):
                restarted.verify_integrity()

    def test_restart_rejects_hash_valid_rejected_ack_with_accepted_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("try-1", submitted_at=SUBMITTED_AT)
            ledger.acknowledge(
                ExternalAcknowledgement(
                    attempt_id="try-1",
                    external_receipt_id="r1",
                    status=AcknowledgementStatus.ACCEPTED,
                    acknowledged_at=RECONCILED_AT,
                    accepted_odds="2.5",
                    accepted_stake="5",
                )
            )

            lines = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            acknowledgement = next(
                envelope
                for envelope in lines
                if envelope["event"]["event_type"]
                == EventType.EXTERNAL_ACKNOWLEDGEMENT.value
            )
            acknowledgement["event"]["payload"]["status"] = (
                AcknowledgementStatus.REJECTED.value
            )

            import hashlib

            body = json.dumps(
                acknowledgement["event"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            acknowledgement["sha256"] = hashlib.sha256(
                body.encode()
            ).hexdigest()
            path.write_text(
                "\n".join(
                    json.dumps(
                        envelope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    for envelope in lines
                )
                + "\n",
                encoding="utf-8",
            )

            restarted = RealExecutionLedger(path)
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "stored acknowledgement values are invalid",
            ):
                restarted.verify_integrity()


    def test_receipt_identity_is_scoped_by_bookmaker_and_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(
                plan(action(bookmaker_id="book-a", account_id="a"), plan_id="p1")
            )
            ledger.reserve_plan(
                plan(action(bookmaker_id="book-b", account_id="b"), plan_id="p2")
            )
            for plan_id, attempt_id in (("p1", "t1"), ("p2", "t2")):
                ledger.begin_attempt(
                    plan_id=plan_id,
                    action_id="a1",
                    attempt_id=attempt_id,
                    reserved_at=RESERVED_AT,
                )
                ledger.mark_submitted(
                    attempt_id,
                    submitted_at=SUBMITTED_AT,
                )
                ledger.acknowledge(
                    ExternalAcknowledgement(
                        attempt_id=attempt_id,
                        external_receipt_id="same-native-id",
                        status=AcknowledgementStatus.ACCEPTED,
                        acknowledged_at=RECONCILED_AT,
                        accepted_odds="2.5",
                        accepted_stake="10",
                    )
                )
            self.assertEqual(ledger.verify_integrity(), 8)

    def test_same_provider_account_receipt_cannot_belong_to_two_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
            ledger.reserve_plan(plan(action(), plan_id="p1"))
            ledger.reserve_plan(plan(action(), plan_id="p2"))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="t1",
                reserved_at=RESERVED_AT,
            )
            ledger.begin_attempt(
                plan_id="p2",
                action_id="a1",
                attempt_id="t2",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("t1", submitted_at=SUBMITTED_AT)
            ledger.mark_submitted("t2", submitted_at=SUBMITTED_AT)
            ledger.acknowledge(
                ExternalAcknowledgement(
                    attempt_id="t1",
                    external_receipt_id="r",
                    status=AcknowledgementStatus.ACCEPTED,
                    acknowledged_at=RECONCILED_AT,
                    accepted_odds="2.5",
                    accepted_stake="10",
                )
            )
            with self.assertRaises(ExecutionIdentityConflict):
                ledger.acknowledge(
                    ExternalAcknowledgement(
                        attempt_id="t2",
                        external_receipt_id="r",
                        status=AcknowledgementStatus.ACCEPTED,
                        acknowledged_at=RECONCILED_AT,
                        accepted_odds="2.5",
                        accepted_stake="10",
                    )
                )

    def test_multi_action_ack_is_itself_crash_atomic_stale_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action("a1"), action("a2")))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="t1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_submitted("t1", submitted_at=SUBMITTED_AT)
            ledger.acknowledge(
                ExternalAcknowledgement(
                    attempt_id="t1",
                    external_receipt_id="r1",
                    status=AcknowledgementStatus.PARTIAL,
                    acknowledged_at=RECONCILED_AT,
                    accepted_odds="2.4",
                    accepted_stake="5",
                )
            )

            restarted = RealExecutionLedger(path)
            self.assertTrue(restarted.plan_is_stale("p1"))
            self.assertFalse(restarted.can_retry_action(plan_id="p1", action_id="a2"))
            with self.assertRaisesRegex(ExecutionStateError, "stale"):
                restarted.begin_attempt(
                    plan_id="p1",
                    action_id="a2",
                    attempt_id="t2",
                    reserved_at=RETRY_RESERVED_AT,
                )

    def test_semantic_plan_fingerprint_tamper_detected_even_if_event_hash_recomputed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["event"]["payload"]["plan"]["approval_id"] = "forged"

            import hashlib

            body = json.dumps(
                envelope["event"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            envelope["sha256"] = hashlib.sha256(body.encode()).hexdigest()
            path.write_text(
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError, "plan fingerprint mismatch"
            ):
                ledger.verify_integrity()

    def test_semantic_effect_fingerprint_tamper_detected_even_if_event_hash_recomputed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="t1",
                reserved_at=RESERVED_AT,
            )
            lines = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            lines[1]["event"]["payload"]["effect_fingerprint"] = "0" * 64

            import hashlib

            body = json.dumps(
                lines[1]["event"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            lines[1]["sha256"] = hashlib.sha256(body.encode()).hexdigest()
            path.write_text(
                "\n".join(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    for item in lines
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError, "effect fingerprint mismatch"
            ):
                ledger.verify_integrity()

    def test_existing_writer_lock_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger._lock_path.write_text("owner", encoding="utf-8")
            with self.assertRaises(ExecutionLedgerBusyError):
                ledger.reserve_plan(plan(action()))

    def test_rejected_ack_cannot_claim_accepted_money(self):
        with self.assertRaises(ValueError):
            ExternalAcknowledgement(
                attempt_id="t1",
                external_receipt_id="r",
                status=AcknowledgementStatus.REJECTED,
                acknowledged_at=RECONCILED_AT,
                accepted_odds="2.5",
                accepted_stake="10",
            )


    def test_restart_rejects_hash_valid_not_found_claiming_found_external_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
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

            lines = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            not_found = next(
                envelope
                for envelope in lines
                if envelope["event"]["event_type"]
                == EventType.RECONCILED_NOT_FOUND.value
            )
            not_found["event"]["payload"]["external_effect_found"] = True

            import hashlib

            body = json.dumps(
                not_found["event"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            not_found["sha256"] = hashlib.sha256(body.encode()).hexdigest()
            path.write_text(
                "\n".join(
                    json.dumps(
                        envelope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    for envelope in lines
                )
                + "\n",
                encoding="utf-8",
            )

            restarted = RealExecutionLedger(path)
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "not-found reconciliation cannot claim external effect",
            ):
                restarted.verify_integrity()

    def test_restart_rejects_hash_valid_not_found_attempt_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.jsonl"
            ledger = RealExecutionLedger(path)
            ledger.reserve_plan(plan(action()))
            ledger.begin_attempt(
                plan_id="p1",
                action_id="a1",
                attempt_id="try-1",
                reserved_at=RESERVED_AT,
            )
            ledger.mark_unknown(
                "try-1", reason="timeout", observed_at=UNKNOWN_AT
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

            lines = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            not_found = next(
                envelope
                for envelope in lines
                if envelope["event"]["event_type"]
                == EventType.RECONCILED_NOT_FOUND.value
            )
            not_found["event"]["payload"]["attempt_id"] = "other-attempt"

            import hashlib

            body = json.dumps(
                not_found["event"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            not_found["sha256"] = hashlib.sha256(body.encode()).hexdigest()
            path.write_text(
                "\n".join(
                    json.dumps(
                        envelope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    for envelope in lines
                )
                + "\n",
                encoding="utf-8",
            )

            restarted = RealExecutionLedger(path)
            with self.assertRaisesRegex(
                ExecutionLedgerIntegrityError,
                "not-found reconciliation attempt identity mismatch",
            ):
                restarted.verify_integrity()



if __name__ == "__main__":
    unittest.main()
