import copy
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from decimal import Decimal

import autosport.market_outcomes as market_outcomes
import autosport.scenario_search as scenario_search
from autosport.domain import MarketType, TicketLeg
from autosport.market_outcomes import (
    MarketOutcomeIdentity,
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    OutcomeRosterBasis,
    SettlementResult,
    SettlementSemantics,
    assess_betfair_historical_market_definition_authority,
    assess_market_outcome_authority,
)
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine
from autosport.scenario_search import (
    ScenarioGroup,
    ScenarioOutcome,
    ScenarioSearchEngine,
)


class MarketOutcomeAuthorityTests(unittest.TestCase):
    SOURCE_ID = "betfair_exchange_historical"
    DECISION_AS_OF = datetime(2026, 9, 18, 15, 0, 2, tzinfo=timezone.utc)

    def _market_definition(
        self,
        selection_ids: tuple[str, ...] = ("away", "draw", "home"),
        *,
        status: str = "OPEN",
    ) -> dict[str, object]:
        return {
            "eventId": "event-1",
            "eventTypeId": "2593174",
            "marketType": "MATCH_ODDS",
            "status": status,
            "runners": [{"id": selection_id} for selection_id in selection_ids],
        }

    def _authority(
        self,
        selection_ids: tuple[str, ...] = ("away", "draw", "home"),
    ) -> MarketSettlementOutcomeAuthority:
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="match_odds",
            market_definition=self._market_definition(selection_ids),
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(
            assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        self.assertIsNotNone(assessment.authority)
        return assessment.authority  # type: ignore[return-value]

    @staticmethod
    def _raw_identity(
        market_type: MarketType = MarketType.WINNER,
    ) -> MarketOutcomeIdentity:
        return MarketOutcomeIdentity(
            sport="table_tennis",
            event_id="event-1",
            market_id="match_odds",
            source_id="provider-a",
            market_type=market_type,
        )

    def _raw_assessment(
        self,
        *,
        roster_basis: OutcomeRosterBasis = OutcomeRosterBasis.PROVIDER_MARKET_DEFINITION,
        settlement_semantics: SettlementSemantics
        | None = SettlementSemantics.EXCLUSIVE_SINGLE_WINNER,
        market_type: MarketType = MarketType.WINNER,
    ):
        return assess_market_outcome_authority(
            identity=self._raw_identity(market_type),
            selection_ids=("away", "draw", "home"),
            roster_basis=roster_basis,
            settlement_semantics=settlement_semantics,
            source_revision="caller-asserted-revision",
            causal_cutoff="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
            roster_provenance_sha256="a" * 64,
            settlement_rules_sha256="b" * 64,
            verification_protocol_sha256="c" * 64,
        )

    def test_verified_betfair_roster_derives_conservative_win_loss_void_cover(self):
        authority = self._authority()
        self.assertEqual(authority.selection_ids, ("away", "draw", "home"))
        self.assertEqual(authority.terminal_state_count, 27)
        self.assertFalse(authority.terminal_space_exact)
        self.assertEqual(len(authority.terminal_states), 27)

        target = next(
            state
            for state in authority.terminal_states
            if dict(state.settlements)
            == {
                "away": SettlementResult.VOID,
                "draw": SettlementResult.LOSS,
                "home": SettlementResult.WIN,
            }
        )
        self.assertEqual(
            authority.settlement_by_quote(target)[
                authority.identity.quote_key("away")
            ],
            "void",
        )
        self.assertEqual(
            authority.settlement_by_quote(target)[
                authority.identity.quote_key("home")
            ],
            "win",
        )

    def test_raw_caller_roster_cannot_self_assert_exhaustive_authority(self):
        assessment = self._raw_assessment()
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertIsNone(assessment.authority)
        self.assertEqual(
            assessment.refusal_reason,
            "caller_supplied_roster_has_no_verified_revision_evidence",
        )

    def test_observed_rows_explicitly_refuse_exhaustive_authority(self):
        assessment = self._raw_assessment(
            roster_basis=OutcomeRosterBasis.OBSERVED_ROWS_ONLY,
        )
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            assessment.refusal_reason,
            "observed_rows_do_not_prove_exhaustive_selection_roster",
        )

    def test_unknown_settlement_semantics_explicitly_refuse_authority(self):
        assessment = self._raw_assessment(settlement_semantics=None)
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            assessment.refusal_reason,
            "settlement_semantics_not_proven",
        )

    def test_non_winner_market_explicitly_refuses_current_supported_authority(self):
        assessment = self._raw_assessment(market_type=MarketType.TOTAL)
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            assessment.refusal_reason,
            "market_type_has_no_supported_terminal_settlement_semantics",
        )

    def test_betfair_adapter_fails_closed_if_market_definition_changes_during_snapshot(self):
        definition = self._market_definition()
        canonical_json = market_outcomes._canonical_json
        definition_reads = 0

        def mutating_canonical_json(payload):
            nonlocal definition_reads
            encoded = canonical_json(payload)
            if payload is definition:
                definition_reads += 1
                if definition_reads == 1:
                    definition["runners"][0]["id"] = "tampered-away"
            return encoded

        with patch(
            "autosport.market_outcomes._canonical_json",
            side_effect=mutating_canonical_json,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "market_definition changed during canonical snapshot",
            ):
                assess_betfair_historical_market_definition_authority(
                    market_id="match_odds",
                    market_definition=definition,
                    provider_publish_at="2026-09-18T15:00:00Z",
                    observed_at="2026-09-18T15:00:01Z",
                )

    def test_betfair_adapter_refuses_non_open_or_unsupported_definition(self):
        closed = assess_betfair_historical_market_definition_authority(
            market_id="match_odds",
            market_definition=self._market_definition(status="CLOSED"),
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(closed.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            closed.refusal_reason,
            "betfair_market_definition_is_not_open_at_roster_revision",
        )

        unsupported = self._market_definition()
        unsupported["marketType"] = "OVER_UNDER_25"
        refused = assess_betfair_historical_market_definition_authority(
            market_id="total_goals",
            market_definition=unsupported,
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(refused.status, OutcomeAuthorityStatus.REFUSED)
        self.assertEqual(
            refused.refusal_reason,
            "market_type_has_no_supported_terminal_settlement_semantics",
        )

    def test_durable_roundtrip_requires_source_reverification_and_rejects_tamper(self):
        authority = self._authority()
        raw = authority.to_dict()

        with self.assertRaisesRegex(
            ValueError,
            "requires separately verified source authority",
        ):
            MarketSettlementOutcomeAuthority.from_dict(raw)

        # Simulate restart by independently deriving the provider authority again.
        reverified = self._authority()
        restored = MarketSettlementOutcomeAuthority.from_dict(
            raw,
            verified_authority=reverified,
        )
        self.assertEqual(restored, authority)
        self.assertEqual(restored.authority_sha256, authority.authority_sha256)
        self.assertFalse(restored.terminal_space_exact)

        copied_reverified = copy.copy(reverified)
        with self.assertRaisesRegex(
            ValueError,
            "requires separately verified source authority",
        ):
            MarketSettlementOutcomeAuthority.from_dict(
                raw,
                verified_authority=copied_reverified,
            )

        type_confused_schema = copy.deepcopy(raw)
        type_confused_schema["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "unsupported market outcome authority schema"):
            MarketSettlementOutcomeAuthority.from_dict(
                type_confused_schema,
                verified_authority=reverified,
            )

        tampered_count = copy.deepcopy(raw)
        tampered_count["terminal_state_count"] += 1
        with self.assertRaisesRegex(
            ValueError,
            "terminal-state count does not match verified source authority",
        ):
            MarketSettlementOutcomeAuthority.from_dict(
                tampered_count,
                verified_authority=reverified,
            )

        tampered_hash = copy.deepcopy(raw)
        tampered_hash["authority_sha256"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "authority hash mismatch"):
            MarketSettlementOutcomeAuthority.from_dict(
                tampered_hash,
                verified_authority=reverified,
            )

        # A different but internally self-consistent durable authority cannot be
        # substituted for the provider evidence re-verified for this readback.
        other = self._authority(("away", "home"))
        with self.assertRaisesRegex(ValueError, "terminal-state count does not match"):
            MarketSettlementOutcomeAuthority.from_dict(
                other.to_dict(),
                verified_authority=reverified,
            )

    def test_authority_rejects_future_evidence_at_decision_boundary(self):
        authority = self._authority()
        with self.assertRaisesRegex(
            ValueError,
            "not causally available at decision_as_of",
        ):
            authority.assert_available_as_of(
                datetime(2026, 9, 18, 15, 0, 0, tzinfo=timezone.utc)
            )

    def test_datetime_subclass_cannot_execute_inside_authority_causal_check(self):
        authority = self._authority(("away", "home"))
        hooks = 0

        class ExecutableDatetime(datetime):
            def astimezone(self, tz=None):
                nonlocal hooks
                hooks += 1
                object.__setattr__(authority, "source_revision", "tampered-by-time-hook")
                return super().astimezone(tz)

        decision = ExecutableDatetime(
            2026,
            9,
            18,
            15,
            0,
            2,
            tzinfo=timezone.utc,
        )
        with self.assertRaisesRegex(TypeError, "decision_as_of must be an exact datetime"):
            authority.assert_available_as_of(decision)
        self.assertEqual(hooks, 0)
        self.assertNotEqual(authority.source_revision, "tampered-by-time-hook")

    def test_authoritative_search_rejects_authority_collection_change_during_snapshot(self):
        authority = self._authority(("away", "home"))
        authorities = [authority]
        canonical_assert = scenario_search._ASSERT_MARKET_OUTCOME_AUTHORITY
        calls = 0

        def mutating_assert(value):
            nonlocal calls
            canonical_assert(value)
            calls += 1
            if calls == 1:
                authorities.clear()

        with patch(
            "autosport.scenario_search._ASSERT_MARKET_OUTCOME_AUTHORITY",
            side_effect=mutating_assert,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "market outcome authority set changed during snapshot",
            ):
                ScenarioSearchEngine().analyse_authoritative(
                    [],
                    authorities,
                    decision_as_of=self.DECISION_AS_OF,
                )

    def test_authoritative_search_rechecks_collection_after_causal_boundary(self):
        authority = self._authority(("away", "home"))
        authorities = [authority]
        canonical_available = scenario_search._OUTCOME_ASSERT_AVAILABLE

        def mutating_available(value, decision_as_of):
            canonical_available(value, decision_as_of)
            authorities.clear()

        with patch(
            "autosport.scenario_search._OUTCOME_ASSERT_AVAILABLE",
            side_effect=mutating_available,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "market outcome authority set changed during analysis boundary",
            ):
                ScenarioSearchEngine().analyse_authoritative(
                    [],
                    authorities,
                    decision_as_of=self.DECISION_AS_OF,
                )

    def test_empty_portfolio_preserves_conservative_terminal_space_truth(self):
        authority = self._authority()

        report = ScenarioSearchEngine().analyse_authoritative(
            [],
            [authority],
            decision_as_of=self.DECISION_AS_OF,
        )

        self.assertEqual(
            report.mode,
            "authoritative-conservative-enumeration",
        )
        self.assertTrue(report.outcome_space_exhaustive)
        self.assertFalse(report.outcome_space_exact)
        self.assertTrue(report.worst_proven)
        self.assertTrue(report.best_proven)
        self.assertEqual(report.observed_worst, Decimal("0"))
        self.assertEqual(report.observed_best, Decimal("0"))
        self.assertEqual(
            report.outcome_authority_sha256s,
            (authority.authority_sha256,),
        )

    def test_empty_portfolio_rejects_duplicate_provider_authority(self):
        authority = self._authority()

        with self.assertRaisesRegex(
            ValueError,
            "duplicate provider authority for canonical market identity",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [],
                [authority, authority],
                decision_as_of=self.DECISION_AS_OF,
            )

    def test_authoritative_scenario_search_covers_unticketed_real_third_outcome(self):
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
        ticket_home = book.open_ticket(
            [home], "10", provider_source_ids=(self.SOURCE_ID,)
        )
        ticket_away = book.open_ticket(
            [away], "10", provider_source_ids=(self.SOURCE_ID,)
        )

        report = ScenarioSearchEngine().analyse_authoritative(
            [ticket_home, ticket_away],
            [authority],
            decision_as_of=self.DECISION_AS_OF,
        )

        self.assertEqual(
            report.mode,
            "authoritative-conservative-enumeration",
        )
        self.assertTrue(report.outcome_space_exhaustive)
        self.assertFalse(report.outcome_space_exact)
        self.assertFalse(report.worst_proven)
        self.assertFalse(report.best_proven)
        self.assertEqual(report.total_states, 27)
        self.assertEqual(report.observed_worst, Decimal("-20"))
        self.assertEqual(
            report.outcome_authority_sha256s,
            (authority.authority_sha256,),
        )

    def test_mixed_void_and_win_state_reuses_paper_settlement_economics(self):
        authority = self._authority(("away", "home"))
        book = PaperBook("100")
        away = TicketLeg(
            "event-1",
            "match_odds",
            "away",
            Decimal("3"),
            sport="table_tennis",
        )
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2"),
            sport="table_tennis",
        )
        parlay = book.open_ticket(
            [away, home],
            "10",
            provider_source_ids=(self.SOURCE_ID,),
        )
        target = next(
            state
            for state in authority.terminal_states
            if dict(state.settlements)
            == {
                "away": SettlementResult.VOID,
                "home": SettlementResult.WIN,
            }
        )
        profit = PortfolioEngine.scenario_profit_settlements(
            [parlay],
            authority.settlement_by_quote(target),
        )
        self.assertEqual(profit, Decimal("10"))

    def test_authoritative_search_rejects_future_authority(self):
        authority = self._authority(("away", "home"))
        book = PaperBook("100")
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2"),
            sport="table_tennis",
        )
        ticket = book.open_ticket(
            [home], "10", provider_source_ids=(self.SOURCE_ID,)
        )
        with self.assertRaisesRegex(
            ValueError,
            "not causally available at decision_as_of",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [ticket],
                [authority],
                decision_as_of=datetime(
                    2026, 9, 18, 15, 0, 0, tzinfo=timezone.utc
                ),
            )

    def test_authoritative_search_fails_closed_before_materializing_oversized_space(self):
        authority = self._authority()
        book = PaperBook("100")
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2"),
            sport="table_tennis",
        )
        ticket = book.open_ticket(
            [home], "10", provider_source_ids=(self.SOURCE_ID,)
        )
        with self.assertRaisesRegex(
            ValueError,
            "authoritative terminal outcome space exceeds exact_state_limit",
        ):
            ScenarioSearchEngine(exact_state_limit=26).analyse_authoritative(
                [ticket],
                [authority],
                decision_as_of=self.DECISION_AS_OF,
            )

    def test_authoritative_search_requires_every_ticket_leg_to_be_covered(self):
        authority = self._authority(("away", "home"))
        book = PaperBook("100")
        other = TicketLeg(
            "event-1",
            "other-market",
            "other",
            Decimal("2"),
            sport="table_tennis",
        )
        ticket = book.open_ticket(
            [other], "10", provider_source_ids=(self.SOURCE_ID,)
        )
        with self.assertRaisesRegex(
            ValueError,
            "ticket leg missing from authoritative outcome universe",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [ticket],
                [authority],
                decision_as_of=self.DECISION_AS_OF,
            )

    def test_provider_source_mismatch_fails_closed(self):
        authority = self._authority(("away", "home"))
        book = PaperBook("100")
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2"),
            sport="table_tennis",
        )
        ticket = book.open_ticket(
            [home], "10", provider_source_ids=("provider-b",)
        )
        with self.assertRaisesRegex(
            ValueError,
            "ticket provider source does not match authoritative",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [ticket],
                [authority],
                decision_as_of=self.DECISION_AS_OF,
            )

    def test_copied_or_mutated_authority_cannot_enter_terminal_proof(self):
        authority = self._authority(("away", "home"))
        book = PaperBook("100")
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2"),
            sport="table_tennis",
        )
        ticket = book.open_ticket(
            [home], "10", provider_source_ids=(self.SOURCE_ID,)
        )

        copied = copy.copy(authority)
        with self.assertRaisesRegex(
            ValueError,
            "not issued by the canonical provider verifier",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [ticket],
                [copied],
                decision_as_of=self.DECISION_AS_OF,
            )

        object.__setattr__(authority, "selection_ids", ("away", "home", "other"))
        with self.assertRaisesRegex(
            ValueError,
            "changed after issuance",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [ticket],
                [authority],
                decision_as_of=self.DECISION_AS_OF,
            )

        class ForgedIdentity(MarketOutcomeIdentity):
            @property
            def market_key(self):
                return ("table_tennis", "event-1", "forged-market", "winner")

        nested = self._authority(("away", "home"))
        forged_identity = ForgedIdentity(
            sport=nested.identity.sport,
            event_id=nested.identity.event_id,
            market_id=nested.identity.market_id,
            source_id=nested.identity.source_id,
            market_type=nested.identity.market_type,
        )
        object.__setattr__(nested, "identity", forged_identity)
        with self.assertRaisesRegex(
            ValueError,
            "invalid after issuance",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [ticket],
                [nested],
                decision_as_of=self.DECISION_AS_OF,
            )

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
        self.assertFalse(report.outcome_space_exact)
        self.assertEqual(report.outcome_authority_sha256s, ())


if __name__ == "__main__":
    unittest.main()
