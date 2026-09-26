from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

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
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    execute_paper_plan,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-21T10:30:00+00:00"
STARTED_AT = "2026-09-21T10:30:00.100000+00:00"
EXPIRES_AT = "2026-09-21T10:31:00+00:00"


def _action(
    action_id: str,
    *,
    selection: str,
    stake: str,
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id=f"paper-venue-{action_id}",
        account_id=f"paper-account-{action_id}",
        event_id="event-scope",
        market_id="winner",
        selection_id=selection,
        side="BACK",
        requested_odds="2.10",
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="scope-test-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="scope-test-decision",
        approval_id="paper-only-no-real-money",
        created_at=QUOTE_AT,
        actions=tuple(actions),
    )


def _config(**overrides: object) -> PaperExecutionModelConfig:
    values = {
        "model_id": "scope-test-reality",
        "model_version": "1",
        "evidence_grade": EvidenceGrade.SYNTHETIC,
        "evidence_source": "scope-test",
        "seed": "scope-fixed-seed",
        "max_quote_age_ms": 5_000,
        "min_delay_ms": 100,
        "max_delay_ms": 100,
        "rejected_bps": 0,
        "partial_bps": 0,
        "unknown_bps": 0,
        "partial_fill_bps": 5_000,
        "max_slippage_bps": 0,
    }
    values.update(overrides)
    return PaperExecutionModelConfig(**values)


def _evidence(
    action: ExecutionAction,
    outcome: PaperAttemptOutcome,
    *,
    accepted_stake: str | None = None,
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
        evidence_source=f"scope-{outcome.value.lower()}",
        accepted_odds="2.10" if accepted else None,
        accepted_stake=accepted_stake if accepted else None,
        reason=f"scope test {outcome.value.lower()}",
    )


