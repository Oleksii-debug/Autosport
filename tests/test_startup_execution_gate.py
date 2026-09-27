import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionPlan,
    ExternalEffectReconciliation,
    RealExecutionLedger,
    ReconciliationSnapshot,
)
from autosport.startup_execution_gate import (
    AccountExposureSnapshot,
    ExecutionAccount,
    StartupExecutionBlockReason,
    StartupExecutionGate,
)


OBSERVED_AT = "2026-09-20T10:00:00+00:00"
RESERVED_AT = "2026-09-20T10:00:01+00:00"
EXPIRES_AT = "2099-01-01T00:00:00+00:00"
RECONCILED_AT = "2099-01-01T00:00:01+00:00"
ACCOUNT_OBSERVED_AT = "2099-01-01T00:00:02+00:00"
ACCOUNT = ExecutionAccount("betfair", "acct-1")


def action(action_id: str = "a1") -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id=ACCOUNT.bookmaker_id,
        account_id=ACCOUNT.account_id,
        event_id=f"event-{action_id}",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id=f"quote-{action_id}",
        quote_observed_at=OBSERVED_AT,
        expires_at=EXPIRES_AT,
    )


def plan(plan_id: str = "p1", action_id: str = "a1") -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        bookmaker_profile_version="profile-v1",
        decision_id=f"decision-{plan_id}",
        approval_id=f"approval-{plan_id}",
        created_at=OBSERVED_AT,
        actions=(action(action_id),),
    )


def account_snapshot(
    *,
    observed_at: str = ACCOUNT_OBSERVED_AT,
    available_bankroll: str = "100.00",
    open_exposure: str = "0.00",
) -> AccountExposureSnapshot:
    return AccountExposureSnapshot(
        bookmaker_id=ACCOUNT.bookmaker_id,
        account_id=ACCOUNT.account_id,
        currency="EUR",
        available_bankroll=available_bankroll,
        open_exposure=open_exposure,
        observed_at=observed_at,
    )


def begin_uncertain(ledger: RealExecutionLedger) -> None:
    ledger.reserve_plan(plan())
    ledger.begin_attempt(
        plan_id="p1",
        action_id="a1",
        attempt_id="try-1",
        reserved_at=RESERVED_AT,
    )


