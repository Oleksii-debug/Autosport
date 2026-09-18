import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent, TicketLeg, TicketStatus
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.paper_strategy import Forecast, PaperValueAgent
from autosport.risk import (
    PaperRiskPolicy,
    ProposedTicketRiskContext,
    RiskOfRuinEvidence,
)


class EconomicGoalEndogenousStakeTests(unittest.TestCase):
    @staticmethod
    def _event(
        *,
        event_id: str = "event-1",
        market_id: str = "market-1",
        selection_id: str = "selection-1",
        sequence: int = 1,
    ) -> MarketEvent:
        return MarketEvent(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=Decimal("2"),
            observed_ts="2026-09-17T15:00:00+00:00",
            source_id="provider-1",
            sequence=sequence,
            source_ts="2026-09-17T14:59:59+00:00",
            ingest_ts="2026-09-17T15:00:00+00:00",
        )

    @staticmethod
    def _goal(**overrides: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "goal-endogenous-stake",
            "revision": 1,
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "max_stake_fraction": Decimal("0.02"),
            "max_session_loss_fraction": Decimal("1"),
            "max_day_loss_fraction": Decimal("1"),
            "max_drawdown_fraction": Decimal("1"),
            "max_capital_at_risk_fraction": Decimal("0.20"),
            "max_turnover_fraction": Decimal("1000"),
            "max_risk_of_ruin": Decimal("1"),
            "max_concurrent_positions": 10,
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @staticmethod
    def _policy(goal: EconomicGoalContract) -> PaperRiskPolicy:
        return PaperRiskPolicy(
            max_ticket_fraction=Decimal("1"),
            max_committed_fraction=Decimal("1"),
            minimum_cash_reserve_fraction=Decimal("0"),
            economic_goal=goal,
        )

    @staticmethod
    def _agent(event: MarketEvent, policy: PaperRiskPolicy) -> PaperValueAgent:
        return PaperValueAgent(
            {
                event.quote_key: Forecast(
                    quote_key=event.quote_key,
                    probability=Decimal("0.60"),
                    model_id="test-model",
                    as_of_ts="2026-09-17T14:59:58+00:00",
                )
            },
            stake=Decimal("999"),
            risk_policy=policy,
        )

    @staticmethod
    def _open_tickets(book: PaperBook):
        return tuple(
            ticket
            for ticket in book.tickets.values()
            if ticket.status is TicketStatus.OPEN
        )

    @staticmethod
    def _risk_context(
        event: MarketEvent,
        goal: EconomicGoalContract,
        *,
        risk_of_ruin_upper_bound: Decimal | None = None,
        include_quote: bool = True,
    ) -> ProposedTicketRiskContext:
        leg = TicketLeg(
            event.event_id,
            event.market_id,
            event.selection_id,
            event.decimal_odds,
        )
        return ProposedTicketRiskContext(
            legs=(leg,),
            quotes=((event,) if include_quote else ()),
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            proposal_ts=event.observed_ts,
            risk_of_ruin_upper_bound=risk_of_ruin_upper_bound,
        )

    @classmethod
    def _bound_ruin_context(
        cls,
        event: MarketEvent,
        goal: EconomicGoalContract,
        policy: PaperRiskPolicy,
        book: PaperBook,
        *,
        evaluated_stake: Decimal,
        upper_bound: Decimal,
    ) -> ProposedTicketRiskContext:
        base = cls._risk_context(event, goal)
        portfolio_sha256 = policy.risk_of_ruin_portfolio_sha256(book)
        candidate_sha256 = policy.risk_of_ruin_candidate_sha256(base)
        assert portfolio_sha256 is not None
        assert candidate_sha256 is not None
        return replace(
            base,
            risk_of_ruin_evidence=RiskOfRuinEvidence(
                evidence_id="ror-evidence-1",
                research_protocol_sha256="a" * 64,
                reproducibility_bundle_sha256="b" * 64,
                producer_identity="test-risk-model-source",
                causal_cutoff="2026-09-17T14:59:58+00:00",
                evaluated_at="2026-09-17T14:59:59+00:00",
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
                base_portfolio_sha256=portfolio_sha256,
                candidate_sha256=candidate_sha256,
                evaluated_stake=evaluated_stake,
                upper_bound=upper_bound,
            ),
        )

    def test_active_economic_goal_removes_fixed_caller_stake_authority(self) -> None:
        event = self._event()
        goal = self._goal()
        book = PaperBook("1000")
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-endogenous-stake",
                decision_ledger=ledger,
            )

            self._agent(event, self._policy(goal)).on_market_event(event, context)

            open_tickets = self._open_tickets(book)
            self.assertEqual(len(open_tickets), 1)
            ticket = open_tickets[0]
            self.assertEqual(ticket.stake, Decimal("20.00"))
            self.assertNotEqual(ticket.stake, Decimal("999"))
            records = ledger.verified_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].payload["stake"], "20.00")

    def test_existing_open_exposure_tightens_next_goal_derived_stake_and_restart_is_idempotent(self) -> None:
        event = self._event(event_id="event-2", market_id="market-2", sequence=2)
        goal = self._goal(
            max_stake_fraction=Decimal("0.20"),
            max_capital_at_risk_fraction=Decimal("0.25"),
        )
        book = PaperBook("100")
        book.open_ticket(
            [TicketLeg("existing-event", "existing-market", "existing-selection", Decimal("2"))],
            Decimal("20"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "decisions.jsonl"
            book_path = root / "paper.json"
            ledger = JsonlDecisionLedger(ledger_path)
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-exposure-aware-stake",
                decision_ledger=ledger,
            )

            self._agent(event, self._policy(goal)).on_market_event(event, context)

            open_tickets = self._open_tickets(book)
            self.assertEqual(len(open_tickets), 2)
            new_ticket = next(
                ticket
                for ticket in open_tickets
                if ticket.legs[0].event_id == event.event_id
            )
            self.assertEqual(new_ticket.stake, Decimal("5.00"))
            self.assertEqual(book.committed_stake, Decimal("25.00"))
            book.save(book_path)

            restarted_book = PaperBook.load(book_path)
            restarted_context = AgentContext(
                restarted_book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-exposure-aware-stake",
                decision_ledger=JsonlDecisionLedger(ledger_path),
            )
            self._agent(event, self._policy(goal)).on_market_event(event, restarted_context)

            self.assertEqual(len(self._open_tickets(restarted_book)), 2)
            self.assertEqual(restarted_book.committed_stake, Decimal("25.00"))
            self.assertEqual(len(restarted_context.decision_ledger.verified_records()), 1)

    def test_exhausted_goal_exposure_returns_zero_by_opening_nothing(self) -> None:
        event = self._event(event_id="event-3", market_id="market-3", sequence=3)
        goal = self._goal(
            max_stake_fraction=Decimal("0.20"),
            max_capital_at_risk_fraction=Decimal("0.20"),
        )
        book = PaperBook("100")
        existing = book.open_ticket(
            [TicketLeg("existing-event", "existing-market", "existing-selection", Decimal("2"))],
            Decimal("20"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
            context = AgentContext(
                book,
                latest_quotes={event.quote_key: event},
                replay_run_id="run-zero-stake",
                decision_ledger=ledger,
            )

            self._agent(event, self._policy(goal)).on_market_event(event, context)

            self.assertEqual(self._open_tickets(book), (existing,))
            self.assertEqual(book.committed_stake, Decimal("20"))
            self.assertFalse((Path(tmp) / "decisions.jsonl").exists())

    def test_multi_candidate_vector_prioritizes_signal_and_reserves_aggregate_cap(self) -> None:
        weak = self._event(event_id="event-weak", market_id="market-weak", sequence=11)
        strong = self._event(
            event_id="event-strong",
            market_id="market-strong",
            selection_id="selection-strong",
            sequence=12,
        )
        goal = self._goal(
            max_stake_fraction=Decimal("0.20"),
            max_capital_at_risk_fraction=Decimal("0.30"),
            max_concurrent_positions=3,
        )
        policy = self._policy(goal)
        book = PaperBook("100")

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("0.50"), Decimal("1")),
            contexts=(
                self._risk_context(weak, goal),
                self._risk_context(strong, goal),
            ),
        )

        self.assertEqual(decision.action, "STAKE_VECTOR")
        self.assertEqual(decision.stakes, (Decimal("10.00"), Decimal("20.00")))
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})
        self.assertEqual(book.committed_stake, Decimal("0"))

    def test_multi_candidate_vector_waits_on_duplicate_candidate_identity(self) -> None:
        event = self._event(
            event_id="event-duplicate",
            market_id="market-duplicate",
            selection_id="selection-duplicate",
            sequence=19,
        )
        goal = self._goal(
            max_stake_fraction=Decimal("0.20"),
            max_capital_at_risk_fraction=Decimal("0.30"),
            max_concurrent_positions=3,
        )
        policy = self._policy(goal)
        book = PaperBook("100")
        context = self._risk_context(event, goal)

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"), Decimal("1")),
            contexts=(context, context),
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.stakes, (Decimal("0"), Decimal("0")))
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})
        self.assertEqual(book.committed_stake, Decimal("0"))

    def test_multi_candidate_vector_waits_without_vector_bound_ruin_evidence(self) -> None:
        first = self._event(
            event_id="event-ruin-vector-1",
            market_id="market-ruin-vector-1",
            selection_id="selection-ruin-vector-1",
            sequence=20,
        )
        second = self._event(
            event_id="event-ruin-vector-2",
            market_id="market-ruin-vector-2",
            selection_id="selection-ruin-vector-2",
            sequence=21,
        )
        goal = self._goal(
            max_risk_of_ruin=Decimal("0.10"),
            max_concurrent_positions=3,
        )
        policy = self._policy(goal)
        book = PaperBook("100")

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"), Decimal("1")),
            contexts=(
                self._risk_context(
                    first,
                    goal,
                    risk_of_ruin_upper_bound=Decimal("0.05"),
                ),
                self._risk_context(
                    second,
                    goal,
                    risk_of_ruin_upper_bound=Decimal("0.05"),
                ),
            ),
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.stakes, (Decimal("0"), Decimal("0")))
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})
        self.assertEqual(book.committed_stake, Decimal("0"))

    def test_multi_candidate_vector_waits_on_incomplete_candidate_evidence(self) -> None:
        event = self._event(event_id="event-wait", market_id="market-wait", sequence=13)
        goal = self._goal()
        policy = self._policy(goal)
        book = PaperBook("100")

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"),),
            contexts=(
                self._risk_context(event, goal, include_quote=False),
            ),
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.stakes, (Decimal("0"),))
        self.assertEqual(book.tickets, {})

    def test_multi_candidate_vector_zero_for_no_positive_signal(self) -> None:
        first = self._event(event_id="event-zero-1", market_id="market-zero-1", sequence=14)
        second = self._event(
            event_id="event-zero-2",
            market_id="market-zero-2",
            selection_id="selection-zero-2",
            sequence=15,
        )
        goal = self._goal()
        policy = self._policy(goal)
        book = PaperBook("100")

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("0"), Decimal("-0.1")),
            contexts=(
                self._risk_context(first, goal),
                self._risk_context(second, goal),
            ),
        )

        self.assertEqual(decision.action, "ZERO")
        self.assertEqual(decision.stakes, (Decimal("0"), Decimal("0")))
        self.assertEqual(book.tickets, {})

    def test_multi_candidate_vector_accepts_explicit_ruin_witness(self) -> None:
        event = self._event(event_id="event-ruin", market_id="market-ruin", sequence=16)
        goal = self._goal(max_risk_of_ruin=Decimal("0.10"))
        policy = self._policy(goal)
        book = PaperBook("100")

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"),),
            contexts=(
                self._bound_ruin_context(
                    event,
                    goal,
                    policy,
                    book,
                    evaluated_stake=Decimal("2.00"),
                    upper_bound=Decimal("0.05"),
                ),
            ),
        )

        self.assertEqual(decision.action, "STAKE_VECTOR")
        self.assertEqual(decision.stakes, (Decimal("2.00"),))
        self.assertEqual(book.tickets, {})

    def test_single_candidate_bare_ruin_scalar_cannot_authorize(self) -> None:
        event = self._event(
            event_id="event-ruin-bare",
            market_id="market-ruin-bare",
            sequence=22,
        )
        goal = self._goal(max_risk_of_ruin=Decimal("0.10"))
        policy = self._policy(goal)
        book = PaperBook("100")

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"),),
            contexts=(
                self._risk_context(
                    event,
                    goal,
                    risk_of_ruin_upper_bound=Decimal("0.05"),
                ),
            ),
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.stakes, (Decimal("0"),))
        self.assertEqual(book.tickets, {})

    def test_multi_candidate_vector_waits_on_stale_quote_evidence(self) -> None:
        event = self._event(event_id="event-stale", market_id="market-stale", sequence=17)
        goal = self._goal(max_quote_age_seconds=Decimal("0.5"))
        policy = self._policy(goal)
        book = PaperBook("100")

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"),),
            contexts=(self._risk_context(event, goal),),
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.stakes, (Decimal("0"),))
        self.assertEqual(book.tickets, {})

    def test_multi_candidate_vector_zero_when_ruin_evidence_exceeds_limit(self) -> None:
        event = self._event(
            event_id="event-ruin-high",
            market_id="market-ruin-high",
            sequence=18,
        )
        goal = self._goal(max_risk_of_ruin=Decimal("0.10"))
        policy = self._policy(goal)
        book = PaperBook("100")

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"),),
            contexts=(
                self._bound_ruin_context(
                    event,
                    goal,
                    policy,
                    book,
                    evaluated_stake=Decimal("2.00"),
                    upper_bound=Decimal("0.20"),
                ),
            ),
        )

        self.assertEqual(decision.action, "ZERO")
        self.assertEqual(decision.stakes, (Decimal("0"),))
        self.assertEqual(book.tickets, {})


if __name__ == "__main__":
    unittest.main()
