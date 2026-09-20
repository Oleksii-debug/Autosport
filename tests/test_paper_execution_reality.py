from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.paper_execution_reality import (
    EvidenceGrade,
    ObservedPaperExecution,
    PaperAttemptOutcome,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
    RecoveryDecision,
    execute_paper_plan,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T03:00:00+00:00"
STARTED_AT = "2026-09-20T03:00:00.100000+00:00"
EXPIRES_AT = "2026-09-20T03:01:00+00:00"


def action(
    action_id: str,
    *,
    odds: str = "2.50",
    stake: str = "10.00",
    side: str = "BACK",
    market_id: str | None = None,
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-1",
        market_id=market_id or f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side=side,
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="paper-plan-1",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="decision-1",
        approval_id="paper-only",
        created_at=QUOTE_AT,
        actions=tuple(actions),
    )


def config(**overrides) -> PaperExecutionModelConfig:
    values = {
        "model_id": "paper-reality",
        "model_version": "1",
        "evidence_grade": EvidenceGrade.SYNTHETIC,
        "evidence_source": "test-seeded-model",
        "seed": "fixed-seed",
        "max_quote_age_ms": 5_000,
        "min_delay_ms": 100,
        "max_delay_ms": 100,
        "rejected_bps": 0,
        "partial_bps": 0,
        "unknown_bps": 0,
        "partial_fill_bps": 5_000,
        "max_slippage_bps": 100,
    }
    values.update(overrides)
    return PaperExecutionModelConfig(**values)


def observed(
    action_id: str,
    outcome: PaperAttemptOutcome,
    *,
    at: str = "2026-09-20T03:00:00.250000+00:00",
    odds: str | None = None,
    stake: str | None = None,
    suspended: bool = False,
) -> ObservedPaperExecution:
    return ObservedPaperExecution(
        action_id=action_id,
        outcome=outcome,
        observed_at=at,
        evidence_grade=EvidenceGrade.EMPIRICAL,
        evidence_source="captured-paper-observation-v1",
        accepted_odds=odds,
        accepted_stake=stake,
        suspended=suspended,
        reason=f"observed {outcome.value.lower()}",
    )


class PaperExecutionRealityTests(unittest.TestCase):
    def test_all_accepted_keeps_decision_and_execution_quotes_distinct_and_dedupes_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1", odds="2.50"), action("a2", odds="3.00"))
            model = config()

            first = execute_paper_plan(
                plan=current,
                trigger_id="trigger-1",
                config=model,
                ledger=ledger,
                started_at=STARTED_AT,
            )

            self.assertTrue(first.completed)
            self.assertTrue(first.all_actions_accepted)
            self.assertEqual(first.pending_action_ids, ())
            self.assertEqual(first.recovery_decision, RecoveryDecision.NONE)
            self.assertEqual(len(first.attempts), 2)
            for attempt, source in zip(first.attempts, current.actions, strict=True):
                self.assertEqual(attempt.decision_quote_id, source.quote_id)
                self.assertEqual(attempt.decision_odds, source.requested_odds)
                self.assertEqual(attempt.execution_stake, source.requested_stake)
                self.assertIsNotNone(attempt.execution_odds)
                self.assertLessEqual(attempt.execution_odds, source.requested_odds)
                self.assertEqual(attempt.evidence_grade, EvidenceGrade.SYNTHETIC)
                self.assertEqual(attempt.evidence_source, "test-seeded-model")

            event_count = len(ledger.events())
            second = execute_paper_plan(
                plan=current,
                trigger_id="trigger-1",
                config=model,
                ledger=ledger,
                started_at=STARTED_AT,
            )
            self.assertEqual(second, first)
            self.assertEqual(len(ledger.events()), event_count)

    def test_second_leg_rejection_stops_sequence_and_exposes_recovery_need(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"), action("a2"), action("a3"))
            result = execute_paper_plan(
                plan=current,
                trigger_id="trigger-reject",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={
                    "a1": observed(
                        "a1",
                        PaperAttemptOutcome.ACCEPTED,
                        odds="2.40",
                        stake="10.00",
                    ),
                    "a2": observed("a2", PaperAttemptOutcome.REJECTED),
                },
            )

            self.assertEqual(
                [item.outcome for item in result.attempts],
                [PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.REJECTED],
            )
            self.assertEqual(result.pending_action_ids, ("a3",))
            self.assertEqual(
                result.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )
            self.assertEqual(result.worst_case_exposure, Decimal("10.00"))

    def test_partial_fill_is_recorded_and_never_blindly_continues_next_leg(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"), action("a2"))
            result = execute_paper_plan(
                plan=current,
                trigger_id="trigger-partial",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={
                    "a1": observed(
                        "a1",
                        PaperAttemptOutcome.PARTIAL,
                        odds="2.30",
                        stake="4.00",
                    )
                },
            )

            self.assertEqual(len(result.attempts), 1)
            self.assertEqual(result.attempts[0].outcome, PaperAttemptOutcome.PARTIAL)
            self.assertEqual(result.attempts[0].execution_stake, Decimal("4.00"))
            self.assertEqual(result.pending_action_ids, ("a2",))
            self.assertEqual(result.worst_case_exposure, Decimal("4.00"))
            self.assertEqual(
                result.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )

    def test_unknown_attempt_uses_requested_stake_as_worst_case_and_does_not_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1", stake="12.50"))
            model = config(unknown_bps=10_000)

            first = execute_paper_plan(
                plan=current,
                trigger_id="trigger-unknown",
                config=model,
                ledger=ledger,
                started_at=STARTED_AT,
            )
            count = len(ledger.events())
            second = execute_paper_plan(
                plan=current,
                trigger_id="trigger-unknown",
                config=model,
                ledger=ledger,
                started_at=STARTED_AT,
            )

            self.assertEqual(first.attempts[0].outcome, PaperAttemptOutcome.UNKNOWN)
            self.assertEqual(first.worst_case_exposure, Decimal("12.50"))
            self.assertEqual(
                first.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )
            self.assertEqual(second, first)
            self.assertEqual(len(ledger.events()), count)

    def test_suspension_rejects_current_leg_and_preserves_remaining_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"), action("a2"), action("a3"))

            result = execute_paper_plan(
                plan=current,
                trigger_id="trigger-suspend",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=frozenset({"a2"}),
            )

            self.assertEqual(
                [item.outcome for item in result.attempts],
                [PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.REJECTED],
            )
            self.assertTrue(result.attempts[1].suspended)
            self.assertEqual(result.pending_action_ids, ("a3",))
            self.assertEqual(
                result.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )

    def test_stale_quote_fails_closed_without_claiming_a_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"))
            result = execute_paper_plan(
                plan=current,
                trigger_id="trigger-stale",
                config=config(max_quote_age_ms=500),
                ledger=ledger,
                started_at="2026-09-20T03:00:01+00:00",
            )

            attempt = result.attempts[0]
            self.assertEqual(attempt.outcome, PaperAttemptOutcome.REJECTED)
            self.assertIsNone(attempt.execution_odds)
            self.assertIsNone(attempt.execution_stake)
            self.assertEqual(result.worst_case_exposure, Decimal("0"))
            self.assertEqual(
                result.recovery_decision,
                RecoveryDecision.NO_EXPOSURE,
            )

    def test_empirical_odds_move_is_preserved_instead_of_rewriting_decision_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1", odds="2.50"))
            result = execute_paper_plan(
                plan=current,
                trigger_id="trigger-odds-move",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={
                    "a1": observed(
                        "a1",
                        PaperAttemptOutcome.ACCEPTED,
                        odds="2.20",
                        stake="10.00",
                    )
                },
            )

            attempt = result.attempts[0]
            self.assertEqual(attempt.decision_odds, Decimal("2.50"))
            self.assertEqual(attempt.execution_odds, Decimal("2.20"))
            self.assertEqual(attempt.evidence_grade, EvidenceGrade.EMPIRICAL)
            self.assertEqual(
                attempt.evidence_source,
                "captured-paper-observation-v1",
            )

    def test_submillisecond_timestamp_precision_is_accepted_and_evidence_is_millisecond_floor(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"))
            result = execute_paper_plan(
                plan=current,
                trigger_id="trigger-sub-ms",
                config=config(),
                ledger=ledger,
                started_at="2026-09-20T03:00:00.100123+00:00",
                observations={
                    "a1": observed(
                        "a1",
                        PaperAttemptOutcome.ACCEPTED,
                        at="2026-09-20T03:00:00.250987+00:00",
                        odds="2.40",
                        stake="10.00",
                    )
                },
            )

            self.assertEqual(result.attempts[0].delay_ms, 150)
            self.assertEqual(result.attempts[0].quote_age_ms, 250)

    def test_restart_reads_same_completed_state_from_durable_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            current = plan(action("a1"), action("a2"))
            model = config()
            first = execute_paper_plan(
                plan=current,
                trigger_id="trigger-restart",
                config=model,
                ledger=PaperExecutionLedger(path),
                started_at=STARTED_AT,
            )

            restarted = execute_paper_plan(
                plan=current,
                trigger_id="trigger-restart",
                config=model,
                ledger=PaperExecutionLedger(path),
                started_at=STARTED_AT,
            )

            self.assertEqual(restarted, first)

    def test_tampered_event_fails_closed_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            current = plan(action("a1"))
            model = config()
            execute_paper_plan(
                plan=current,
                trigger_id="trigger-tamper",
                config=model,
                ledger=PaperExecutionLedger(path),
                started_at=STARTED_AT,
            )

            lines = path.read_text(encoding="utf-8").splitlines()
            event = json.loads(lines[1])
            event["payload"]["reason"] = "tampered"
            lines[1] = json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "digest mismatch",
            ):
                PaperExecutionLedger(path).events()

    def test_writer_lock_fails_closed_instead_of_creating_parallel_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            ledger._lock_path.write_text("occupied", encoding="utf-8")
            try:
                with self.assertRaisesRegex(
                    PaperExecutionStateError,
                    "writer lock exists",
                ):
                    execute_paper_plan(
                        plan=plan(action("a1")),
                        trigger_id="trigger-lock",
                        config=config(),
                        ledger=ledger,
                        started_at=STARTED_AT,
                    )
            finally:
                ledger._lock_path.unlink()

    def test_non_back_plan_fails_closed_until_liability_authority_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "supports BACK only",
            ):
                execute_paper_plan(
                    plan=plan(action("a1", side="LAY")),
                    trigger_id="trigger-lay",
                    config=config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                )

    def test_per_leg_market_identity_survives_for_canonical_external_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(
                action("a1", market_id="match-odds"),
                action("a2", market_id="total-goals"),
            )
            result = execute_paper_plan(
                plan=current,
                trigger_id="trigger-settlement-identity",
                config=config(max_slippage_bps=0),
                ledger=ledger,
                started_at=STARTED_AT,
            )

            self.assertEqual(
                [(item.event_id, item.market_id, item.selection_id) for item in result.attempts],
                [
                    ("event-1", "match-odds", "selection-a1"),
                    ("event-1", "total-goals", "selection-a2"),
                ],
            )
            self.assertTrue(result.all_actions_accepted)


if __name__ == "__main__":
    unittest.main()
