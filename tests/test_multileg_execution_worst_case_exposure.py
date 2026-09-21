from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from autosport.domain import TicketLeg
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
from autosport.portfolio import PortfolioEngine
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-21T09:30:00+00:00"
STARTED_AT = "2026-09-21T09:30:00.100000+00:00"
EXPIRES_AT = "2026-09-21T09:31:00+00:00"


def _action(
    action_id: str,
    selection_id: str,
    *,
    odds: str = "2.10",
    stake: str = "50.00",
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id=f"paper-venue-{action_id}",
        account_id=f"paper-account-{action_id}",
        event_id="event-final",
        market_id="winner",
        selection_id=selection_id,
        side="BACK",
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="wp-e03-execution-reality",
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
    *actions: ExecutionAction,
) -> PreparedPaperExecution:
    return runtime._mint_prepared(
        PreparedPaperExecution(
            execution_plan=ExecutionPlan(
                plan_id="wp-e03-plan",
                bookmaker_profile_version="paper-profile-v1",
                decision_id="wp-e03-decision",
                approval_id="paper-only-no-real-money",
                created_at=QUOTE_AT,
                actions=tuple(actions),
            ),
            exposure_bindings=tuple(
                PaperExposureBinding(
                    action_id=item.action_id,
                    sport="soccer",
                    bankroll_id="paper-bankroll",
                    currency="EUR",
                )
                for item in actions
            ),
            intent_evidence_json='{"schema":"wp-e03-test-intent"}',
        )
    )


def _evidence(
    action: ExecutionAction,
    outcome: PaperAttemptOutcome,
    *,
    accepted_odds: str | None = None,
    accepted_stake: str | None = None,
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
        accepted_odds=accepted_odds,
        accepted_stake=accepted_stake,
        reason=f"WP-E03 {outcome.value.lower()}",
    )


def _runtime(tmp: str) -> tuple[PaperBook, PaperExecutionLedger, PaperExecutionAdoptionRuntime]:
    book = PaperBook("1000.00")
    ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
    runtime = PaperExecutionAdoptionRuntime(
        book=book,
        ledger=ledger,
        config=_config(),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=Path(tmp) / "paper-book.json",
    )
    return book, ledger, runtime


def _winner_states(first_quote_key: str, second_quote_key: str) -> tuple[dict[str, str], ...]:
    return (
        {first_quote_key: "win", second_quote_key: "loss"},
        {first_quote_key: "loss", second_quote_key: "win"},
    )


def _terminal_profits(book: PaperBook, states: tuple[dict[str, str], ...]) -> tuple[Decimal, ...]:
    tickets = list(book.tickets.values())
    return tuple(
        PortfolioEngine.scenario_profit_settlements(tickets, state)
        for state in states
    )