class DurableRealizedExposureScopeTests(unittest.TestCase):
    def _runtime(self, root: Path, *, config: PaperExecutionModelConfig):
        book = PaperBook("1000.00")
        ledger = PaperExecutionLedger(root / "paper-execution.jsonl")
        book_path = root / "paper-book.json"
        runtime = PaperExecutionAdoptionRuntime(
            book=book,
            ledger=ledger,
            config=config,
            max_quote_age=timedelta(seconds=5),
            paper_book_path=book_path,
        )
        return book, ledger, runtime, book_path

    @staticmethod
    def _prepared(
        runtime: PaperExecutionAdoptionRuntime,
        plan: ExecutionPlan,
        bindings: tuple[PaperExposureBinding, ...],
    ) -> PreparedPaperExecution:
        return runtime._mint_prepared(
            PreparedPaperExecution(
                execution_plan=plan,
                exposure_bindings=bindings,
                intent_evidence_json='{"schema":"scope-test-intent"}',
            )
        )

    def _accepted_then_unknown(self, root: Path):
        config = _config()
        book, ledger, runtime, book_path = self._runtime(root, config=config)
        a1 = _action("a1", selection="home", stake="7.25")
        a2 = _action("a2", selection="away", stake="11.75")
        plan = _plan(a1, a2)
        bindings = (
            PaperExposureBinding(
                action_id=a1.action_id,
                sport="soccer",
                bankroll_id="bank-eur",
                currency="EUR",
            ),
            PaperExposureBinding(
                action_id=a2.action_id,
                sport="soccer",
                bankroll_id="bank-usd",
                currency="USD",
            ),
        )
        prepared = self._prepared(runtime, plan, bindings)
        e1 = _evidence(a1, PaperAttemptOutcome.ACCEPTED, accepted_stake="7.25")
        e2 = _evidence(a2, PaperAttemptOutcome.UNKNOWN)
        registry = PaperExecutionEvidenceRegistry(ledger)
        registry.register(e1)
        registry.register(e2)
        result = runtime.execute(
            prepared=prepared,
            trigger_id="scope-trigger",
            started_at=STARTED_AT,
            materialize_exposure=True,
            observations={
                a1.action_id: e1.as_observation(),
                a2.action_id: e2.as_observation(),
            },
            evidence_registry=registry,
        )
        return (
            book,
            ledger,
            runtime,
            book_path,
            config,
            plan,
            bindings,
            prepared,
            result,
        )

    def test_durable_scope_survives_unknown_and_projection_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                _book,
                ledger,
                _runtime,
                book_path,
                config,
                plan,
                _bindings,
                _prepared,
                result,
            ) = self._accepted_then_unknown(root)

            first = project_multileg_realized_exposure(
                ledger=ledger,
                plan=plan,
                config=config,
                run_id=result.run.run_id,
                paper_book_path=book_path,
            )
            restarted = project_multileg_realized_exposure(
                ledger=PaperExecutionLedger(root / "paper-execution.jsonl"),
                plan=plan,
                config=config,
                run_id=result.run.run_id,
                paper_book_path=book_path,
            )

            self.assertEqual(first, restarted)
            self.assertTrue(first.economic_scope_authoritative)
            self.assertEqual(
                tuple(leg.state for leg in first.legs),
                (RealizedLegState.ACCEPTED, RealizedLegState.UNKNOWN),
            )
            self.assertEqual(
                (
                    first.legs[0].sport,
                    first.legs[0].bankroll_id,
                    first.legs[0].currency,
                    first.legs[0].economic_scope_authoritative,
                ),
                ("soccer", "bank-eur", "EUR", True),
            )
            self.assertEqual(
                (
                    first.legs[1].sport,
                    first.legs[1].bankroll_id,
                    first.legs[1].currency,
                    first.legs[1].economic_scope_authoritative,
                ),
                ("soccer", "bank-usd", "USD", True),
            )
            self.assertEqual(first.confirmed_matched_stake, Decimal("7.25"))
            self.assertEqual(
                first.unresolved_unknown_stake_upper_bound,
                Decimal("11.75"),
            )
            self.assertEqual(
                first.worst_case_execution_exposure,
                Decimal("19.00"),
            )

    def test_restart_scope_substitution_conflicts_with_durable_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                _book,
                ledger,
                runtime,
                book_path,
                config,
                plan,
                bindings,
                _prepared,
                _result,
            ) = self._accepted_then_unknown(root)

            restarted = PaperExecutionAdoptionRuntime(
                book=PaperBook.load(book_path),
                ledger=ledger,
                config=config,
                max_quote_age=runtime.max_quote_age,
                paper_book_path=book_path,
            )
            forged_bindings = (
                bindings[0],
                replace(bindings[1], currency="GBP"),
            )
            forged = self._prepared(restarted, plan, forged_bindings)
            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "event_key already has different payload",
            ):
                restarted.execute(
                    prepared=forged,
                    trigger_id="scope-trigger",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                )

    def test_materialized_ticket_scope_substitution_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                book,
                ledger,
                _runtime,
                book_path,
                config,
                plan,
                _bindings,
                _prepared,
                result,
            ) = self._accepted_then_unknown(root)

            ticket_id = result.ticket_ids[0]
            ticket = book.tickets[ticket_id]
            book.tickets[ticket_id] = replace(ticket, currency="GBP")
            book.save(book_path)

            with self.assertRaisesRegex(
                MultiLegExposureProjectionError,
                "economic scope",
            ):
                project_multileg_realized_exposure(
                    ledger=ledger,
                    plan=plan,
                    config=config,
                    run_id=result.run.run_id,
                    paper_book_path=book_path,
                )

    def test_direct_623_run_remains_explicitly_unscoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = PaperExecutionLedger(root / "paper-execution.jsonl")
            action = _action("legacy", selection="home", stake="12.50")
            plan = _plan(action)
            config = _config(unknown_bps=10_000)
            run = execute_paper_plan(
                plan=plan,
                trigger_id="legacy-direct-trigger",
                config=config,
                ledger=ledger,
                started_at=STARTED_AT,
            )
            book_path = root / "paper-book.json"
            PaperBook("1000.00").save(book_path)

            projected = project_multileg_realized_exposure(
                ledger=ledger,
                plan=plan,
                config=config,
                run_id=run.run_id,
                paper_book_path=book_path,
            )

            self.assertFalse(projected.economic_scope_authoritative)
            self.assertFalse(projected.legs[0].economic_scope_authoritative)
            self.assertIsNone(projected.legs[0].sport)
            self.assertIsNone(projected.legs[0].bankroll_id)
            self.assertIsNone(projected.legs[0].currency)
            self.assertEqual(
                projected.unresolved_unknown_stake_upper_bound,
                Decimal("12.50"),
            )


if __name__ == "__main__":
    unittest.main()
