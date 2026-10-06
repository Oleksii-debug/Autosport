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
    RiskOfRuinVectorEvidence,
    StakeVectorDecision,
)


class _HostileSignalInput:
    calls = 0

    def __str__(self) -> str:
        type(self).calls += 1
        return "1"


class EconomicGoalEndogenousStakeTests(unittest.TestCase):

    def test_stake_vector_decision_rejects_action_subclass_before_comparison(self) -> None:
        class HostileAction(str):
            comparisons = 0

            def __hash__(self) -> int:
                type(self).comparisons += 1
                return super().__hash__()

            def __eq__(self, other: object) -> bool:
                type(self).comparisons += 1
                return super().__eq__(other)

        hostile = HostileAction("WAIT")
        with self.assertRaisesRegex(ValueError, "stake vector action"):
            StakeVectorDecision(hostile, (Decimal("0"),), "reason")

        self.assertEqual(HostileAction.comparisons, 0)

    def test_stake_vector_decision_rejects_decimal_subclass_before_comparison(self) -> None:
        class HostileStake(Decimal):
            comparisons = 0

            def __lt__(self, other: object) -> bool:
                type(self).comparisons += 1
                return super().__lt__(other)

            def __gt__(self, other: object) -> bool:
                type(self).comparisons += 1
                return super().__gt__(other)

        hostile = HostileStake("0")
        with self.assertRaisesRegex(ValueError, "exact non-negative finite Decimal"):
            StakeVectorDecision("WAIT", (hostile,), "reason")

        self.assertEqual(HostileStake.comparisons, 0)

    @staticmethod
    def _event(
        *,
        event_id: str = "event-1",
        market_id: str = "market-1",
        selection_id: str = "selection-1",
        sequence: int = 1,
        source_id: str = "provider-1",
    ) -> MarketEvent:
        return MarketEvent(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=Decimal("2"),
            observed_ts="2026-09-17T15:00:00+00:00",
            source_id=source_id,
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

    @staticmethod
    def _lay_context(
        goal: EconomicGoalContract,
        *,
        odds: str,
        event_id: str = "event-lay",
        market_id: str = "market-lay",
        selection_id: str = "selection-lay",
    ) -> ProposedTicketRiskContext:
        decimal_odds = Decimal(odds)
        event = MarketEvent(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=decimal_odds,
            observed_ts="2026-09-17T15:00:00+00:00",
            source_id="provider-lay",
            sequence=1,
            source_ts="2026-09-17T14:59:59+00:00",
            ingest_ts="2026-09-17T15:00:00+00:00",
            exchange_side="lay",
        )
        leg = TicketLeg(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            locked_odds=decimal_odds,
            exchange_side="lay",
        )
        return ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(event,),
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            proposal_ts=event.observed_ts,
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

    @classmethod
    def _bound_ruin_vector_evidence(
        cls,
        policy: PaperRiskPolicy,
        book: PaperBook,
        contexts: tuple[ProposedTicketRiskContext, ...],
        stakes: tuple[Decimal, ...],
        *,
        upper_bound: Decimal,
        **overrides: object,
    ) -> RiskOfRuinVectorEvidence:
        portfolio_sha256 = policy.risk_of_ruin_portfolio_sha256(book)
        candidate_vector_sha256 = policy.risk_of_ruin_candidate_vector_sha256(
            contexts
        )
        assert portfolio_sha256 is not None
        assert candidate_vector_sha256 is not None
        values: dict[str, object] = {
            "evidence_id": "ror-vector-evidence-1",
            "research_protocol_sha256": "c" * 64,
            "reproducibility_bundle_sha256": "d" * 64,
            "producer_identity": "test-vector-risk-model-source",
            "causal_cutoff": "2026-09-17T14:59:58+00:00",
            "evaluated_at": "2026-09-17T14:59:59+00:00",
            "bankroll_id": contexts[0].bankroll_id,
            "currency": contexts[0].currency,
            "base_portfolio_sha256": portfolio_sha256,
            "candidate_vector_sha256": candidate_vector_sha256,
            "evaluated_stakes": stakes,
            "upper_bound": upper_bound,
        }
        values.update(overrides)
        return RiskOfRuinVectorEvidence(**values)  # type: ignore[arg-type]

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
            self.assertIsNotNone(context.paper_execution)
            book_path = context.paper_execution.paper_book_path
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





    def test_lay_endogenous_stake_is_sized_from_liability_room(self) -> None:
        goal = self._goal(
            max_stake_fraction=Decimal("1"),
            max_capital_at_risk_fraction=Decimal("0.40"),
        )
        policy = self._policy(goal)
        book = PaperBook("100")
        context = self._lay_context(goal, odds="5")

        amount = policy.derive_goal_stake(
            book,
            Decimal("1"),
            context=context,
        )

        self.assertEqual(amount, Decimal("10"))
        assert amount is not None
        self.assertTrue(policy.evaluate(book, amount, context=context).allowed)

    def test_lay_endogenous_stake_fails_closed_when_inverse_is_not_representable(self) -> None:
        goal = self._goal(
            max_stake_fraction=Decimal("1"),
            max_capital_at_risk_fraction=Decimal("0.20"),
        )
        policy = self._policy(goal)
        book = PaperBook("100")
        context = self._lay_context(goal, odds="1.3")

        amount = policy.derive_goal_stake(
            book,
            Decimal("1"),
            context=context,
        )

        self.assertIsNone(amount)

    def test_lay_stake_vector_reserves_liability_in_shadow_book(self) -> None:
        goal = self._goal(
            max_stake_fraction=Decimal("1"),
            max_capital_at_risk_fraction=Decimal("0.40"),
        )
        policy = self._policy(goal)
        book = PaperBook("100")
        context = self._lay_context(goal, odds="5")

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"),),
            contexts=(context,),
        )

        self.assertEqual(decision.action, "STAKE_VECTOR")
        self.assertEqual(decision.stakes, (Decimal("10"),))
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_single_goal_stake_rejects_context_subclass(self) -> None:
        class DerivedContext(ProposedTicketRiskContext):
            pass

        event = self._event(
            event_id="event-derived-single",
            market_id="market-derived-single",
            selection_id="selection-derived-single",
            sequence=92,
        )
        goal = self._goal()
        base = self._risk_context(event, goal)
        derived = DerivedContext(
            legs=base.legs,
            quotes=base.quotes,
            provider_accounts=base.provider_accounts,
            bankroll_id=base.bankroll_id,
            currency=base.currency,
            measurement_window_start=base.measurement_window_start,
            measurement_window_end=base.measurement_window_end,
            proposal_ts=base.proposal_ts,
        )

        amount = self._policy(goal).derive_goal_stake(
            PaperBook("100"),
            Decimal("1"),
            context=derived,
        )

        self.assertIsNone(amount)


    def test_multi_candidate_vector_rejects_hostile_signal_before_str(self) -> None:
        _HostileSignalInput.calls = 0
        goal = self._goal()
        policy = self._policy(goal)
        event = self._event(
            event_id="event-hostile-vector",
            market_id="market-hostile-vector",
            selection_id="selection-hostile-vector",
            sequence=95,
        )
        context = self._risk_context(event, goal)

        decision = policy.derive_goal_stake_vector(
            PaperBook("100"),
            (_HostileSignalInput(),),
            contexts=(context,),
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.reason, "candidate signal evidence is invalid")
        self.assertEqual(_HostileSignalInput.calls, 0)

    def test_multi_candidate_vector_rejects_context_subclass(self) -> None:
        class DerivedContext(ProposedTicketRiskContext):
            pass

        event = self._event(
            event_id="event-derived-context",
            market_id="market-derived-context",
            selection_id="selection-derived-context",
            sequence=91,
        )
        goal = self._goal()
        base = self._risk_context(event, goal)
        derived = DerivedContext(
            legs=base.legs,
            quotes=base.quotes,
            provider_accounts=base.provider_accounts,
            bankroll_id=base.bankroll_id,
            currency=base.currency,
            measurement_window_start=base.measurement_window_start,
            measurement_window_end=base.measurement_window_end,
            proposal_ts=base.proposal_ts,
        )
        decision = self._policy(goal).derive_goal_stake_vector(
            PaperBook("100"),
            (Decimal("1"),),
            contexts=(derived,),
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.stakes, (Decimal("0"),))
        self.assertEqual(decision.reason, "candidate risk context is invalid")

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

    def test_multi_candidate_vector_preserves_provider_accounts_in_shadow(self) -> None:
        first = self._event(
            event_id="event-provider-1",
            market_id="market-provider-1",
            selection_id="selection-provider-1",
            sequence=23,
            source_id="provider-1",
        )
        second = self._event(
            event_id="event-provider-2",
            market_id="market-provider-2",
            selection_id="selection-provider-2",
            sequence=24,
            source_id="provider-1",
        )
        goal = self._goal(
            max_stake_fraction=Decimal("0.20"),
            max_capital_at_risk_fraction=Decimal("0.60"),
            max_provider_concentration_fraction=Decimal("0.60"),
            max_concurrent_positions=4,
        )
        policy = self._policy(goal)
        book = PaperBook("100")
        book.open_ticket(
            [
                TicketLeg(
                    "existing-provider-event",
                    "existing-provider-market",
                    "existing-provider-selection",
                    Decimal("2"),
                )
            ],
            Decimal("20"),
            placed_at="2026-09-17T14:58:00+00:00",
            provider_source_ids=("provider-0",),
            provider_accounts=(("provider-0", "account-0"),),
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
        )
        contexts = (
            replace(
                self._risk_context(first, goal),
                provider_accounts=(("provider-1", "account-1"),),
            ),
            replace(
                self._risk_context(second, goal),
                provider_accounts=(("provider-1", "account-1"),),
            ),
        )

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"), Decimal("1")),
            contexts=contexts,
        )

        self.assertEqual(decision.action, "STAKE_VECTOR")
        self.assertEqual(decision.stakes, (Decimal("20.00"), Decimal("0")))
        self.assertEqual(book.balance, Decimal("80"))
        self.assertEqual(book.committed_stake, Decimal("20"))
        self.assertEqual(len(book.tickets), 1)

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

    def test_multi_candidate_vector_rejects_caller_constructed_exact_ruin_evidence(self) -> None:
        first = self._event(
            event_id="event-vector-ror-1",
            market_id="market-vector-ror-1",
            selection_id="selection-vector-ror-1",
            sequence=30,
        )
        second = self._event(
            event_id="event-vector-ror-2",
            market_id="market-vector-ror-2",
            selection_id="selection-vector-ror-2",
            sequence=31,
        )
        goal = self._goal(
            max_risk_of_ruin=Decimal("0.10"),
            max_concurrent_positions=3,
        )
        policy = self._policy(goal)
        book = PaperBook("100")
        contexts = (
            self._risk_context(first, goal),
            self._risk_context(second, goal),
        )
        expected_stakes = (Decimal("2.00"), Decimal("2.00"))
        witness = self._bound_ruin_vector_evidence(
            policy,
            book,
            contexts,
            expected_stakes,
            upper_bound=Decimal("0.05"),
        )

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"), Decimal("1")),
            contexts=contexts,
            risk_of_ruin_vector_evidence=witness,
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.stakes, (Decimal("0"), Decimal("0")))
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})
        self.assertEqual(book.committed_stake, Decimal("0"))




    def test_vector_ruin_evidence_revalidated_after_mutation_before_comparison(self) -> None:
        class HostileBound(Decimal):
            comparisons = 0

            def __gt__(self, other: object) -> bool:
                type(self).comparisons += 1
                return super().__gt__(other)

        first = self._event(
            event_id="event-mutated-vector-1",
            market_id="market-mutated-vector-1",
            selection_id="selection-mutated-vector-1",
            sequence=96,
        )
        second = self._event(
            event_id="event-mutated-vector-2",
            market_id="market-mutated-vector-2",
            selection_id="selection-mutated-vector-2",
            sequence=97,
        )
        goal = self._goal(
            max_risk_of_ruin=Decimal("0.10"),
            max_concurrent_positions=3,
        )
        policy = self._policy(goal)
        book = PaperBook("100")
        contexts = (
            self._risk_context(first, goal),
            self._risk_context(second, goal),
        )
        evidence = self._bound_ruin_vector_evidence(
            policy,
            book,
            contexts,
            (Decimal("2.00"), Decimal("2.00")),
            upper_bound=Decimal("0.05"),
        )
        hostile = HostileBound("0.05")
        HostileBound.comparisons = 0
        object.__setattr__(evidence, "upper_bound", hostile)

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"), Decimal("1")),
            contexts=contexts,
            risk_of_ruin_vector_evidence=evidence,
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(
            decision.reason,
            "multi-candidate portfolio risk-of-ruin vector evidence is invalid",
        )
        self.assertEqual(HostileBound.comparisons, 0)

    def test_multi_candidate_vector_rejects_vector_evidence_subclass(self) -> None:
        class DerivedVectorEvidence(RiskOfRuinVectorEvidence):
            pass

        first = self._event(
            event_id="event-vector-subclass-1",
            market_id="market-vector-subclass-1",
            selection_id="selection-vector-subclass-1",
            sequence=93,
        )
        second = self._event(
            event_id="event-vector-subclass-2",
            market_id="market-vector-subclass-2",
            selection_id="selection-vector-subclass-2",
            sequence=94,
        )
        goal = self._goal(
            max_risk_of_ruin=Decimal("0.10"),
            max_concurrent_positions=3,
        )
        policy = self._policy(goal)
        book = PaperBook("100")
        contexts = (
            self._risk_context(first, goal),
            self._risk_context(second, goal),
        )
        canonical = self._bound_ruin_vector_evidence(
            policy,
            book,
            contexts,
            (Decimal("2.00"), Decimal("2.00")),
            upper_bound=Decimal("0.05"),
        )
        derived = DerivedVectorEvidence(
            evidence_id=canonical.evidence_id,
            research_protocol_sha256=canonical.research_protocol_sha256,
            reproducibility_bundle_sha256=canonical.reproducibility_bundle_sha256,
            producer_identity=canonical.producer_identity,
            causal_cutoff=canonical.causal_cutoff,
            evaluated_at=canonical.evaluated_at,
            bankroll_id=canonical.bankroll_id,
            currency=canonical.currency,
            base_portfolio_sha256=canonical.base_portfolio_sha256,
            candidate_vector_sha256=canonical.candidate_vector_sha256,
            evaluated_stakes=canonical.evaluated_stakes,
            upper_bound=canonical.upper_bound,
        )

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"), Decimal("1")),
            contexts=contexts,
            risk_of_ruin_vector_evidence=derived,
        )

        self.assertEqual(decision.action, "WAIT")
        self.assertEqual(decision.stakes, (Decimal("0"), Decimal("0")))
        self.assertEqual(
            decision.reason,
            "multi-candidate portfolio risk-of-ruin vector evidence is invalid",
        )

    def test_multi_candidate_vector_rejects_mismatched_vector_ruin_evidence(self) -> None:
        first = self._event(
            event_id="event-vector-ror-mismatch-1",
            market_id="market-vector-ror-mismatch-1",
            selection_id="selection-vector-ror-mismatch-1",
            sequence=32,
        )
        second = self._event(
            event_id="event-vector-ror-mismatch-2",
            market_id="market-vector-ror-mismatch-2",
            selection_id="selection-vector-ror-mismatch-2",
            sequence=33,
        )
        goal = self._goal(
            max_risk_of_ruin=Decimal("0.10"),
            max_concurrent_positions=3,
        )
        policy = self._policy(goal)
        book = PaperBook("100")
        contexts = (
            self._risk_context(first, goal),
            self._risk_context(second, goal),
        )
        wrong_stakes = self._bound_ruin_vector_evidence(
            policy,
            book,
            contexts,
            (Decimal("2.00"), Decimal("1.99")),
            upper_bound=Decimal("0.05"),
        )
        wrong_candidates = self._bound_ruin_vector_evidence(
            policy,
            book,
            tuple(reversed(contexts)),
            (Decimal("2.00"), Decimal("2.00")),
            upper_bound=Decimal("0.05"),
        )

        for witness in (wrong_stakes, wrong_candidates):
            with self.subTest(evidence_id=witness.evidence_id):
                decision = policy.derive_goal_stake_vector(
                    book,
                    (Decimal("1"), Decimal("1")),
                    contexts=contexts,
                    risk_of_ruin_vector_evidence=witness,
                )
                self.assertEqual(decision.action, "WAIT")
                self.assertEqual(
                    decision.stakes,
                    (Decimal("0"), Decimal("0")),
                )
                self.assertEqual(book.balance, Decimal("100"))
                self.assertEqual(book.tickets, {})

    def test_multi_candidate_vector_zero_when_vector_ruin_bound_exceeds_goal(self) -> None:
        first = self._event(
            event_id="event-vector-ror-high-1",
            market_id="market-vector-ror-high-1",
            selection_id="selection-vector-ror-high-1",
            sequence=34,
        )
        second = self._event(
            event_id="event-vector-ror-high-2",
            market_id="market-vector-ror-high-2",
            selection_id="selection-vector-ror-high-2",
            sequence=35,
        )
        goal = self._goal(
            max_risk_of_ruin=Decimal("0.10"),
            max_concurrent_positions=3,
        )
        policy = self._policy(goal)
        book = PaperBook("100")
        contexts = (
            self._risk_context(first, goal),
            self._risk_context(second, goal),
        )
        witness = self._bound_ruin_vector_evidence(
            policy,
            book,
            contexts,
            (Decimal("2.00"), Decimal("2.00")),
            upper_bound=Decimal("0.11"),
        )

        decision = policy.derive_goal_stake_vector(
            book,
            (Decimal("1"), Decimal("1")),
            contexts=contexts,
            risk_of_ruin_vector_evidence=witness,
        )

        self.assertEqual(decision.action, "ZERO")
        self.assertEqual(decision.stakes, (Decimal("0"), Decimal("0")))
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

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

    def test_multi_candidate_vector_rejects_caller_constructed_single_ruin_witness(self) -> None:
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

        self.assertEqual(decision.action, "ZERO")
        self.assertEqual(decision.stakes, (Decimal("0"),))
        self.assertEqual(book.balance, Decimal("100"))
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
