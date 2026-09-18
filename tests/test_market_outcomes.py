import copy
import unittest
from decimal import Decimal

from autosport.domain import MarketType, TicketLeg
from autosport.market_outcomes import (
    MarketOutcomeIdentity,
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    OutcomeRosterBasis,
    SettlementResult,
    SettlementSemantics,
    assess_market_outcome_authority,
)
from autosport.paper import PaperBook
from autosport.scenario_search import (
    ScenarioGroup,
    ScenarioOutcome,
    ScenarioSearchEngine,
)


class MarketOutcomeAuthorityTests(unittest.TestCase):
    def _identity(
        self,
        market_type: MarketType = MarketType.WINNER,
    ) -> MarketOutcomeIdentity:
        return MarketOutcomeIdentity(
            sport="table_tennis",
            event_id="event-1",
            market_id="match_odds",
            source_id="provider-a",
            market_type=market_type,
        )

    def _assessment(
        self,
        *,
        selection_ids: tuple[str, ...] = ("away", "draw", "home"),
        roster_basis: OutcomeRosterBasis = OutcomeRosterBasis.PROVIDER_MARKET_DEFINITION,
        settlement_semantics: SettlementSemantics
        | None = SettlementSemantics.EXCLUSIVE_SINGLE_WINNER,
        market_type: MarketType = MarketType.WINNER,
    ):
        return assess_market_outcome_authority(
            identity=self._identity(market_type),
            selection_ids=selection_ids,
            roster_basis=roster_basis,
            settlement_semantics=settlement_semantics,
            source_revision="provider-seq-42",
            causal_cutoff="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
            roster_provenance_sha256="a" * 64,
            settlement_rules_sha256="b" * 64,
            verification_protocol_sha256="c" * 64,
        )

    def _authority(
        self,
        *,
        selection_ids: tuple[str, ...] = ("away", "draw", "home"),
        semantics: SettlementSemantics = SettlementSemantics.EXCLUSIVE_SINGLE_WINNER,
    ) -> MarketSettlementOutcomeAuthority:
        assessment = self._assessment(
            selection_ids=selection_ids,
            settlement_semantics=semantics,
        )
        self.assertEqual(
            assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        self.assertIsNotNone(assessment.authority)
        return assessment.authority  # type: ignore[return-value]

    def test_three_way_roster_mechanically_derives_every_winner_state(self):
        authority = self._authority()
        self.assertEqual(
            tuple(state.state_id for state in authority.terminal_states),
            ("winner:away", "winner:draw", "winner:home"),
        )
        for state in authority.terminal_states:
            self.assertEqual(
                tuple(selection for selection, _ in state.settlements),
                authority.selection_ids,
            )
            self.assertEqual(
                sum(
                    result is SettlementResult.WIN
                    for _, result in state.settlements
                ),
                1,
            )

    def test_durable_roundtrip_rederives_terminal_states_and_rejects_tamper(self):
        authority = self._authority()
        raw = authority.to_dict()
        restored = MarketSettlementOutcomeAuthority.from_dict(raw)
        self.assertEqual(restored, authority)
        self.assertEqual(restored.authority_sha256, authority.authority_sha256)

        tampered = copy.deepcopy(raw)
        tampered["terminal_states"].pop()
        with self.assertRaisesRegex(
            ValueError,
            "serialized terminal states do not match",
        ):
            MarketSettlementOutcomeAuthority.from_dict(tampered)

        tampered_hash = copy.deepcopy(raw)
        tampered_hash["authority_sha256"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "authority hash mismatch"):
            MarketSettlementOutcomeAuthority.from_dict(tampered_hash)

    def test_observed_rows_explicitly_refuse_exhaustive_authority(self):
        assessment = self._assessment(
            roster_basis=OutcomeRosterBasis.OBSERVED_ROWS_ONLY,
        )
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertIsNone(assessment.authority)
        self.assertEqual(
            assessment.refusal_reason,
            "observed_rows_do_not_prove_exhaustive_selection_roster",
        )

    def test_unknown_settlement_semantics_explicitly_refuse_authority(self):
        assessment = self._assessment(settlement_semantics=None)
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertIsNone(assessment.authority)
        self.assertEqual(
            assessment.refusal_reason,
            "settlement_semantics_not_proven",
        )

    def test_non_winner_market_explicitly_refuses_current_supported_authority(self):
        assessment = self._assessment(market_type=MarketType.TOTAL)
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertIsNone(assessment.authority)
        self.assertEqual(
            assessment.refusal_reason,
            "market_type_has_no_supported_terminal_settlement_semantics",
        )

    def test_authoritative_scenario_search_includes_unticketed_real_third_outcome(self):
        authority = self._authority()
        book = PaperBook("100")
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2.2"),
            sport="table_tennis",
        )
        away = TicketLeg(
            "event-1",
            "match_odds",
            "away",
            Decimal("2.2"),
            sport="table_tennis",
        )
        ticket_home = book.open_ticket([home], "10")
        ticket_away = book.open_ticket([away], "10")

        report = ScenarioSearchEngine().analyse_authoritative(
            [ticket_home, ticket_away],
            [authority],
        )

        self.assertEqual(report.mode, "authoritative-exact-enumeration")
        self.assertTrue(report.worst_proven)
        self.assertTrue(report.best_proven)
        self.assertTrue(report.outcome_space_exhaustive)
        self.assertEqual(
            report.outcome_authority_sha256s,
            (authority.authority_sha256,),
        )
        self.assertEqual(report.total_states, 3)
        self.assertEqual(report.observed_best, Decimal("2.0"))
        self.assertEqual(report.observed_worst, Decimal("-20"))

    def test_legacy_caller_group_never_claims_exhaustive_outcome_space(self):
        book = PaperBook("100")
        home = TicketLeg("e1", "winner", "home", Decimal("2"))
        away = TicketLeg("e1", "winner", "away", Decimal("2"))
        ticket_home = book.open_ticket([home], "10")
        ticket_away = book.open_ticket([away], "10")
        report = ScenarioSearchEngine().analyse(
            [ticket_home, ticket_away],
            [
                ScenarioGroup(
                    "caller-partial",
                    (
                        ScenarioOutcome(home.quote_key),
                        ScenarioOutcome(away.quote_key),
                    ),
                )
            ],
        )
        self.assertFalse(report.outcome_space_exhaustive)
        self.assertEqual(report.outcome_authority_sha256s, ())

    def test_all_void_terminal_semantics_use_canonical_paper_settlement_economics(self):
        authority = self._authority(
            selection_ids=("away", "home"),
            semantics=SettlementSemantics.EXCLUSIVE_SINGLE_WINNER_OR_ALL_VOID,
        )
        book = PaperBook("100")
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2"),
            sport="table_tennis",
        )
        away = TicketLeg(
            "event-1",
            "match_odds",
            "away",
            Decimal("2"),
            sport="table_tennis",
        )
        ticket_home = book.open_ticket([home], "10")
        ticket_away = book.open_ticket([away], "10")

        report = ScenarioSearchEngine().analyse_authoritative(
            [ticket_home, ticket_away],
            [authority],
        )
        self.assertEqual(report.total_states, 3)
        self.assertEqual(report.observed_worst, Decimal("0"))
        self.assertEqual(report.observed_best, Decimal("0"))

    def test_authoritative_search_fails_closed_instead_of_approximating(self):
        authority = self._authority()
        book = PaperBook("100")
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2"),
            sport="table_tennis",
        )
        ticket = book.open_ticket([home], "10")
        with self.assertRaisesRegex(
            ValueError,
            "authoritative terminal outcome space exceeds exact_state_limit",
        ):
            ScenarioSearchEngine(exact_state_limit=2).analyse_authoritative(
                [ticket],
                [authority],
            )

    def test_authoritative_search_requires_every_ticket_leg_to_be_covered(self):
        authority = self._authority(selection_ids=("away", "home"))
        book = PaperBook("100")
        other = TicketLeg(
            "event-1",
            "other-market",
            "other",
            Decimal("2"),
            sport="table_tennis",
        )
        ticket = book.open_ticket([other], "10")
        with self.assertRaisesRegex(
            ValueError,
            "ticket leg missing from authoritative outcome universe",
        ):
            ScenarioSearchEngine().analyse_authoritative([ticket], [authority])


if __name__ == "__main__":
    unittest.main()