class StartupExecutionGateTests(unittest.TestCase):
    def gate(
        self,
        ledger: RealExecutionLedger,
        *,
        reconcile=None,
        rebuild=None,
    ) -> StartupExecutionGate:
        return StartupExecutionGate(
            ledger=ledger,
            expected_accounts=(ACCOUNT,),
            reconcile_unresolved=reconcile,
            rebuild_account_state=rebuild,
        )

    def test_restart_promotes_uncertain_attempt_and_blocks_without_provider_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
            begin_uncertain(ledger)
            rebuild_called = False

            def rebuild():
                nonlocal rebuild_called
                rebuild_called = True
                return (account_snapshot(),)

            status = self.gate(ledger, rebuild=rebuild).evaluate()

            self.assertFalse(status.execution_enabled)
            self.assertTrue(status.analysis_read_only_enabled)
            self.assertEqual(
                status.reason,
                StartupExecutionBlockReason.PROVIDER_RECONCILIATION_UNAVAILABLE,
            )
            self.assertEqual(status.promoted_attempt_ids, ("try-1",))
            self.assertEqual(status.unresolved_attempt_ids, ("try-1",))
            self.assertEqual(ledger.attempt_state("try-1"), AttemptState.UNKNOWN)
            self.assertFalse(rebuild_called)

    def test_reconciliation_callback_cannot_claim_success_without_resolving_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
            begin_uncertain(ledger)
            seen = []

            def reconcile(attempt_ids):
                seen.append(attempt_ids)

            status = self.gate(
                ledger,
                reconcile=reconcile,
                rebuild=lambda: (account_snapshot(),),
            ).evaluate()

            self.assertEqual(seen, [("try-1",)])
            self.assertFalse(status.execution_enabled)
            self.assertEqual(
                status.reason,
                StartupExecutionBlockReason.UNRESOLVED_EXTERNAL_EFFECTS,
            )
            self.assertEqual(status.unresolved_attempt_ids, ("try-1",))

    def test_positive_readback_without_acknowledgement_remains_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
            begin_uncertain(ledger)

            def reconcile(attempt_ids):
                self.assertEqual(attempt_ids, ("try-1",))
                ledger.reconcile_found(
                    ExternalEffectReconciliation(
                        attempt_id="try-1",
                        evidence_id="provider-readback-1",
                        external_receipt_id="receipt-1",
                        observed_at=RECONCILED_AT,
                        source="provider-readback",
                    )
                )

            status = self.gate(
                ledger,
                reconcile=reconcile,
                rebuild=lambda: (account_snapshot(),),
            ).evaluate()

            self.assertFalse(status.execution_enabled)
            self.assertEqual(
                status.reason,
                StartupExecutionBlockReason.UNRESOLVED_EXTERNAL_EFFECTS,
            )
            self.assertEqual(ledger.attempt_state("try-1"), AttemptState.UNKNOWN)

    def test_reconciled_execution_stays_blocked_without_product_account_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
            begin_uncertain(ledger)

            def reconcile(attempt_ids):
                for attempt_id in attempt_ids:
                    ledger.reconcile_not_found(
                        ReconciliationSnapshot(
                            attempt_id=attempt_id,
                            evidence_id=f"not-found-{attempt_id}",
                            observed_at=RECONCILED_AT,
                            external_effect_found=False,
                            source="provider-readback",
                        )
                    )

            status = self.gate(
                ledger,
                reconcile=reconcile,
                rebuild=lambda: (account_snapshot(),),
            ).evaluate()

            self.assertFalse(status.execution_enabled)
            self.assertTrue(status.analysis_read_only_enabled)
            self.assertEqual(
                status.reason,
                StartupExecutionBlockReason.ACCOUNT_STATE_AUTHORITY_UNAVAILABLE,
            )
            self.assertEqual(status.unresolved_attempt_ids, ())
            self.assertEqual(status.account_snapshots, (account_snapshot(),))
            self.assertEqual(
                ledger.attempt_state("try-1"), AttemptState.RECONCILED_NOT_FOUND
            )

    def test_caller_minted_account_snapshot_cannot_enable_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
            forged = account_snapshot(
                available_bankroll="999999999.99",
                open_exposure="0",
            )

            status = self.gate(
                ledger,
                rebuild=lambda: (forged,),
            ).evaluate()

            self.assertFalse(status.execution_enabled)
            self.assertEqual(
                status.reason,
                StartupExecutionBlockReason.ACCOUNT_STATE_AUTHORITY_UNAVAILABLE,
            )
            self.assertEqual(status.account_snapshots, (forged,))

    def test_stale_caller_snapshot_cannot_enable_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
            stale = account_snapshot(observed_at="2000-01-01T00:00:00+00:00")

            status = self.gate(
                ledger,
                rebuild=lambda: (stale,),
            ).evaluate()

            self.assertFalse(status.execution_enabled)
            self.assertEqual(
                status.reason,
                StartupExecutionBlockReason.ACCOUNT_STATE_AUTHORITY_UNAVAILABLE,
            )
            self.assertEqual(status.account_snapshots, (stale,))

    def test_account_rebuild_must_cover_exact_configured_account_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
            wrong = AccountExposureSnapshot(
                bookmaker_id="betfair",
                account_id="acct-other",
                currency="EUR",
                available_bankroll="100",
                open_exposure="0",
                observed_at=ACCOUNT_OBSERVED_AT,
            )

            status = self.gate(ledger, rebuild=lambda: (wrong,)).evaluate()

            self.assertFalse(status.execution_enabled)
            self.assertEqual(
                status.reason, StartupExecutionBlockReason.ACCOUNT_STATE_INVALID
            )
            self.assertTrue(status.analysis_read_only_enabled)

    def test_provider_and_account_failures_are_contained_as_fail_closed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
            begin_uncertain(ledger)

            def provider_down(_attempt_ids):
                raise TimeoutError("provider unavailable")

            provider_status = self.gate(
                ledger,
                reconcile=provider_down,
                rebuild=lambda: (account_snapshot(),),
            ).evaluate()
            self.assertEqual(
                provider_status.reason,
                StartupExecutionBlockReason.PROVIDER_RECONCILIATION_FAILED,
            )
            self.assertTrue(provider_status.analysis_read_only_enabled)

            ledger.reconcile_not_found(
                ReconciliationSnapshot(
                    attempt_id="try-1",
                    evidence_id="manual-not-found",
                    observed_at=RECONCILED_AT,
                    external_effect_found=False,
                    source="provider-readback",
                )
            )

            def account_down():
                raise TimeoutError("account endpoint unavailable")

            account_status = self.gate(ledger, rebuild=account_down).evaluate()
            self.assertEqual(
                account_status.reason,
                StartupExecutionBlockReason.ACCOUNT_STATE_REBUILD_FAILED,
            )
            self.assertFalse(account_status.execution_enabled)
            self.assertTrue(account_status.analysis_read_only_enabled)

    def test_new_uncertain_effect_during_rebuild_is_caught_by_final_ledger_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")

            def rebuild():
                ledger.reserve_plan(plan("p2", "a2"))
                ledger.begin_attempt(
                    plan_id="p2",
                    action_id="a2",
                    attempt_id="try-race",
                    reserved_at=RESERVED_AT,
                )
                return (account_snapshot(),)

            status = self.gate(ledger, rebuild=rebuild).evaluate()

            self.assertFalse(status.execution_enabled)
            self.assertEqual(
                status.reason,
                StartupExecutionBlockReason.UNRESOLVED_EXTERNAL_EFFECTS,
            )
            self.assertEqual(status.unresolved_attempt_ids, ("try-race",))
            self.assertEqual(status.account_snapshots, (account_snapshot(),))

    def test_attempt_appended_after_final_snapshot_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = RealExecutionLedger(Path(tmp) / "execution.jsonl")
            original_verified_snapshot = ledger.verified_snapshot
            snapshot_calls = 0

            def verified_snapshot_with_post_capture_append():
                nonlocal snapshot_calls
                snapshot = original_verified_snapshot()
                snapshot_calls += 1
                if snapshot_calls == 2:
                    ledger.reserve_plan(plan("race-plan", "race-action"))
                    ledger.begin_attempt(
                        plan_id="race-plan",
                        action_id="race-action",
                        attempt_id="race-attempt",
                        reserved_at=RESERVED_AT,
                    )
                return snapshot

            gate = self.gate(
                ledger,
                reconcile=lambda _attempt_ids: None,
                rebuild=lambda: (account_snapshot(),),
            )

            with patch.object(
                ledger,
                "verified_snapshot",
                side_effect=verified_snapshot_with_post_capture_append,
            ):
                status = gate.evaluate()

            self.assertEqual(snapshot_calls, 2)
            self.assertEqual(
                ledger.attempt_state("race-attempt"),
                AttemptState.RESERVED,
            )
            self.assertFalse(status.execution_enabled)
            self.assertEqual(
                status.reason,
                StartupExecutionBlockReason.UNRESOLVED_EXTERNAL_EFFECTS,
            )
            self.assertEqual(status.unresolved_attempt_ids, ("race-attempt",))

    def test_corrupt_ledger_disables_execution_without_disabling_read_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            path.write_text("not-json\n", encoding="utf-8")
            ledger = RealExecutionLedger(path)

            status = self.gate(
                ledger,
                reconcile=lambda _attempt_ids: None,
                rebuild=lambda: (account_snapshot(),),
            ).evaluate()

            self.assertFalse(status.execution_enabled)
            self.assertTrue(status.analysis_read_only_enabled)
            self.assertEqual(
                status.reason, StartupExecutionBlockReason.LEDGER_UNAVAILABLE
            )


if __name__ == "__main__":
    unittest.main()