class MultiLegExecutionWorstCaseExposureTests(unittest.TestCase):
    def test_full_two_way_dutch_is_positive_only_after_both_exact_fills_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = _runtime(tmp)
            first = _action("a", "home")
            second = _action("b", "away")
            first_evidence = _evidence(
                first,
                PaperAttemptOutcome.ACCEPTED,
                accepted_odds="2.10",
                accepted_stake="50.00",
            )
            second_evidence = _evidence(
                second,
                PaperAttemptOutcome.ACCEPTED,
                accepted_odds="2.10",
                accepted_stake="50.00",
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(first_evidence)
            registry.register(second_evidence)

            result = runtime.execute(
                prepared=_prepared(runtime, first, second),
                trigger_id="wp-e03-full",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={
                    first.action_id: first_evidence.as_observation(),
                    second.action_id: second_evidence.as_observation(),
                },
                evidence_registry=registry,
            )

            tickets = list(book.tickets.values())
            self.assertEqual(len(tickets), 2)
            states = _winner_states(
                tickets[0].legs[0].quote_key,
                tickets[1].legs[0].quote_key,
            )
            profits = _terminal_profits(book, states)
            self.assertEqual(profits, (Decimal("5.0000"), Decimal("5.0000")))
            self.assertGreater(min(profits), Decimal("0"))
            self.assertTrue(result.run.all_actions_accepted)
            self.assertEqual(result.run.recovery_decision, RecoveryDecision.NONE)

    def test_second_leg_rejection_destroys_positive_minimum_pnl_and_requires_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = _runtime(tmp)
            first = _action("a", "home")
            second = _action("b", "away")
            first_evidence = _evidence(
                first,
                PaperAttemptOutcome.ACCEPTED,
                accepted_odds="2.10",
                accepted_stake="50.00",
            )
            second_evidence = _evidence(second, PaperAttemptOutcome.REJECTED)
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(first_evidence)
            registry.register(second_evidence)

            result = runtime.execute(
                prepared=_prepared(runtime, first, second),
                trigger_id="wp-e03-reject",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={
                    first.action_id: first_evidence.as_observation(),
                    second.action_id: second_evidence.as_observation(),
                },
                evidence_registry=registry,
            )

            self.assertEqual(len(book.tickets), 1)
            ticket = next(iter(book.tickets.values()))
            missing_quote_key = TicketLeg(
                second.event_id,
                second.market_id,
                second.selection_id,
                second.requested_odds,
                sport="soccer",
            ).quote_key
            states = _winner_states(ticket.legs[0].quote_key, missing_quote_key)
            profits = _terminal_profits(book, states)
            self.assertEqual(profits, (Decimal("55.0000"), Decimal("-50.00")))
            self.assertLess(min(profits), Decimal("0"))
            self.assertEqual(result.run.worst_case_exposure, Decimal("50.00"))
            self.assertEqual(
                result.run.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )
            self.assertFalse(result.run.all_actions_accepted)

    def test_partial_first_leg_stops_sequence_and_scales_terminal_loss_to_actual_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = _runtime(tmp)
            first = _action("a", "home")
            second = _action("b", "away")
            partial = _evidence(
                first,
                PaperAttemptOutcome.PARTIAL,
                accepted_odds="2.10",
                accepted_stake="20.00",
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(partial)

            result = runtime.execute(
                prepared=_prepared(runtime, first, second),
                trigger_id="wp-e03-partial",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={first.action_id: partial.as_observation()},
                evidence_registry=registry,
            )

            self.assertEqual(len(result.run.attempts), 1)
            self.assertEqual(result.run.pending_action_ids, (second.action_id,))
            self.assertEqual(result.run.worst_case_exposure, Decimal("20.00"))
            self.assertEqual(
                result.run.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )
            self.assertEqual(len(book.tickets), 1)
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(ticket.stake, Decimal("20.00"))
            missing_quote_key = TicketLeg(
                second.event_id,
                second.market_id,
                second.selection_id,
                second.requested_odds,
                sport="soccer",
            ).quote_key
            profits = _terminal_profits(
                book,
                _winner_states(ticket.legs[0].quote_key, missing_quote_key),
            )
            self.assertEqual(profits, (Decimal("22.0000"), Decimal("-20.00")))
            self.assertEqual(min(profits), -result.run.worst_case_exposure)

    def test_unavailable_hedge_leg_preserves_original_position_downside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = _runtime(tmp)
            original = _action("open", "home", odds="2.00", stake="100.00")
            hedge = _action("hedge", "away", odds="2.10", stake="100.00")
            original_evidence = _evidence(
                original,
                PaperAttemptOutcome.ACCEPTED,
                accepted_odds="2.00",
                accepted_stake="100.00",
            )
            hedge_rejection = _evidence(hedge, PaperAttemptOutcome.REJECTED)
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(original_evidence)
            registry.register(hedge_rejection)

            result = runtime.execute(
                prepared=_prepared(runtime, original, hedge),
                trigger_id="wp-e03-hedge-unavailable",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={
                    original.action_id: original_evidence.as_observation(),
                    hedge.action_id: hedge_rejection.as_observation(),
                },
                evidence_registry=registry,
            )

            self.assertEqual(len(book.tickets), 1)
            ticket = next(iter(book.tickets.values()))
            unavailable_hedge_key = TicketLeg(
                hedge.event_id,
                hedge.market_id,
                hedge.selection_id,
                hedge.requested_odds,
                sport="soccer",
            ).quote_key
            profits = _terminal_profits(
                book,
                _winner_states(ticket.legs[0].quote_key, unavailable_hedge_key),
            )
            self.assertEqual(profits, (Decimal("100.0000"), Decimal("-100.00")))
            self.assertEqual(result.run.worst_case_exposure, Decimal("100.00"))
            self.assertEqual(min(profits), -result.run.worst_case_exposure)
            self.assertEqual(
                result.run.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )

    def test_provider_settlement_asymmetry_can_break_nominal_two_way_arbitrage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = _runtime(tmp)
            first = _action("a", "home")
            second = _action("b", "away")
            first_evidence = _evidence(
                first,
                PaperAttemptOutcome.ACCEPTED,
                accepted_odds="2.10",
                accepted_stake="50.00",
            )
            second_evidence = _evidence(
                second,
                PaperAttemptOutcome.ACCEPTED,
                accepted_odds="2.10",
                accepted_stake="50.00",
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(first_evidence)
            registry.register(second_evidence)
            runtime.execute(
                prepared=_prepared(runtime, first, second),
                trigger_id="wp-e03-settlement-asymmetry",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={
                    first.action_id: first_evidence.as_observation(),
                    second.action_id: second_evidence.as_observation(),
                },
                evidence_registry=registry,
            )

            tickets = list(book.tickets.values())
            first_key = tickets[0].legs[0].quote_key
            second_key = tickets[1].legs[0].quote_key
            ordinary = _terminal_profits(book, _winner_states(first_key, second_key))
            asymmetric = PortfolioEngine.scenario_profit_settlements(
                tickets,
                {first_key: "void", second_key: "loss"},
            )
            self.assertGreater(min(ordinary), Decimal("0"))
            self.assertEqual(asymmetric, Decimal("-50.00"))
            self.assertLess(asymmetric, Decimal("0"))

    def test_unknown_attempt_keeps_potential_exposure_even_without_materialized_ticket(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = _runtime(tmp)
            first = _action("a", "home")
            unknown = _evidence(first, PaperAttemptOutcome.UNKNOWN)
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(unknown)

            result = runtime.execute(
                prepared=_prepared(runtime, first),
                trigger_id="wp-e03-unknown",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={first.action_id: unknown.as_observation()},
                evidence_registry=registry,
            )

            self.assertEqual(book.tickets, {})
            self.assertEqual(result.run.worst_case_exposure, Decimal("50.00"))
            self.assertEqual(
                result.run.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )
            self.assertEqual(
                PortfolioEngine.scenario_profit_settlements([], {}),
                Decimal("0"),
            )
            self.assertNotEqual(result.run.worst_case_exposure, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
