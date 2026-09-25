from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.domain import TicketLeg
from autosport.multileg_realized_exposure import (
    MultiLegExposureProjectionError,
    RealizedLegState,
    project_multileg_realized_exposure,
)
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    RecoveryDecision,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-21T10:30:00+00:00"
STARTED_AT = "2026-09-21T10:30:00.100000+00:00"
EXPIRES_AT = "2026-09-21T10:31:00+00:00"


def _action(
    action_id: str,
    selection_id: str,
    *,
    stake: str = "50.00",
    odds: str = "2.10",
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id=f"paper-venue-{action_id}",
        account_id=f"paper-account-{action_id}",
        event_id="event-e03a",
        market_id="winner",
        selection_id=selection_id,
        side="BACK",
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="wp-e03a-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="wp-e03a-decision",
        approval_id="paper-only-no-real-money",
        created_at=QUOTE_AT,
        actions=tuple(actions),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="wp-e03a-execution-reality",
        model_version="1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="wp-e03a-seeded-fallback",
        seed="wp-e03a-fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _prepared(
    runtime: PaperExecutionAdoptionRuntime,
    *actions: ExecutionAction,
) -> PreparedPaperExecution:
    return runtime._mint_prepared(
        PreparedPaperExecution(
            execution_plan=_plan(*actions),
            exposure_bindings=tuple(
                PaperExposureBinding(
                    action_id=action.action_id,
                    sport="soccer",
                    bankroll_id="paper-bankroll",
                    currency="EUR",
                )
                for action in actions
            ),
            intent_evidence_json='{"schema":"wp-e03a-test-intent"}',
        )
    )


def _evidence(
    action: ExecutionAction,
    outcome: PaperAttemptOutcome,
    *,
    accepted_stake: str | None = None,
    accepted_odds: str = "2.10",
) -> PaperExecutionEvidenceRecord:
    accepted = outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}
    return PaperExecutionEvidenceRecord(
        action_id=action.action_id,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        quote_id=action.quote_id,
        outcome=outcome,
        observed_at=STARTED_AT,
        evidence_grade=EvidenceGrade.CONFIGURED,
        evidence_source=f"wp-e03a-{action.action_id}-{outcome.value.lower()}",
        accepted_odds=accepted_odds if accepted else None,
        accepted_stake=accepted_stake if accepted else None,
        reason=f"WP-E03A {outcome.value.lower()}",
    )


def _runtime(root: Path):
    book = PaperBook("1000.00")
    ledger_path = root / "workspace" / "paper-execution.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    book_path = root / "paper-book.json"
    config = _config()
    ledger = PaperExecutionLedger(ledger_path)
    runtime = PaperExecutionAdoptionRuntime(
        book=book,
        ledger=ledger,
        config=config,
        max_quote_age=timedelta(seconds=5),
        paper_book_path=book_path,
    )
    return book, ledger, runtime, config, ledger_path, book_path


def _execute(
    *,
    root: Path,
    actions: tuple[ExecutionAction, ...],
    outcomes: tuple[tuple[PaperAttemptOutcome, str | None], ...],
    materialize: bool = True,
):
    book, ledger, runtime, config, ledger_path, book_path = _runtime(root)
    prepared = _prepared(runtime, *actions)
    registry = PaperExecutionEvidenceRegistry(ledger)
    observations = {}
    for action, (outcome, accepted_stake) in zip(actions, outcomes, strict=False):
        record = _evidence(action, outcome, accepted_stake=accepted_stake)
        registry.register(record)
        observations[action.action_id] = record.as_observation()
        if outcome is not PaperAttemptOutcome.ACCEPTED:
            break
    result = runtime.execute(
        prepared=prepared,
        trigger_id="wp-e03a-trigger",
        started_at=STARTED_AT,
        materialize_exposure=materialize,
        observations=observations,
        evidence_registry=registry,
    )
    return (
        book,
        ledger,
        prepared.execution_plan,
        result,
        config,
        ledger_path,
        book_path,
    )


