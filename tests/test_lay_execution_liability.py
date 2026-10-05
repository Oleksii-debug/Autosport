from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.exchange_exposure import locked_capital_for_exchange_side
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
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


def _action(
    action_id: str = "lay-1",
    *,
    side: str = "LAY",
    odds: str = "5.00",
    stake: str = "10.00",
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-exchange",
        account_id="paper-account",
        event_id="event-1",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side=side,
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="lay-paper-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="decision-1",
        approval_id="paper-only",
        created_at=QUOTE_AT,
        actions=tuple(actions),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="lay-liability-1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="test-seeded-model",
        seed="fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _registered_observation(
    ledger: PaperExecutionLedger,
    source_action: ExecutionAction,
    outcome: PaperAttemptOutcome,
    *,
    odds: str | None = None,
    stake: str | None = None,
    grade: EvidenceGrade = EvidenceGrade.EMPIRICAL,
):
    record = PaperExecutionEvidenceRecord(
        action_id=source_action.action_id,
        bookmaker_id=source_action.bookmaker_id,
        account_id=source_action.account_id,
        event_id=source_action.event_id,
        market_id=source_action.market_id,
        selection_id=source_action.selection_id,
        side=source_action.side,
        quote_id=source_action.quote_id,
        outcome=outcome,
        observed_at="2026-09-20T03:00:00.250000+00:00",
        evidence_grade=grade,
        evidence_source="captured-paper-observation-v1",
        accepted_odds=odds,
        accepted_stake=stake,
        reason=f"observed {outcome.value.lower()}",
    )
    registry = PaperExecutionEvidenceRegistry(ledger)
    registry.register(record)
    return record.as_observation(), registry


class ExchangeLockedCapitalTests(unittest.TestCase):
    def test_back_locks_stake_exactly(self):
        stake = Decimal("10.000")
        self.assertEqual(
            locked_capital_for_exchange_side(
                stake=stake,
                odds=Decimal("5.00"),
                exchange_side="BACK",
            ),
            stake,
        )

    def test_lay_locks_exact_liability_without_quantization(self):
        self.assertEqual(
            locked_capital_for_exchange_side(
                stake=Decimal("10.00"),
                odds=Decimal("5.00"),
                exchange_side="lay",
            ),
            Decimal("40.0000"),
        )

    def test_invalid_or_inexact_inputs_fail_closed(self):
        with self.assertRaises(TypeError):
            locked_capital_for_exchange_side(
                stake=10.0,
                odds=Decimal("2"),
                exchange_side="LAY",
            )
        with self.assertRaises(ValueError):
            locked_capital_for_exchange_side(
                stake=Decimal("10"),
                odds=Decimal("2"),
                exchange_side="UNKNOWN",
            )
        with self.assertRaises(ValueError):
            locked_capital_for_exchange_side(
                stake=Decimal("10"),
                odds=Decimal("NaN"),
                exchange_side="LAY",
            )


class LayExecutionLiabilityTests(unittest.TestCase):
    def test_empirical_accepted_lay_uses_liability_and_survives_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action(odds="5.00", stake="10.00")
            current = _plan(source_action)
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            first = execute_paper_plan(
                plan=current,
                trigger_id="lay-accepted",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={source_action.action_id: observation},
                evidence_registry=registry,
            )
            event_count = len(ledger.events())
            resumed = execute_paper_plan(
                plan=current,
                trigger_id="lay-accepted",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={source_action.action_id: observation},
                evidence_registry=registry,
            )

            self.assertEqual(first.attempts[0].side, "LAY")
            self.assertEqual(first.attempts[0].execution_stake, Decimal("10.00"))
            self.assertEqual(first.attempts[0].execution_odds, Decimal("5.00"))
            self.assertEqual(first.worst_case_exposure, Decimal("40.0000"))
            self.assertEqual(first.recovery_decision, RecoveryDecision.NONE)
            self.assertEqual(resumed, first)
            self.assertEqual(len(ledger.events()), event_count)

    def test_empirical_partial_lay_uses_accepted_partial_liability(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action(odds="5.00", stake="10.00")
            current = _plan(source_action)
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.PARTIAL,
                odds="4.50",
                stake="4.00",
            )
            result = execute_paper_plan(
                plan=current,
                trigger_id="lay-partial",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={source_action.action_id: observation},
                evidence_registry=registry,
            )

            self.assertEqual(result.attempts[0].execution_stake, Decimal("4.00"))
            self.assertEqual(result.worst_case_exposure, Decimal("14.0000"))
            self.assertEqual(
                result.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )

    def test_synthetic_lay_fails_before_run_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "explicit empirical execution evidence",
            ):
                execute_paper_plan(
                    plan=_plan(_action()),
                    trigger_id="lay-synthetic",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                )
            self.assertEqual(ledger.events(), ())

    def test_configured_lay_and_unknown_lay_fail_before_run_reservation(self):
        for grade, outcome, odds, stake in (
            (EvidenceGrade.CONFIGURED, PaperAttemptOutcome.ACCEPTED, "5.00", "10.00"),
            (EvidenceGrade.EMPIRICAL, PaperAttemptOutcome.UNKNOWN, None, None),
        ):
            with self.subTest(grade=grade, outcome=outcome):
                with tempfile.TemporaryDirectory() as tmp:
                    ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
                    source_action = _action()
                    observation, registry = _registered_observation(
                        ledger,
                        source_action,
                        outcome,
                        odds=odds,
                        stake=stake,
                        grade=grade,
                    )
                    before = len(ledger.events())
                    with self.assertRaises(PaperExecutionStateError):
                        execute_paper_plan(
                            plan=_plan(source_action),
                            trigger_id=f"lay-{grade.value}-{outcome.value}",
                            config=_config(),
                            ledger=ledger,
                            started_at=STARTED_AT,
                            observations={source_action.action_id: observation},
                            evidence_registry=registry,
                        )
                    self.assertEqual(len(ledger.events()), before)

    def test_mixed_or_multileg_lay_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            lay = _action("lay")
            back = _action("back", side="BACK", odds="2.00", stake="5.00")
            observation, registry = _registered_observation(
                ledger,
                lay,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            before = len(ledger.events())
            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "single-leg",
            ):
                execute_paper_plan(
                    plan=_plan(lay, back),
                    trigger_id="mixed-lay",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={lay.action_id: observation},
                    evidence_registry=registry,
                )
            self.assertEqual(len(ledger.events()), before)

    def test_back_regression_keeps_stake_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            result = execute_paper_plan(
                plan=_plan(_action("back", side="BACK", odds="5.00", stake="10.00")),
                trigger_id="back-regression",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )
            self.assertEqual(result.worst_case_exposure, Decimal("10.00"))


if __name__ == "__main__":
    unittest.main()
