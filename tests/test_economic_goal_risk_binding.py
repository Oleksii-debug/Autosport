import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import (
    PaperRiskPolicy,
    ProposedTicketRiskContext,
    RiskOfRuinEvidence,
)


class EconomicGoalRiskBindingTests(unittest.TestCase):
    @staticmethod
    def _leg() -> TicketLeg:
        return TicketLeg("event-1", "market-1", "selection-1", Decimal("2"))

    @classmethod
    def _context(cls) -> ProposedTicketRiskContext:
        leg = cls._leg()
        quote = MarketEvent(
            event_id=leg.event_id,
            market_id=leg.market_id,
            selection_id=leg.selection_id,
            decimal_odds=leg.locked_odds,
            observed_ts="2026-09-16T15:00:00+00:00",
            source_id="provider-1",
            sequence=1,
            source_ts="2026-09-16T14:59:59+00:00",
            ingest_ts="2026-09-16T15:00:01+00:00",
        )
        return ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(quote,),
            bankroll_id="paper-bankroll",
            currency="USD",
            proposal_ts="2026-09-16T15:00:02+00:00",
        )

    @classmethod
    def _evaluate(
        cls,
        policy: PaperRiskPolicy,
        book: PaperBook,
        stake: Decimal,
    ):
        return policy.evaluate(book, stake, context=cls._context())

    @classmethod
    def _bound_ruin_context(
        cls,
        policy: PaperRiskPolicy,
        book: PaperBook,
        stake: Decimal,
        upper_bound: Decimal,
        **evidence_overrides: object,
    ) -> ProposedTicketRiskContext:
        base = cls._context()
        portfolio_sha256 = policy.risk_of_ruin_portfolio_sha256(book)
        candidate_sha256 = policy.risk_of_ruin_candidate_sha256(base)
        assert portfolio_sha256 is not None
        assert candidate_sha256 is not None
        evidence = RiskOfRuinEvidence(
            evidence_id="ror-evidence-binding",
            research_protocol_sha256="a" * 64,
            reproducibility_bundle_sha256="b" * 64,
            producer_identity="test-risk-model-source",
            causal_cutoff="2026-09-16T14:59:58+00:00",
            evaluated_at="2026-09-16T15:00:01+00:00",
            bankroll_id="paper-bankroll",
            currency="USD",
            base_portfolio_sha256=portfolio_sha256,
            candidate_sha256=candidate_sha256,
            evaluated_stake=stake,
            upper_bound=upper_bound,
        )
        if evidence_overrides:
            evidence = replace(evidence, **evidence_overrides)
        return replace(base, risk_of_ruin_evidence=evidence)

    @staticmethod
    def _goal(**overrides: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "goal-1",
            "revision": 1,
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "max_stake_fraction": Decimal("1"),
            "max_session_loss_fraction": Decimal("1"),
            "max_day_loss_fraction": Decimal("1"),
            "max_drawdown_fraction": Decimal("1"),
            "max_capital_at_risk_fraction": Decimal("1"),
            "max_turnover_fraction": Decimal("1000"),
            "max_risk_of_ruin": Decimal("1"),
            "max_concurrent_positions": 10,
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @classmethod
    def _policy(cls, goal: EconomicGoalContract, **overrides: object) -> PaperRiskPolicy:
        values: dict[str, object] = {
            "max_ticket_fraction": Decimal("1"),
            "max_committed_fraction": Decimal("1"),
            "minimum_cash_reserve_fraction": Decimal("0"),
            "economic_goal": goal,
        }
        values.update(overrides)
        return PaperRiskPolicy(**values)  # type: ignore[arg-type]

    def test_owner_stake_fraction_tightens_executable_policy_at_exact_boundary(self) -> None:
        policy = self._policy(self._goal(max_stake_fraction=Decimal("0.10")))
        book = PaperBook("100")

        self.assertTrue(self._evaluate(policy, book, Decimal("10")).allowed)
        decision = self._evaluate(policy, book, Decimal("10.01"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "ticket exceeds configured bankroll fraction")

    def test_stricter_executable_ticket_fraction_cannot_be_widened_by_owner_contract(self) -> None:
        policy = self._policy(
            self._goal(max_stake_fraction=Decimal("0.50")),
            max_ticket_fraction=Decimal("0.05"),
        )
        book = PaperBook("100")

        self.assertTrue(self._evaluate(policy, book, Decimal("5")).allowed)
        self.assertFalse(self._evaluate(policy, book, Decimal("5.01")).allowed)

    def test_owner_absolute_stake_amount_is_exact_and_inclusive(self) -> None:
        policy = self._policy(self._goal(max_stake_amount=Decimal("7.50")))
        book = PaperBook("100")

        self.assertTrue(self._evaluate(policy, book, Decimal("7.50")).allowed)
        decision = self._evaluate(
            policy, book, Decimal("7.500000000000000000000000001")
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "ticket exceeds economic goal absolute stake limit")

    def test_zero_owner_absolute_stake_amount_denies_every_positive_new_stake(self) -> None:
        policy = self._policy(self._goal(max_stake_amount=Decimal("0")))
        decision = self._evaluate(
            policy, PaperBook("100"), Decimal("0.000000000000000000000000001")
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "ticket exceeds economic goal absolute stake limit")

    def test_owner_capital_at_risk_counts_existing_open_exposure_plus_proposed_stake(self) -> None:
        book = PaperBook("100")
        book.open_ticket([self._leg()], Decimal("15"))
        policy = self._policy(
            self._goal(max_capital_at_risk_fraction=Decimal("0.20"))
        )

        self.assertTrue(self._evaluate(policy, book, Decimal("5")).allowed)
        decision = self._evaluate(policy, book, Decimal("5.01"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "aggregate committed stake limit exceeded")

    def test_settled_historical_stake_does_not_count_as_current_capital_at_risk(self) -> None:
        book = PaperBook("100")
        settled = book.open_ticket([self._leg()], Decimal("15"))
        book.settle(settled.ticket_id, {settled.legs[0].quote_key})
        policy = self._policy(
            self._goal(max_capital_at_risk_fraction=Decimal("0.10"))
        )

        self.assertEqual(book.committed_stake, Decimal("0"))
        self.assertTrue(self._evaluate(policy, book, Decimal("10")).allowed)
        decision = self._evaluate(policy, book, Decimal("10.01"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "aggregate committed stake limit exceeded")


    def test_existing_open_exposure_consumes_session_day_and_drawdown_loss_rooms(self) -> None:
        limits = (
            (
                "max_session_loss_fraction",
                "economic goal conservative session loss limit exceeded",
            ),
            (
                "max_day_loss_fraction",
                "economic goal conservative day loss limit exceeded",
            ),
            ("max_drawdown_fraction", "economic goal drawdown limit exceeded"),
        )
        for field_name, reason in limits:
            with self.subTest(field_name=field_name):
                book = PaperBook("100")
                book.open_ticket([self._leg()], Decimal("4"))
                policy = self._policy(
                    self._goal(**{field_name: Decimal("0.05")})
                )

                self.assertTrue(self._evaluate(policy, book, Decimal("1")).allowed)
                blocked = self._evaluate(policy, book, Decimal("1.01"))

                self.assertFalse(blocked.allowed)
                self.assertEqual(blocked.reason, reason)

    def test_goal_stake_derivation_fails_closed_without_required_ruin_evidence(self) -> None:
        policy = self._policy(
            self._goal(max_risk_of_ruin=Decimal("0.01"))
        )

        self.assertIsNone(
            policy.derive_goal_stake(PaperBook("100"), Decimal("1"))
        )

    def test_session_loss_limit_uses_conservative_realized_loss_plus_proposed_worst_case(self) -> None:
        book = PaperBook("100")
        lost = book.open_ticket([self._leg()], Decimal("4"))
        book.settle(lost.ticket_id, set())
        policy = self._policy(
            self._goal(max_session_loss_fraction=Decimal("0.05"))
        )

        self.assertTrue(self._evaluate(policy, book, Decimal("1")).allowed)
        blocked = self._evaluate(policy, book, Decimal("1.01"))

        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.reason,
            "economic goal conservative session loss limit exceeded",
        )

    def test_day_loss_limit_uses_same_safe_all_history_upper_bound(self) -> None:
        book = PaperBook("100")
        lost = book.open_ticket([self._leg()], Decimal("4"))
        book.settle(lost.ticket_id, set())
        policy = self._policy(
            self._goal(max_day_loss_fraction=Decimal("0.05"))
        )

        self.assertTrue(self._evaluate(policy, book, Decimal("1")).allowed)
        blocked = self._evaluate(policy, book, Decimal("1.01"))

        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.reason,
            "economic goal conservative day loss limit exceeded",
        )

    def test_proven_out_of_window_loss_does_not_consume_session_or_day_room(self) -> None:
        for field_name in ("max_session_loss_fraction", "max_day_loss_fraction"):
            with self.subTest(field_name=field_name):
                book = PaperBook("100")
                lost = book.open_ticket(
                    [self._leg()],
                    Decimal("4"),
                    placed_at="2026-09-16T12:00:00+00:00",
                )
                book.settle(
                    lost.ticket_id,
                    set(),
                    settled_at="2026-09-16T12:30:00+00:00",
                )
                policy = self._policy(
                    self._goal(**{field_name: Decimal("0.05")})
                )
                context = replace(
                    self._context(),
                    measurement_window_start="2026-09-16T14:00:00+00:00",
                    measurement_window_end="2026-09-16T15:00:00+00:00",
                )

                self.assertEqual(
                    policy.derive_goal_stake(
                        book,
                        Decimal("1"),
                        context=context,
                    ),
                    Decimal("5"),
                )
                self.assertTrue(
                    policy.evaluate(
                        book,
                        Decimal("5"),
                        context=context,
                    ).allowed
                )
                self.assertFalse(
                    policy.evaluate(
                        book,
                        Decimal("5.01"),
                        context=context,
                    ).allowed
                )

    def test_proven_in_window_loss_consumes_exact_loss_room(self) -> None:
        book = PaperBook("100")
        lost = book.open_ticket(
            [self._leg()],
            Decimal("4"),
            placed_at="2026-09-16T14:00:00+00:00",
        )
        book.settle(
            lost.ticket_id,
            set(),
            settled_at="2026-09-16T14:30:00+00:00",
        )
        policy = self._policy(
            self._goal(max_session_loss_fraction=Decimal("0.05"))
        )
        context = replace(
            self._context(),
            measurement_window_start="2026-09-16T14:00:00+00:00",
            measurement_window_end="2026-09-16T15:00:00+00:00",
        )

        self.assertTrue(
            policy.evaluate(book, Decimal("1"), context=context).allowed
        )
        blocked = policy.evaluate(
            book,
            Decimal("1.01"),
            context=context,
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.reason,
            "economic goal conservative session loss limit exceeded",
        )

    def test_legacy_unknown_settlement_time_remains_conservatively_in_window_after_restart(self) -> None:
        book = PaperBook("100")
        lost = book.open_ticket(
            [self._leg()],
            Decimal("4"),
            placed_at="2026-09-16T12:00:00+00:00",
        )
        book.settle(
            lost.ticket_id,
            set(),
            settled_at="2026-09-16T12:30:00+00:00",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.json"
            book.save(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = 4
            for ticket in payload["tickets"]:
                ticket.pop("settled_at")
            for entry in payload["lifecycle"]:
                if entry["action"] == "settle":
                    entry.pop("settled_at")
            path.write_text(json.dumps(payload), encoding="utf-8")
            restarted = PaperBook.load(path)

        self.assertIsNone(restarted.tickets[lost.ticket_id].settled_at)
        policy = self._policy(
            self._goal(max_day_loss_fraction=Decimal("0.05"))
        )
        context = replace(
            self._context(),
            measurement_window_start="2026-09-16T14:00:00+00:00",
            measurement_window_end="2026-09-16T15:00:00+00:00",
        )

        self.assertTrue(
            policy.evaluate(restarted, Decimal("1"), context=context).allowed
        )
        blocked = policy.evaluate(
            restarted,
            Decimal("1.01"),
            context=context,
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(
            blocked.reason,
            "economic goal conservative day loss limit exceeded",
        )

    def test_unanchored_measurement_window_cannot_narrow_historical_loss(self) -> None:
        book = PaperBook("100")
        lost = book.open_ticket(
            [self._leg()],
            Decimal("4"),
            placed_at="2026-09-16T12:00:00+00:00",
        )
        book.settle(
            lost.ticket_id,
            set(),
            settled_at="2026-09-16T12:30:00+00:00",
        )
        policy = self._policy(
            self._goal(max_session_loss_fraction=Decimal("0.05"))
        )
        context = replace(
            self._context(),
            proposal_ts=None,
            measurement_window_start="2026-09-16T14:00:00+00:00",
            measurement_window_end="2026-09-16T15:00:00+00:00",
        )

        self.assertEqual(
            policy.derive_goal_stake(
                book,
                Decimal("1"),
                context=context,
            ),
            Decimal("1"),
        )

    def test_known_settlement_after_proposal_time_fails_closed(self) -> None:
        book = PaperBook("100")
        lost = book.open_ticket(
            [self._leg()],
            Decimal("4"),
            placed_at="2026-09-16T14:00:00+00:00",
        )
        book.settle(
            lost.ticket_id,
            set(),
            settled_at="2026-09-16T15:00:03+00:00",
        )
        policy = self._policy(
            self._goal(max_session_loss_fraction=Decimal("0.50"))
        )

        decision = policy.evaluate(
            book,
            Decimal("1"),
            context=self._context(),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "virtual bankroll risk history is invalid",
        )

    def test_measurement_window_cannot_extend_beyond_proposal_time(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "measurement window end must not be after proposal time",
        ):
            replace(
                self._context(),
                measurement_window_start="2026-09-16T14:00:00+00:00",
                measurement_window_end="2026-09-16T15:00:03+00:00",
            )

    def test_drawdown_limit_uses_stake_basis_equity_high_watermark(self) -> None:
        book = PaperBook("100")
        winner = book.open_ticket([self._leg()], Decimal("10"))
        book.settle(winner.ticket_id, {winner.legs[0].quote_key})
        loser = book.open_ticket(
            [TicketLeg("event-2", "market-2", "selection-2", Decimal("2"))],
            Decimal("10"),
        )
        book.settle(loser.ticket_id, set())
        policy = self._policy(
            self._goal(max_drawdown_fraction=Decimal("0.10"))
        )

        self.assertTrue(self._evaluate(policy, book, Decimal("1")).allowed)
        blocked = self._evaluate(policy, book, Decimal("1.01"))

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "economic goal drawdown limit exceeded")

    def test_turnover_limit_counts_every_durable_ticket_stake_including_settled(self) -> None:
        book = PaperBook("100")
        prior = book.open_ticket([self._leg()], Decimal("50"))
        book.settle(
            prior.ticket_id,
            set(),
            {prior.legs[0].quote_key},
        )
        policy = self._policy(
            self._goal(max_turnover_fraction=Decimal("0.51"))
        )

        self.assertTrue(self._evaluate(policy, book, Decimal("1")).allowed)
        blocked = self._evaluate(policy, book, Decimal("1.01"))

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "economic goal turnover limit exceeded")

    def test_nontrivial_risk_of_ruin_requires_provenance_bound_evidence(self) -> None:
        goal = self._goal(max_risk_of_ruin=Decimal("0.01"))
        policy = self._policy(goal)
        book = PaperBook("100")

        missing = self._evaluate(policy, book, Decimal("1"))
        self.assertFalse(missing.allowed)
        self.assertEqual(
            missing.reason,
            "portfolio risk-of-ruin provenance-bound evidence is required by economic goal",
        )

        bare_scalar = policy.evaluate(
            book,
            Decimal("1"),
            context=replace(
                self._context(),
                risk_of_ruin_upper_bound=Decimal("0.01"),
            ),
        )
        self.assertFalse(bare_scalar.allowed)
        self.assertEqual(
            bare_scalar.reason,
            "portfolio risk-of-ruin provenance-bound evidence is required by economic goal",
        )

        at_boundary = policy.evaluate(
            book,
            Decimal("1"),
            context=self._bound_ruin_context(
                policy,
                book,
                Decimal("1"),
                Decimal("0.01"),
            ),
        )
        self.assertFalse(at_boundary.allowed)
        self.assertEqual(
            at_boundary.reason,
            "portfolio risk-of-ruin evidence lacks product-issued durable authority",
        )

        exceeded = policy.evaluate(
            book,
            Decimal("1"),
            context=self._bound_ruin_context(
                policy,
                book,
                Decimal("1"),
                Decimal("0.0100001"),
            ),
        )
        self.assertFalse(exceeded.allowed)
        self.assertEqual(
            exceeded.reason,
            "portfolio risk-of-ruin upper bound exceeds economic goal limit",
        )

        wrong_stake = policy.evaluate(
            book,
            Decimal("1.01"),
            context=self._bound_ruin_context(
                policy,
                book,
                Decimal("1"),
                Decimal("0.01"),
            ),
        )
        self.assertFalse(wrong_stake.allowed)
        self.assertEqual(
            wrong_stake.reason,
            "portfolio risk-of-ruin evidence does not match exact proposal state",
        )

    def test_risk_of_ruin_evidence_rejects_future_or_changed_portfolio_state(self) -> None:
        goal = self._goal(max_risk_of_ruin=Decimal("0.01"))
        policy = self._policy(goal)
        book = PaperBook("100")

        future = policy.evaluate(
            book,
            Decimal("1"),
            context=self._bound_ruin_context(
                policy,
                book,
                Decimal("1"),
                Decimal("0.01"),
                evaluated_at="2026-09-16T15:00:03+00:00",
            ),
        )
        self.assertFalse(future.allowed)
        self.assertEqual(
            future.reason,
            "portfolio risk-of-ruin evidence uses future information",
        )

        bound = self._bound_ruin_context(
            policy,
            book,
            Decimal("1"),
            Decimal("0.01"),
        )
        book.open_ticket(
            [self._leg()],
            Decimal("1"),
            placed_at="2026-09-16T14:00:00+00:00",
            provider_source_ids=("provider-1",),
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
        )
        changed = policy.evaluate(book, Decimal("1"), context=bound)
        self.assertFalse(changed.allowed)
        self.assertEqual(
            changed.reason,
            "portfolio risk-of-ruin evidence does not match exact proposal state",
        )

    def test_risk_of_ruin_portfolio_binding_is_stable_across_exact_restart(self) -> None:
        goal = self._goal(max_risk_of_ruin=Decimal("0.01"))
        policy = self._policy(goal)
        book = PaperBook("100")
        bound = self._bound_ruin_context(
            policy,
            book,
            Decimal("1"),
            Decimal("0.01"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper.json"
            book.save(path)
            restarted = PaperBook.load(path)

        decision = policy.evaluate(restarted, Decimal("1"), context=bound)
        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.reason,
            "portfolio risk-of-ruin evidence lacks product-issued durable authority",
        )

    def test_owner_concurrent_position_limit_counts_only_canonical_open_tickets(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket([self._leg()], Decimal("10"))
        policy = self._policy(self._goal(max_concurrent_positions=1))

        blocked = self._evaluate(policy, book, Decimal("1"))
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "economic goal concurrent position limit exceeded")

        book.settle(ticket.ticket_id, {ticket.legs[0].quote_key})
        self.assertTrue(self._evaluate(policy, book, Decimal("1")).allowed)

    def test_zero_owner_concurrent_position_limit_denies_first_new_exposure(self) -> None:
        policy = self._policy(self._goal(max_concurrent_positions=0))
        decision = self._evaluate(policy, PaperBook("100"), Decimal("1"))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "economic goal concurrent position limit exceeded")

    def test_owner_emergency_stop_denies_without_mutating_book_goal_or_decimal_context(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket([self._leg()], Decimal("10"))
        goal = self._goal(emergency_stop=True)
        goal_before = replace(goal)
        policy = self._policy(goal)
        book_before = (
            book.balance,
            book.committed_stake,
            tuple(
                (ticket_id, value.status, value.stake)
                for ticket_id, value in book.tickets.items()
            ),
        )

        with localcontext() as caller:
            caller.prec = 3
            caller.Emax = 1
            caller.Emin = -1
            caller.clear_flags()
            decision = policy.evaluate(book, Decimal("1"))
            self.assertFalse(any(caller.flags.values()))

        book_after = (
            book.balance,
            book.committed_stake,
            tuple(
                (ticket_id, value.status, value.stake)
                for ticket_id, value in book.tickets.items()
            ),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "economic goal emergency stop is active")
        self.assertEqual(book_after, book_before)
        self.assertEqual(goal, goal_before)
        self.assertIs(policy.economic_goal, goal)
        self.assertEqual(book.tickets[ticket.ticket_id], ticket)

    def test_non_contract_economic_goal_is_rejected_at_policy_construction(self) -> None:
        with self.assertRaisesRegex(
            TypeError, "economic_goal must be an EconomicGoalContract or None"
        ):
            PaperRiskPolicy(economic_goal=object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