class MultiLegRealizedExposureProjectionTests(unittest.TestCase):
    def test_full_acceptance_projects_exact_canonical_fills_and_restart_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(Path(tmp) / "authority")},
            clear=False,
        ):
            root = Path(tmp)
            actions = (_action("a", "home"), _action("b", "away"))
            _, ledger, plan, result, config, ledger_path, book_path = _execute(
                root=root,
                actions=actions,
                outcomes=(
                    (PaperAttemptOutcome.ACCEPTED, "50.00"),
                    (PaperAttemptOutcome.ACCEPTED, "50.00"),
                ),
            )

            first = project_multileg_realized_exposure(
                ledger=ledger,
                plan=plan,
                config=config,
                run_id=result.run.run_id,
                paper_book_path=book_path,
            )
            restarted = project_multileg_realized_exposure(
                ledger=PaperExecutionLedger(ledger_path),
                plan=plan,
                config=config,
                run_id=result.run.run_id,
                paper_book_path=book_path,
            )

            self.assertEqual(first, restarted)
            self.assertEqual(
                tuple(leg.state for leg in first.legs),
                (RealizedLegState.ACCEPTED, RealizedLegState.ACCEPTED),
            )
            self.assertEqual(first.confirmed_matched_stake, Decimal("100.00"))
            self.assertEqual(first.unresolved_unknown_stake_upper_bound, Decimal("0"))
            self.assertEqual(first.worst_case_execution_exposure, Decimal("100.00"))
            self.assertFalse(first.sequencing_exposure)
            self.assertTrue(first.all_actions_accepted)
            self.assertEqual(first.recovery_decision, RecoveryDecision.NONE)
            self.assertEqual(
                first.run_materialized_unconstrained_worst_case_pnl,
                Decimal("-100.00"),
            )
            self.assertEqual(first.complete_run_worst_case_pnl, Decimal("-100.00"))
            self.assertEqual(len(first.projection_sha256), 64)
            self.assertFalse(first.execution_authorized)
            self.assertFalse(first.provider_write_authorized)
            self.assertFalse(first.real_money_authorized)

    def test_accepted_then_rejected_keeps_realized_prefix_unsafe_and_pending_tail_unexecuted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(Path(tmp) / "authority")},
            clear=False,
        ):
            root = Path(tmp)
            actions = (
                _action("a", "home"),
                _action("b", "away"),
                _action("c", "draw"),
            )
            _, ledger, plan, result, config, _, book_path = _execute(
                root=root,
                actions=actions,
                outcomes=(
                    (PaperAttemptOutcome.ACCEPTED, "50.00"),
                    (PaperAttemptOutcome.REJECTED, None),
                ),
            )
            projected = project_multileg_realized_exposure(
                ledger=ledger,
                plan=plan,
                config=config,
                run_id=result.run.run_id,
                paper_book_path=book_path,
            )

            self.assertEqual(
                tuple(leg.state for leg in projected.legs),
                (
                    RealizedLegState.ACCEPTED,
                    RealizedLegState.REJECTED,
                    RealizedLegState.UNATTEMPTED,
                ),
            )
            self.assertEqual(projected.confirmed_matched_stake, Decimal("50.00"))
            self.assertEqual(projected.worst_case_execution_exposure, Decimal("50.00"))
            self.assertTrue(projected.sequencing_exposure)
            self.assertEqual(
                projected.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )
            self.assertEqual(projected.complete_run_worst_case_pnl, Decimal("-50.00"))

    def test_partial_first_leg_uses_exact_fill_and_never_counts_unattempted_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(Path(tmp) / "authority")},
            clear=False,
        ):
            root = Path(tmp)
            actions = (_action("a", "home"), _action("b", "away"))
            _, ledger, plan, result, config, _, book_path = _execute(
                root=root,
                actions=actions,
                outcomes=((PaperAttemptOutcome.PARTIAL, "20.00"),),
            )
            projected = project_multileg_realized_exposure(
                ledger=ledger,
                plan=plan,
                config=config,
                run_id=result.run.run_id,
                paper_book_path=book_path,
            )

            self.assertEqual(projected.legs[0].state, RealizedLegState.PARTIAL)
            self.assertEqual(projected.legs[0].realized_matched_stake, Decimal("20.00"))
            self.assertEqual(projected.legs[1].state, RealizedLegState.UNATTEMPTED)
            self.assertEqual(projected.worst_case_execution_exposure, Decimal("20.00"))
            self.assertEqual(projected.complete_run_worst_case_pnl, Decimal("-20.00"))
            self.assertTrue(projected.sequencing_exposure)

    def test_unknown_keeps_full_requested_upper_bound_and_withholds_complete_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(Path(tmp) / "authority")},
            clear=False,
        ):
            root = Path(tmp)
            actions = (
                _action("a", "home", stake="7.25"),
                _action("b", "away", stake="11.75"),
                _action("c", "draw", stake="5.00"),
            )
            _, ledger, plan, result, config, _, book_path = _execute(
                root=root,
                actions=actions,
                outcomes=(
                    (PaperAttemptOutcome.ACCEPTED, "7.25"),
                    (PaperAttemptOutcome.UNKNOWN, None),
                ),
            )
            projected = project_multileg_realized_exposure(
                ledger=ledger,
                plan=plan,
                config=config,
                run_id=result.run.run_id,
                paper_book_path=book_path,
            )

            self.assertEqual(projected.confirmed_matched_stake, Decimal("7.25"))
            self.assertEqual(
                projected.unresolved_unknown_stake_upper_bound,
                Decimal("11.75"),
            )
            self.assertEqual(projected.worst_case_execution_exposure, Decimal("19.00"))
            self.assertEqual(projected.legs[1].state, RealizedLegState.UNKNOWN)
            self.assertEqual(
                projected.legs[1].unresolved_stake_upper_bound,
                Decimal("11.75"),
            )
            self.assertEqual(
                projected.run_materialized_unconstrained_worst_case_pnl,
                Decimal("-7.25"),
            )
            self.assertIsNone(projected.complete_run_worst_case_pnl)
            self.assertTrue(projected.sequencing_exposure)

    def test_projection_refuses_accepted_attempt_without_646_materialized_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(Path(tmp) / "authority")},
            clear=False,
        ):
            root = Path(tmp)
            actions = (_action("a", "home"),)
            _, ledger, plan, result, config, _, book_path = _execute(
                root=root,
                actions=actions,
                outcomes=((PaperAttemptOutcome.ACCEPTED, "50.00"),),
                materialize=False,
            )
            with self.assertRaisesRegex(
                MultiLegExposureProjectionError,
                "bind exactly one durable PaperTicket",
            ):
                project_multileg_realized_exposure(
                    ledger=ledger,
                    plan=plan,
                    config=config,
                    run_id=result.run.run_id,
                    paper_book_path=book_path,
                )

    def test_projection_refuses_paperticket_for_unknown_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(Path(tmp) / "authority")},
            clear=False,
        ):
            root = Path(tmp)
            actions = (_action("a", "home"),)
            book, ledger, plan, result, config, _, book_path = _execute(
                root=root,
                actions=actions,
                outcomes=((PaperAttemptOutcome.UNKNOWN, None),),
            )
            attempt = result.run.attempts[0]
            book.open_ticket(
                [
                    TicketLeg(
                        attempt.event_id,
                        attempt.market_id,
                        attempt.selection_id,
                        Decimal("2.10"),
                        sport="soccer",
                    )
                ],
                Decimal("1.00"),
                reason=(
                    f"forged; decision_id={plan.decision_id}; run_id={result.run.run_id}; "
                    f"paper_execution_attempt_id={attempt.attempt_id}"
                ),
                placed_at=attempt.execution_observed_at,
                provider_source_ids=(attempt.bookmaker_id,),
                provider_accounts=((attempt.bookmaker_id, attempt.account_id),),
                bankroll_id="paper-bankroll",
                currency="EUR",
            )
            book.save(book_path)

            with self.assertRaisesRegex(
                MultiLegExposureProjectionError,
                "UNKNOWN execution attempt must not materialize",
            ):
                project_multileg_realized_exposure(
                    ledger=ledger,
                    plan=plan,
                    config=config,
                    run_id=result.run.run_id,
                    paper_book_path=book_path,
                )

    def test_projection_refuses_stale_or_substituted_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(Path(tmp) / "authority")},
            clear=False,
        ):
            root = Path(tmp)
            actions = (_action("a", "home"),)
            _, ledger, plan, result, config, _, book_path = _execute(
                root=root,
                actions=actions,
                outcomes=((PaperAttemptOutcome.ACCEPTED, "50.00"),),
            )
            substituted = ExecutionPlan(
                plan_id=plan.plan_id,
                bookmaker_profile_version=plan.bookmaker_profile_version,
                decision_id=plan.decision_id,
                approval_id=plan.approval_id,
                created_at=plan.created_at,
                actions=(_action("a", "home", stake="49.99"),),
            )
            with self.assertRaisesRegex(
                MultiLegExposureProjectionError,
                "durable exposure-scope event does not bind exact execution plan",
            ):
                project_multileg_realized_exposure(
                    ledger=ledger,
                    plan=substituted,
                    config=config,
                    run_id=result.run.run_id,
                    paper_book_path=book_path,
                )

    def test_unrelated_open_book_exposure_is_included_in_portfolio_floor_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(Path(tmp) / "authority")},
            clear=False,
        ):
            root = Path(tmp)
            actions = (_action("a", "home", stake="20.00"),)
            book, ledger, plan, result, config, _, book_path = _execute(
                root=root,
                actions=actions,
                outcomes=((PaperAttemptOutcome.ACCEPTED, "20.00"),),
            )
            book.open_ticket(
                [
                    TicketLeg(
                        "other-event",
                        "other-market",
                        "other-selection",
                        Decimal("3.00"),
                        sport="soccer",
                    )
                ],
                Decimal("5.00"),
                reason="unrelated canonical PAPER exposure",
                provider_source_ids=("other-provider",),
                provider_accounts=(("other-provider", "other-account"),),
                bankroll_id="paper-bankroll",
                currency="EUR",
            )
            book.save(book_path)

            projected = project_multileg_realized_exposure(
                ledger=ledger,
                plan=plan,
                config=config,
                run_id=result.run.run_id,
                paper_book_path=book_path,
            )
            self.assertEqual(
                projected.run_materialized_unconstrained_worst_case_pnl,
                Decimal("-20.00"),
            )
            self.assertEqual(
                projected.portfolio_materialized_unconstrained_worst_case_pnl,
                Decimal("-25.00"),
            )
            self.assertEqual(projected.complete_run_worst_case_pnl, Decimal("-20.00"))


if __name__ == "__main__":
    unittest.main()