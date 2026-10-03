from __future__ import annotations

import tempfile
from datetime import timedelta
from decimal import Decimal
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
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    RecoveryDecision,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-21T09:30:00+00:00"
STARTED_AT = "2026-09-21T09:30:00.100000+00:00"
EXPIRES_AT = "2026-09-21T09:31:00+00:00"


def _action(action_id: str, selection_id: str) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id=f"paper-venue-{action_id}",
        account_id=f"paper-account-{action_id}",
        event_id="event-final",
        market_id="winner",
        selection_id=selection_id,
        side="BACK",
        requested_odds="2.10",
        requested_stake="50.00",
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="wp-e03-cumulative-unknown",
        model_version="1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="wp-e03-seeded-fallback",
        seed="wp-e03-fixed-seed",
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
    first: ExecutionAction,
    second: ExecutionAction,
) -> PreparedPaperExecution:
    return runtime._mint_prepared(
        PreparedPaperExecution(
            execution_plan=ExecutionPlan(
                plan_id="wp-e03-cumulative-plan",
                bookmaker_profile_version="paper-profile-v1",
                decision_id="wp-e03-cumulative-decision",
                approval_id="paper-only-no-real-money",
                created_at=QUOTE_AT,
                actions=(first, second),
            ),
            exposure_bindings=(
                PaperExposureBinding(first.action_id, "soccer", "paper-bankroll", "EUR"),
                PaperExposureBinding(second.action_id, "soccer", "paper-bankroll", "EUR"),
            ),
            intent_evidence_json='{"schema":"wp-e03-cumulative-test-intent"}',
        )
    )


def _evidence(
    action: ExecutionAction,
    outcome: PaperAttemptOutcome,
    *,
    accepted: bool = False,
) -> PaperExecutionEvidenceRecord:
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
        evidence_source="wp-e03-observed-attempt",
        accepted_odds="2.10" if accepted else None,
        accepted_stake="50.00" if accepted else None,
        reason=f"WP-E03 {outcome.value.lower()}",
    )


def test_accepted_then_unknown_preserves_cumulative_exposure_across_resume() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        book_path = root / "paper-book.json"
        ledger = PaperExecutionLedger(root / "paper-execution.jsonl")
        config = _config()
        book = PaperBook("1000.00")
        runtime = PaperExecutionAdoptionRuntime(
            book=book,
            ledger=ledger,
            config=config,
            max_quote_age=timedelta(seconds=5),
            paper_book_path=book_path,
        )
        first = _action("a", "home")
        second = _action("b", "away")
        first_evidence = _evidence(
            first,
            PaperAttemptOutcome.ACCEPTED,
            accepted=True,
        )
        second_evidence = _evidence(second, PaperAttemptOutcome.UNKNOWN)
        registry = PaperExecutionEvidenceRegistry(ledger)
        registry.register(first_evidence)
        registry.register(second_evidence)

        result = runtime.execute(
            prepared=_prepared(runtime, first, second),
            trigger_id="wp-e03-cumulative-unknown",
            started_at=STARTED_AT,
            materialize_exposure=True,
            observations={
                first.action_id: first_evidence.as_observation(),
                second.action_id: second_evidence.as_observation(),
            },
            evidence_registry=registry,
        )

        assert len(result.run.attempts) == 2
        assert result.run.pending_action_ids == ()
        assert result.run.worst_case_exposure == Decimal("100.00")
        assert result.run.recovery_decision is RecoveryDecision.HEDGE_REVIEW_REQUIRED
        assert result.run.all_actions_accepted is False
        assert len(book.tickets) == 1
        ticket = next(iter(book.tickets.values()))
        assert ticket.stake == Decimal("50.00")
        assert ticket.legs[0].selection_id == "home"

        restarted_book = PaperBook.load(book_path)
        restarted = PaperExecutionAdoptionRuntime(
            book=restarted_book,
            ledger=ledger,
            config=config,
            max_quote_age=timedelta(seconds=5),
            paper_book_path=book_path,
        )
        resumed = restarted.execute(
            prepared=_prepared(restarted, first, second),
            trigger_id="wp-e03-cumulative-unknown",
            started_at=STARTED_AT,
            materialize_exposure=True,
            observations={
                first.action_id: first_evidence.as_observation(),
                second.action_id: second_evidence.as_observation(),
            },
            evidence_registry=PaperExecutionEvidenceRegistry(ledger),
        )

        assert resumed.run.run_id == result.run.run_id
        assert resumed.run.worst_case_exposure == Decimal("100.00")
        assert resumed.run.recovery_decision is RecoveryDecision.HEDGE_REVIEW_REQUIRED
        assert len(restarted_book.tickets) == 1
        resumed_ticket = next(iter(restarted_book.tickets.values()))
        assert resumed_ticket.ticket_id == ticket.ticket_id
        assert resumed_ticket.stake == Decimal("50.00")
