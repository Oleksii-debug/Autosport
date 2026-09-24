from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
    PaperLegAttempt,
    RecoveryDecision,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-21T12:00:00+00:00"
STARTED_AT = "2026-09-21T12:00:00.100000+00:00"
EXECUTED_AT = "2026-09-21T12:00:00.200000+00:00"
EXPIRES_AT = "2026-09-21T12:01:00+00:00"
RUN_ID = "wp-p06-inflight-run"
TRIGGER_ID = "wp-p06-trigger"


def _action(action_id: str, *, stake: str) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id=f"paper-venue-{action_id}",
        account_id=f"paper-account-{action_id}",
        event_id="event-p06",
        market_id="winner",
        selection_id=f"selection-{action_id}",
        side="BACK",
        requested_odds="2.10",
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="wp-p06-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="wp-p06-decision",
        approval_id="paper-only-no-real-money",
        created_at=QUOTE_AT,
        actions=tuple(actions),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="wp-p06-capital-risk",
        model_version="1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="wp-p06-durable-ledger-test",
        seed="wp-p06-fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _reserve(
    ledger: PaperExecutionLedger,
    plan: ExecutionPlan,
    config: PaperExecutionModelConfig,
) -> None:
    ledger.reserve_run(
        run_id=RUN_ID,
        trigger_id=TRIGGER_ID,
        plan=plan,
        config=config,
        started_at=STARTED_AT,
        observation_evidence_ids={},
    )


def _load(
    ledger: PaperExecutionLedger,
    plan: ExecutionPlan,
    config: PaperExecutionModelConfig,
):
    result = ledger.load_run(
        run_id=RUN_ID,
        trigger_id=TRIGGER_ID,
        plan=plan,
        config=config,
        started_at=STARTED_AT,
        observation_evidence_ids={},
    )
    if result is None:
        raise AssertionError("reserved run disappeared")
    return result


def _attempt(
    *,
    plan: ExecutionPlan,
    config: PaperExecutionModelConfig,
    sequence: int,
    outcome: PaperAttemptOutcome,
    execution_stake: str | None = None,
) -> PaperLegAttempt:
    action = plan.actions[sequence]
    if outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}:
        if execution_stake is None:
            raise AssertionError("filled outcome requires execution_stake")
        execution_odds: Decimal | None = Decimal("2.10")
        filled_stake: Decimal | None = Decimal(execution_stake)
    else:
        execution_odds = None
        filled_stake = None
    return PaperLegAttempt(
        attempt_id=f"wp-p06-attempt-{sequence}",
        run_id=RUN_ID,
        plan_id=plan.plan_id,
        action_id=action.action_id,
        sequence=sequence,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        decision_quote_id=action.quote_id,
        decision_odds=action.requested_odds,
        requested_stake=action.requested_stake,
        decision_observed_at=action.quote_observed_at,
        execution_observed_at=EXECUTED_AT,
        delay_ms=100,
        quote_age_ms=200,
        outcome=outcome,
        execution_odds=execution_odds,
        execution_stake=filled_stake,
        suspended=False,
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="wp-p06-durable-ledger-test",
        evidence_id=None,
        evidence_sha256=None,
        model_fingerprint=config.fingerprint,
        reason=f"WP-P06 {outcome.value.lower()}",
    )


