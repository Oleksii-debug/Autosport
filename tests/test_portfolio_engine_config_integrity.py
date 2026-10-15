import unittest
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine


class PortfolioEngineConfigurationIntegrityTests(unittest.TestCase):
    def test_engine_rejects_invalid_enumeration_and_sampling_limits(self) -> None:
        cases = (
            ("max_exact_states", 0),
            ("max_exact_states", -1),
            ("max_exact_states", True),
            ("max_exact_states", "10"),
            ("max_exact_states", 1.5),
            ("max_exact_states", Decimal("2")),
            ("max_exact_states", None),
            ("sample_count", 0),
            ("sample_count", -1),
            ("sample_count", False),
            ("sample_count", "10"),
            ("sample_count", 1.5),
            ("sample_count", Decimal("2")),
            ("sample_count", None),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, rf"{field} must be a positive integer"):
                    PortfolioEngine(**{field: value})

    def test_engine_rejects_non_integer_or_boolean_seed(self) -> None:
        for value in (True, False, "7", Decimal("7"), 7.0, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "seed must be an integer"):
                    PortfolioEngine(seed=value)

    def test_ticket_identity_is_revalidated_before_portfolio_dispatch(self) -> None:
        class HostileTicketId(str):
            def strip(self) -> str:
                raise AssertionError(
                    "hostile ticket identity dispatched before exact-type admission"
                )

        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")
        ticket.ticket_id = HostileTicketId(ticket.ticket_id)

        with self.assertRaisesRegex(
            ValueError,
            "portfolio ticket_id must be a non-empty trimmed string",
        ):
            PortfolioEngine().analyse([ticket])

        with self.assertRaisesRegex(
            ValueError,
            "portfolio ticket_id must be a non-empty trimmed string",
        ):
            PortfolioEngine.affected_tickets([ticket], leg.quote_key)

    def test_affected_tickets_rejects_hostile_quote_identity_before_dispatch(self) -> None:
        class HostileQuoteKey(str):
            def strip(self) -> str:
                raise AssertionError(
                    "hostile quote identity dispatched before exact-type admission"
                )

        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")

        with self.assertRaisesRegex(
            ValueError,
            "portfolio quote_key must be a non-empty trimmed string",
        ):
            PortfolioEngine.affected_tickets([ticket], HostileQuoteKey(leg.quote_key))

    def test_scenario_profit_rejects_hostile_winning_quote_before_membership(self) -> None:
        class HostileQuoteKey(str):
            def strip(self) -> str:
                raise AssertionError(
                    "hostile winning quote dispatched before exact-type admission"
                )

        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")

        with self.assertRaisesRegex(
            ValueError,
            r"portfolio winning_quote_keys\[0\] must be a non-empty trimmed string",
        ):
            PortfolioEngine.scenario_profit(
                [ticket],
                {HostileQuoteKey(leg.quote_key)},
            )

    def test_portfolio_snapshot_revalidates_mutated_ticket_leg_identity(self) -> None:
        class HostileEventId(str):
            def encode(self, *args, **kwargs):
                raise AssertionError(
                    "hostile event identity encoded before exact-type admission"
                )

        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")
        object.__setattr__(leg, "event_id", HostileEventId(leg.event_id))

        with self.assertRaisesRegex(
            ValueError,
            "PaperBook event_id.* must be an exact string",
        ):
            PortfolioEngine().analyse([ticket])

    def test_direct_portfolio_paths_reject_ticket_leg_subclass_before_dispatch(self) -> None:
        class HostileLeg(TicketLeg):
            @property
            def quote_key(self) -> str:
                raise AssertionError(
                    "hostile ticket leg dispatched before exact-type admission"
                )

        book = PaperBook("100")
        canonical_leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([canonical_leg], "10")
        ticket.legs = (
            HostileLeg("event", "winner", "alice", Decimal("2")),
        )

        with self.assertRaisesRegex(
            ValueError,
            "PaperBook ticket legs must be canonical TicketLeg values",
        ):
            PortfolioEngine.affected_tickets([ticket], canonical_leg.quote_key)

        with self.assertRaisesRegex(
            ValueError,
            "PaperBook ticket legs must be canonical TicketLeg values",
        ):
            PortfolioEngine.scenario_profit([ticket], {canonical_leg.quote_key})

    def test_portfolio_entrypoints_reject_ticket_list_subclass_before_iteration(self) -> None:
        class HostileTickets(list):
            def __iter__(self):
                raise AssertionError(
                    "hostile ticket container iterated before exact-list admission"
                )

        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")
        hostile = HostileTickets([ticket])

        with self.assertRaisesRegex(ValueError, "portfolio tickets must be an exact list"):
            PortfolioEngine.affected_tickets(hostile, leg.quote_key)

        with self.assertRaisesRegex(ValueError, "portfolio tickets must be an exact list"):
            PortfolioEngine.scenario_profit(hostile, {leg.quote_key})

        with self.assertRaisesRegex(ValueError, "portfolio tickets must be an exact list"):
            PortfolioEngine.scenario_profit_settlements(
                hostile,
                {leg.quote_key: "win"},
            )

        with self.assertRaisesRegex(ValueError, "portfolio tickets must be an exact list"):
            PortfolioEngine().analyse(hostile)

    def test_settlement_map_rejects_control_and_non_utf8_quote_aliases(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")
        invalid_keys = (
            "event" + chr(0) + "|winner|alice",
            "event|winner|" + chr(0xD800),
        )

        for invalid_key in invalid_keys:
            with self.subTest(invalid_key=repr(invalid_key)):
                with self.assertRaisesRegex(
                    ValueError,
                    "settlement_by_quote keys must be non-empty canonical strings",
                ):
                    PortfolioEngine.scenario_profit_settlements(
                        [ticket],
                        {
                            leg.quote_key: "win",
                            invalid_key: "loss",
                        },
                    )

    def test_settlement_map_rejects_quote_key_subclass_before_strip_dispatch(self) -> None:
        dispatch_calls = []

        class HostileQuoteKey(str):
            def strip(self, *args, **kwargs):
                dispatch_calls.append("strip")
                raise AssertionError(
                    "hostile settlement quote identity dispatched before exact-type admission"
                )

        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")

        with self.assertRaisesRegex(
            ValueError,
            "settlement_by_quote keys must be non-empty canonical strings",
        ):
            PortfolioEngine.scenario_profit_settlements(
                [ticket],
                {
                    leg.quote_key: "win",
                    HostileQuoteKey("other|winner|bob"): "loss",
                },
            )

        self.assertEqual(dispatch_calls, [])

    def test_portfolio_decimal_subclasses_fail_before_virtual_dispatch(self) -> None:
        class HostileDecimal(Decimal):
            def is_finite(self):
                raise AssertionError(
                    "hostile Decimal.is_finite dispatched before exact-type admission"
                )

            def __le__(self, other):
                raise AssertionError(
                    "hostile Decimal comparison dispatched before exact-type admission"
                )

        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")
        ticket.stake = HostileDecimal("10")

        with self.assertRaisesRegex(
            ValueError,
            "portfolio ticket .* stake must be a finite Decimal",
        ):
            PortfolioEngine.scenario_profit([ticket], {leg.quote_key})

        ticket.stake = Decimal("10")
        object.__setattr__(leg, "locked_odds", HostileDecimal("2"))

        with self.assertRaisesRegex(
            ValueError,
            "PaperBook snapshot contains non-finite locked_odds",
        ):
            PortfolioEngine.scenario_profit([ticket], {leg.quote_key})

    def test_scenario_profit_rejects_noncanonical_winner_container(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")

        with self.assertRaisesRegex(
            ValueError,
            "portfolio winning_quote_keys must be an exact set of quote keys",
        ):
            PortfolioEngine.scenario_profit([ticket], [leg.quote_key])  # type: ignore[arg-type]

    def test_empty_exclusive_group_fails_before_scenario_enumeration(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")

        with self.assertRaisesRegex(ValueError, "exclusive groups must not be empty"):
            PortfolioEngine().analyse([ticket], exclusive_groups=[set()])

    def test_exclusive_group_rejects_noncanonical_container_before_quote_hash_dispatch(self) -> None:
        class HostileQuoteKey(str):
            def __hash__(self) -> int:
                raise AssertionError(
                    "hostile quote_key hash dispatched before portfolio identity admission"
                )

        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")

        with self.assertRaisesRegex(
            ValueError,
            "exclusive_groups entries must be exact sets of quote keys",
        ):
            PortfolioEngine().analyse(
                [ticket],
                exclusive_groups=[[HostileQuoteKey(leg.quote_key)]],
            )

    def test_exclusive_groups_rejects_list_subclass_before_truthiness_dispatch(self) -> None:
        class HostileGroups(list):
            def __bool__(self) -> bool:
                raise AssertionError(
                    "exclusive_groups truthiness dispatched before container admission"
                )

        with self.assertRaisesRegex(
            ValueError,
            "exclusive_groups must be an exact list",
        ):
            PortfolioEngine().analyse([], exclusive_groups=HostileGroups())

    def test_empty_exclusive_group_fails_for_empty_portfolio(self) -> None:
        with self.assertRaisesRegex(ValueError, "exclusive groups must not be empty"):
            PortfolioEngine().analyse([], exclusive_groups=[set()])

    def test_absent_exclusive_group_fails_for_empty_portfolio(self) -> None:
        with self.assertRaisesRegex(ValueError, "exclusive group contains quote not present in portfolio"):
            PortfolioEngine().analyse([], exclusive_groups=[{"ghost|winner|x"}])

    def test_absent_exclusive_group_fails_for_closed_only_portfolio(self) -> None:
        book = PaperBook("100")
        leg = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([leg], "10")
        book.settle(ticket.ticket_id, {leg.quote_key})

        with self.assertRaisesRegex(ValueError, "exclusive group contains quote not present in portfolio"):
            PortfolioEngine().analyse([ticket], exclusive_groups=[{leg.quote_key}])

    def test_valid_empty_portfolio_keeps_zero_exact_report(self) -> None:
        report = PortfolioEngine().analyse([])
        self.assertEqual(report.mode, "exact")
        self.assertEqual(report.scenario_count, 1)
        self.assertEqual(report.worst_case, Decimal("0"))
        self.assertEqual(report.best_case, Decimal("0"))
        self.assertEqual(report.mean_case, Decimal("0"))

    def test_partial_singleton_group_reserves_unlisted_terminal_outcome(self) -> None:
        book = PaperBook("100")
        alice = TicketLeg("event", "winner", "alice", Decimal("2"))
        ticket = book.open_ticket([alice], "10")

        report = PortfolioEngine().analyse([ticket], exclusive_groups=[{alice.quote_key}])

        self.assertEqual(report.mode, "conservative-enumeration")
        self.assertEqual(report.scenario_count, 2)
        self.assertEqual(report.worst_case, Decimal("-10"))
        self.assertEqual(report.best_case, Decimal("10"))

    def test_multi_member_group_does_not_claim_terminal_completeness(self) -> None:
        book = PaperBook("100")
        alice = TicketLeg("event", "winner", "alice", Decimal("2"))
        bob = TicketLeg("event", "winner", "bob", Decimal("3"))
        tickets = [book.open_ticket([alice], "10"), book.open_ticket([bob], "10")]

        report = PortfolioEngine().analyse(
            tickets,
            exclusive_groups=[{alice.quote_key, bob.quote_key}],
        )

        self.assertEqual(report.mode, "conservative-enumeration")
        self.assertEqual(report.scenario_count, 3)
        self.assertEqual(report.worst_case, Decimal("-20"))
        self.assertEqual(report.best_case, Decimal("10"))

    def test_valid_approximate_configuration_produces_requested_finite_samples(self) -> None:
        book = PaperBook("100")
        alice = TicketLeg("event", "winner", "alice", Decimal("2"))
        bob = TicketLeg("event", "winner", "bob", Decimal("3"))
        tickets = [book.open_ticket([alice], "10"), book.open_ticket([bob], "10")]

        report = PortfolioEngine(max_exact_states=1, sample_count=7, seed=11).analyse(tickets)

        self.assertEqual(report.mode, "approximate")
        self.assertEqual(report.scenario_count, 7)
        self.assertTrue(report.worst_case.is_finite())
        self.assertTrue(report.best_case.is_finite())
        self.assertTrue(report.mean_case.is_finite())

    def test_approximate_sampling_is_stable_across_equivalent_group_order(self) -> None:
        book = PaperBook("1000")
        legs = (
            TicketLeg("event-a", "winner", "a1", Decimal("2")),
            TicketLeg("event-a", "winner", "a2", Decimal("10")),
            TicketLeg("event-b", "winner", "b1", Decimal("3")),
            TicketLeg("event-b", "winner", "b2", Decimal("30")),
            TicketLeg("event-b", "winner", "b3", Decimal("300")),
        )
        tickets = [book.open_ticket([leg], "1") for leg in legs]
        group_a = {legs[0].quote_key, legs[1].quote_key}
        group_b = {legs[2].quote_key, legs[3].quote_key, legs[4].quote_key}
        engine = PortfolioEngine(max_exact_states=1, sample_count=1, seed=7)

        forward = engine.analyse(tickets, exclusive_groups=[group_a, group_b])
        reverse = engine.analyse(tickets, exclusive_groups=[group_b, group_a])

        self.assertEqual(forward.mode, "conservative-approximate")
        self.assertEqual(forward, reverse)


if __name__ == "__main__":
    unittest.main()
