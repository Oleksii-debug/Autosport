from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    RecoveryDecision,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-21T21:58:00+00:00"
STARTED_AT = "2026-09-21T21:58:00.100000+00:00"
EXPIRES_AT = "2026-09-21T21:59:00+00:00"
TRIGGER_ID = "crash-prefix-paper-adoption"

_BEFORE_ATTEMPT = "before_attempt"
_AFTER_ATTEMPT = "after_durable_attempt_before_book"
_AFTER_BOOK = "after_book_phase_before_ack"
_PREFIXES = (_BEFORE_ATTEMPT, _AFTER_ATTEMPT, _AFTER_BOOK)


def _config(**overrides) -> PaperExecutionModelConfig:
    values = {
        "model_id": "paper-reality",
        "model_version": "crash-prefix-matrix-v1",
        "evidence_grade": EvidenceGrade.SYNTHETIC,
        "evidence_source": "crash-prefix-matrix-test",
        "seed": "crash-prefix-matrix-fixed-seed",
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


def _action(
    action_id: str = "crash-prefix-action",
    *,
    stake: str = "10.00",
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id=f"event-{action_id}",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side="BACK",
        requested_odds="2.50",
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _prepared(
    runtime: PaperExecutionAdoptionRuntime,
    *actions: ExecutionAction,
) -> PreparedPaperExecution:
    if not actions:
        actions = (_action(),)
    return runtime._mint_prepared(
        PreparedPaperExecution(
            execution_plan=ExecutionPlan(
                plan_id="crash-prefix-plan",
                bookmaker_profile_version="paper-profile-v1",
                decision_id="decision-crash-prefix",
                approval_id="paper-only-no-real-money",
                created_at=QUOTE_AT,
                actions=tuple(actions),
            ),
            exposure_bindings=tuple(
                PaperExposureBinding(
                    action_id=current.action_id,
                    sport="soccer",
                    bankroll_id="paper-bankroll",
                    currency="EUR",
                )
                for current in actions
            ),
            intent_evidence_json='{"schema":"crash-prefix-matrix-test"}',
        )
    )


def _runtime(
    root: Path,
    book: PaperBook,
    *,
    model: PaperExecutionModelConfig,
) -> PaperExecutionAdoptionRuntime:
    return PaperExecutionAdoptionRuntime(
        book=book,
        ledger=PaperExecutionLedger(root / "paper-execution.jsonl"),
        config=model,
        max_quote_age=timedelta(seconds=5),
        paper_book_path=root / "paper_book.json",
    )


def _book_semantics(book: PaperBook) -> tuple[object, ...]:
    """Project durable economic truth while ignoring intentionally random ticket UUIDs."""
    tickets = tuple(
        (
            str(ticket.stake),
            tuple(
                (
                    leg.event_id,
                    leg.market_id,
                    leg.selection_id,
                    str(leg.locked_odds),
                    leg.sport,
                )
                for leg in ticket.legs
            ),
            ticket.placed_at,
            ticket.settled_at,
            ticket.status.value,
            str(ticket.payout),
            ticket.strategy_reason,
            ticket.provider_source_ids,
            ticket.provider_accounts,
            ticket.bankroll_id,
            ticket.currency,
        )
        for ticket in book.tickets.values()
    )
    return (
        str(book.initial_bankroll),
        str(book.balance),
        tickets,
    )


class PaperExecutionCrashPrefixMatrixTests(unittest.TestCase):
    def _exercise_prefix(
        self,
        prefix: str,
        *,
        model: PaperExecutionModelConfig,
        actions: tuple[ExecutionAction, ...] | None = None,
        suspended_action_ids: frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path = root / "paper_book.json"
            ledger_path = root / "paper-execution.jsonl"
            current_actions = (_action(),) if actions is None else actions

            book = PaperBook("100.00")
            runtime = _runtime(root, book, model=model)
            prefix_result = None
            if prefix == _AFTER_ATTEMPT:
                prefix_result = runtime.execute(
                    prepared=_prepared(runtime, *current_actions),
                    trigger_id=TRIGGER_ID,
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                    suspended_action_ids=suspended_action_ids,
                )
                self.assertEqual(prefix_result.ticket_ids, ())
            elif prefix == _AFTER_BOOK:
                prefix_result = runtime.execute(
                    prepared=_prepared(runtime, *current_actions),
                    trigger_id=TRIGGER_ID,
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                    suspended_action_ids=suspended_action_ids,
                )
            elif prefix != _BEFORE_ATTEMPT:
                raise AssertionError(f"unknown crash prefix: {prefix}")

            # Simulated process loss: reconstruct every authority from durable bytes.
            recovered_book = PaperBook.load(book_path)
            recovered_runtime = _runtime(root, recovered_book, model=model)
            recovered = recovered_runtime.execute(
                prepared=_prepared(recovered_runtime, *current_actions),
                trigger_id=TRIGGER_ID,
                started_at=STARTED_AT,
                materialize_exposure=True,
                suspended_action_ids=suspended_action_ids,
            )
            if prefix_result is not None:
                self.assertEqual(prefix_result.run, recovered.run)

            stable_book_bytes = book_path.read_bytes()
            stable_ledger_bytes = ledger_path.read_bytes()
            stable_ticket_ids = recovered.ticket_ids
            stable_semantics = _book_semantics(recovered_book)

            # A second fresh-process recovery must be a pure idempotent read/reuse.
            second_book = PaperBook.load(book_path)
            second_runtime = _runtime(root, second_book, model=model)
            second = second_runtime.execute(
                prepared=_prepared(second_runtime, *current_actions),
                trigger_id=TRIGGER_ID,
                started_at=STARTED_AT,
                materialize_exposure=True,
                suspended_action_ids=suspended_action_ids,
            )
            self.assertEqual(recovered.run, second.run)
            self.assertEqual(stable_ticket_ids, second.ticket_ids)
            self.assertEqual(stable_book_bytes, book_path.read_bytes())
            self.assertEqual(stable_ledger_bytes, ledger_path.read_bytes())
            self.assertEqual(stable_semantics, _book_semantics(second_book))

            return {
                "run": recovered.run,
                "ledger_bytes": stable_ledger_bytes,
                "book_semantics": stable_semantics,
                "ticket_ids": stable_ticket_ids,
            }

    def test_accepted_attempt_converges_across_all_crash_prefixes(self) -> None:
        states = {
            prefix: self._exercise_prefix(prefix, model=_config())
            for prefix in _PREFIXES
        }
        baseline = states[_BEFORE_ATTEMPT]
        for prefix, state in states.items():
            with self.subTest(prefix=prefix):
                self.assertEqual(state["run"], baseline["run"])
                self.assertEqual(state["ledger_bytes"], baseline["ledger_bytes"])
                self.assertEqual(state["book_semantics"], baseline["book_semantics"])
                self.assertEqual(len(state["ticket_ids"]), 1)

        run = baseline["run"]
        self.assertEqual(run.attempts[0].outcome, PaperAttemptOutcome.ACCEPTED)
        self.assertEqual(run.recovery_decision, RecoveryDecision.NONE)
        self.assertEqual(str(run.worst_case_exposure), "10.00")
        self.assertEqual(baseline["book_semantics"][1], "90.00")

    def test_accepted_then_rejected_plan_converges_without_duplicate_exposure(self) -> None:
        actions = (
            _action("crash-prefix-action-1", stake="7.00"),
            _action("crash-prefix-action-2", stake="11.00"),
        )
        suspended = frozenset({"crash-prefix-action-2"})
        states = {
            prefix: self._exercise_prefix(
                prefix,
                model=_config(),
                actions=actions,
                suspended_action_ids=suspended,
            )
            for prefix in _PREFIXES
        }
        baseline = states[_BEFORE_ATTEMPT]
        for prefix, state in states.items():
            with self.subTest(prefix=prefix):
                self.assertEqual(state["run"], baseline["run"])
                self.assertEqual(state["ledger_bytes"], baseline["ledger_bytes"])
                self.assertEqual(state["book_semantics"], baseline["book_semantics"])
                self.assertEqual(len(state["ticket_ids"]), 1)

        run = baseline["run"]
        self.assertEqual(
            tuple(attempt.outcome for attempt in run.attempts),
            (
                PaperAttemptOutcome.ACCEPTED,
                PaperAttemptOutcome.REJECTED,
            ),
        )
        self.assertEqual(run.pending_action_ids, ())
        self.assertEqual(
            run.recovery_decision,
            RecoveryDecision.HEDGE_REVIEW_REQUIRED,
        )
        self.assertEqual(str(run.worst_case_exposure), "7.00")
        self.assertEqual(baseline["book_semantics"][1], "93.00")
        self.assertEqual(len(baseline["book_semantics"][2]), 1)

    def test_unknown_attempt_remains_negative_evidence_without_ghost_exposure(self) -> None:
        states = {
            prefix: self._exercise_prefix(
                prefix,
                model=_config(unknown_bps=10_000),
            )
            for prefix in _PREFIXES
        }
        baseline = states[_BEFORE_ATTEMPT]
        for prefix, state in states.items():
            with self.subTest(prefix=prefix):
                self.assertEqual(state["run"], baseline["run"])
                self.assertEqual(state["ledger_bytes"], baseline["ledger_bytes"])
                self.assertEqual(state["book_semantics"], baseline["book_semantics"])
                self.assertEqual(state["ticket_ids"], ())

        run = baseline["run"]
        self.assertEqual(run.attempts[0].outcome, PaperAttemptOutcome.UNKNOWN)
        self.assertEqual(
            run.recovery_decision,
            RecoveryDecision.HEDGE_REVIEW_REQUIRED,
        )
        self.assertEqual(str(run.worst_case_exposure), "10.00")
        self.assertEqual(baseline["book_semantics"][1], "100.00")
        self.assertEqual(baseline["book_semantics"][2], ())


if __name__ == "__main__":
    unittest.main()