class PartialMultiLegCapitalAtRiskTests(unittest.TestCase):
    def test_reservation_without_attempts_does_not_invent_executed_capital(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = _plan(_action("a", stake="30.00"), _action("b", stake="40.00"))
            model = _config()
            _reserve(ledger, current, model)

            restarted = _load(PaperExecutionLedger(path), current, model)

            self.assertFalse(restarted.completed)
            self.assertEqual(restarted.attempts, ())
            self.assertEqual(restarted.pending_action_ids, ("a", "b"))
            self.assertEqual(restarted.worst_case_exposure, Decimal("0"))
            self.assertEqual(restarted.recovery_decision, RecoveryDecision.NONE)

    def test_restart_after_accepted_prefix_preserves_realized_capital_at_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = _plan(_action("a", stake="30.00"), _action("b", stake="40.00"))
            model = _config()
            _reserve(ledger, current, model)
            ledger.record_attempt(
                _attempt(
                    plan=current,
                    config=model,
                    sequence=0,
                    outcome=PaperAttemptOutcome.ACCEPTED,
                    execution_stake="30.00",
                )
            )

            restarted = _load(PaperExecutionLedger(path), current, model)

            self.assertFalse(restarted.completed)
            self.assertEqual(restarted.pending_action_ids, ("b",))
            self.assertEqual(restarted.worst_case_exposure, Decimal("30.00"))
            self.assertEqual(
                restarted.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )

    def test_attempt_write_is_bound_to_exact_reserved_action_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = _plan(
                _action("a", stake="100.00"),
                _action("b", stake="25.00"),
            )
            model = _config()
            _reserve(ledger, current, model)
            canonical = _attempt(
                plan=current,
                config=model,
                sequence=0,
                outcome=PaperAttemptOutcome.ACCEPTED,
                execution_stake="100.00",
            )

            cases = (
                (
                    "understated stake",
                    {
                        "requested_stake": Decimal("1.00"),
                        "execution_stake": Decimal("1.00"),
                    },
                ),
                (
                    "overstated stake",
                    {
                        "requested_stake": Decimal("200.00"),
                        "execution_stake": Decimal("200.00"),
                    },
                ),
                ("decision odds drift", {"decision_odds": Decimal("3.00")}),
                ("bookmaker drift", {"bookmaker_id": "paper-venue-forged"}),
                ("account drift", {"account_id": "paper-account-forged"}),
                ("event drift", {"event_id": "event-forged"}),
                ("market drift", {"market_id": "market-forged"}),
                ("selection drift", {"selection_id": "selection-forged"}),
                ("side drift", {"side": "LAY"}),
                ("quote drift", {"decision_quote_id": "quote-forged"}),
                (
                    "quote observation drift",
                    {"decision_observed_at": "2026-09-21T11:59:59+00:00"},
                ),
                ("model drift", {"model_fingerprint": "forged-model"}),
            )
            for label, changes in cases:
                with self.subTest(label=label):
                    forged = replace(canonical, **changes)
                    with self.assertRaisesRegex(
                        PaperExecutionIntegrityError,
                        "reserved action/model binding",
                    ):
                        ledger.record_attempt(forged)

            restarted = _load(PaperExecutionLedger(path), current, model)
            self.assertEqual(restarted.attempts, ())
            self.assertEqual(restarted.worst_case_exposure, Decimal("0"))

    def test_restart_and_completion_reject_prebinding_forged_attempt_economics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = _plan(
                _action("a", stake="100.00"),
                _action("b", stake="25.00"),
            )
            model = _config()
            _reserve(ledger, current, model)
            canonical = _attempt(
                plan=current,
                config=model,
                sequence=0,
                outcome=PaperAttemptOutcome.ACCEPTED,
                execution_stake="100.00",
            )
            forged = replace(
                canonical,
                requested_stake=Decimal("1.00"),
                execution_stake=Decimal("1.00"),
            )

            # Simulate a durable ATTEMPT_RECORDED event produced by the
            # pre-binding implementation.  The repaired subclass must reject it
            # both when reconstructing restart truth and before completion can
            # turn its caller-authored economics into terminal authority.
            super(PaperExecutionLedger, ledger).record_attempt(forged)

            restarted_ledger = PaperExecutionLedger(path)
            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "reserved action/model binding",
            ):
                _load(restarted_ledger, current, model)

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "reserved action/model binding",
            ):
                restarted_ledger.complete_run(
                    run_id=RUN_ID,
                    pending_action_ids=("b",),
                    recovery_decision=RecoveryDecision.HEDGE_REVIEW_REQUIRED,
                    worst_case_exposure=Decimal("1.00"),
                )

    def test_partial_fill_uses_actual_fill_and_rejects_completion_understatement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = _plan(_action("a", stake="100.00"), _action("b", stake="25.00"))
            model = _config()
            _reserve(ledger, current, model)
            ledger.record_attempt(
                _attempt(
                    plan=current,
                    config=model,
                    sequence=0,
                    outcome=PaperAttemptOutcome.PARTIAL,
                    execution_stake="25.00",
                )
            )

            inflight = _load(PaperExecutionLedger(path), current, model)
            self.assertFalse(inflight.completed)
            self.assertEqual(inflight.pending_action_ids, ("b",))
            self.assertEqual(inflight.worst_case_exposure, Decimal("25.00"))
            self.assertEqual(
                inflight.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "worst_case_exposure conflicts",
            ):
                ledger.complete_run(
                    run_id=RUN_ID,
                    pending_action_ids=("b",),
                    recovery_decision=RecoveryDecision.HEDGE_REVIEW_REQUIRED,
                    worst_case_exposure=Decimal("0"),
                )

            ledger.complete_run(
                run_id=RUN_ID,
                pending_action_ids=("b",),
                recovery_decision=RecoveryDecision.HEDGE_REVIEW_REQUIRED,
                worst_case_exposure=Decimal("25.00"),
            )
            completed = _load(PaperExecutionLedger(path), current, model)
            self.assertTrue(completed.completed)
            self.assertEqual(completed.worst_case_exposure, Decimal("25.00"))

    def test_unknown_after_known_fill_includes_full_unresolved_requested_stake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = _plan(
                _action("a", stake="10.00"),
                _action("b", stake="40.00"),
                _action("c", stake="20.00"),
            )
            model = _config()
            _reserve(ledger, current, model)
            ledger.record_attempt(
                _attempt(
                    plan=current,
                    config=model,
                    sequence=0,
                    outcome=PaperAttemptOutcome.ACCEPTED,
                    execution_stake="10.00",
                )
            )
            ledger.record_attempt(
                _attempt(
                    plan=current,
                    config=model,
                    sequence=1,
                    outcome=PaperAttemptOutcome.UNKNOWN,
                )
            )

            restarted = _load(PaperExecutionLedger(path), current, model)

            self.assertFalse(restarted.completed)
            self.assertEqual(restarted.pending_action_ids, ("c",))
            self.assertEqual(restarted.worst_case_exposure, Decimal("50.00"))
            self.assertEqual(
                restarted.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )

    def test_rejection_before_any_fill_reports_no_executed_capital(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = _plan(_action("a", stake="30.00"), _action("b", stake="40.00"))
            model = _config()
            _reserve(ledger, current, model)
            ledger.record_attempt(
                _attempt(
                    plan=current,
                    config=model,
                    sequence=0,
                    outcome=PaperAttemptOutcome.REJECTED,
                )
            )

            restarted = _load(PaperExecutionLedger(path), current, model)

            self.assertFalse(restarted.completed)
            self.assertEqual(restarted.pending_action_ids, ("b",))
            self.assertEqual(restarted.worst_case_exposure, Decimal("0"))
            self.assertEqual(
                restarted.recovery_decision,
                RecoveryDecision.NO_EXPOSURE,
            )


if __name__ == "__main__":
    unittest.main()
