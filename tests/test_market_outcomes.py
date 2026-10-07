import copy
import unittest
from datetime import datetime, timezone
from decimal import Decimal

import autosport.market_outcomes as market_outcomes_module
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

    def test_market_outcome_identity_readback_rejects_hostile_market_type_value(self):
        class HostileMarketType(str):
            def __hash__(self):
                raise AssertionError(
                    "hostile serialized market_type hashed before exact admission"
                )

            def __eq__(self, other):
                raise AssertionError(
                    "hostile serialized market_type compared before exact admission"
                )

        payload = self._raw_identity().to_dict()
        payload["market_type"] = HostileMarketType(payload["market_type"])
        with self.assertRaisesRegex(
            ValueError,
            "serialized market outcome identity is invalid",
        ):
            MarketOutcomeIdentity.from_dict(payload)

    def test_betfair_runner_identity_rejects_virtual_string_coercion(self):
        class HostileRunnerId(int):
            def __str__(self):
                raise AssertionError(
                    "runner identity must not dispatch virtual string coercion"
                )

        market_definition = self._market_definition(("away", "home"))
        market_definition["runners"] = [
            {"id": HostileRunnerId(101)},
            {"id": 202},
        ]
        with self.assertRaisesRegex(
            ValueError,
            r"marketDefinition\.runners\[0\]\.id must be a canonical string or integer",
        ):
            assess_betfair_historical_market_definition_authority(
                market_id="match_odds",
                market_definition=market_definition,
                provider_publish_at="2026-09-18T15:00:00Z",
                observed_at="2026-09-18T15:00:01Z",
            )

    def test_betfair_runner_identity_accepts_exact_integer_ids_without_aliasing(self):
        market_definition = self._market_definition(("away", "home"))
        market_definition["runners"] = [{"id": 202}, {"id": 101}]
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="match_odds",
            market_definition=market_definition,
            provider_publish_at="2026-09-18T15:00:00Z",
            observed_at="2026-09-18T15:00:01Z",
        )
        self.assertEqual(assessment.status, OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE)
        self.assertIsNotNone(assessment.authority)
        self.assertEqual(assessment.authority.selection_ids, ("101", "202"))

    def test_market_outcome_identity_readback_rejects_subclass_constructor(self):
        class HostileIdentity(MarketOutcomeIdentity):
            pass

        payload = self._raw_identity().to_dict()
        with self.assertRaisesRegex(
            TypeError,
            "market outcome identity readback requires canonical type",
        ):
            HostileIdentity.from_dict(payload)

    def test_market_outcome_identity_read_boundaries_revalidate_mutated_fields(self):
        class HostileSourceId(str):
            def strip(self, *args, **kwargs):
                raise AssertionError(
                    "mutated market identity must fail before virtual strip dispatch"
                )

            def __hash__(self):
                raise AssertionError(
                    "mutated market identity must fail before hash dispatch"
                )

            def __format__(self, spec):
                raise AssertionError(
                    "mutated market identity must fail before format dispatch"
                )

        accessors = (
            lambda identity: identity.identity_key,
            lambda identity: identity.market_key,
            lambda identity: identity.quote_key("home"),
            lambda identity: identity.to_dict(),
        )
        for accessor in accessors:
            with self.subTest(accessor=accessor):
                identity = self._raw_identity()
                object.__setattr__(
                    identity,
                    "source_id",
                    HostileSourceId(identity.source_id),
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "market outcome source_id must be a non-empty canonical string",
                ):
                    accessor(identity)

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

    def test_durable_authority_readback_rejects_hostile_key_before_hash_dispatch(self):
        authority = self._authority()
        reverified = self._authority()
        raw = authority.to_dict()

        class HostileKey(str):
            armed = False

            def __hash__(self):
                if self.armed:
                    raise AssertionError(
                        "hostile serialized authority key hashed before exact admission"
                    )
                return str.__hash__(self)

            def __eq__(self, other):
                if self.armed:
                    raise AssertionError(
                        "hostile serialized authority key compared before exact admission"
                    )
                return str.__eq__(self, other)

        key = HostileKey("schema")
        schema = raw.pop("schema")
        raw[key] = schema
        key.armed = True

        with self.assertRaisesRegex(
            ValueError,
            "serialized market outcome authority must contain canonical fields",
        ):
            MarketSettlementOutcomeAuthority.from_dict(
                raw,
                verified_authority=reverified,
            )

    def test_durable_authority_readback_rejects_hostile_scalar_before_comparison(self):
        authority = self._authority()
        reverified = self._authority()
        raw = authority.to_dict()

        class HostileText(str):
            def __hash__(self):
                raise AssertionError(
                    "hostile serialized authority text hashed before exact admission"
                )

            def __eq__(self, other):
                raise AssertionError(
                    "hostile serialized authority text compared before exact admission"
                )

            def strip(self, *args, **kwargs):
                raise AssertionError(
                    "hostile serialized authority text stripped before exact admission"
                )

        raw["authority_sha256"] = HostileText(raw["authority_sha256"])
        with self.assertRaisesRegex(
            ValueError,
            "serialized authority_sha256 must be a non-empty canonical string",
        ):
            MarketSettlementOutcomeAuthority.from_dict(
                raw,
                verified_authority=reverified,
            )

    def test_durable_authority_readback_rejects_hostile_nested_selection_identity(self):
        authority = self._authority()
        reverified = self._authority()
        raw = authority.to_dict()

        class HostileSelectionId(str):
            def __hash__(self):
                raise AssertionError(
                    "hostile selection identity hashed before exact admission"
                )

            def __eq__(self, other):
                raise AssertionError(
                    "hostile selection identity compared before exact admission"
                )

            def strip(self, *args, **kwargs):
                raise AssertionError(
                    "hostile selection identity stripped before exact admission"
                )

        raw["selection_ids"][0] = HostileSelectionId(raw["selection_ids"][0])
        with self.assertRaisesRegex(
            ValueError,
            r"serialized market outcome selection_ids\[0\] must be a non-empty canonical string",
        ):
            MarketSettlementOutcomeAuthority.from_dict(
                raw,
                verified_authority=reverified,
            )

    def test_settlement_authority_read_boundaries_revalidate_mutated_semantics(self):
        accessors = (
            lambda authority: authority.quote_keys,
            lambda authority: authority.terminal_state_count,
            lambda authority: authority.terminal_space_exact,
            lambda authority: authority.terminal_states,
            lambda authority: authority.assert_available_as_of(self.DECISION_AS_OF),
            lambda authority: authority.authority_sha256,
            lambda authority: authority.to_dict(),
        )
        for accessor in accessors:
            with self.subTest(accessor=accessor):
                authority = self._authority(("away", "home"))
                object.__setattr__(
                    authority,
                    "settlement_semantics",
                    "forged-settlement-semantics",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "settlement_semantics must be SettlementSemantics",
                ):
                    accessor(authority)

    def test_durable_readback_rejects_verified_authority_subclass_before_dispatch(self):
        authority = self._authority(("away", "home"))

        class HostileAuthority(MarketSettlementOutcomeAuthority):
            __slots__ = ("_armed",)

            def __getattribute__(self, name):
                if name == "to_dict":
                    try:
                        armed = object.__getattribute__(self, "_armed")
                    except AttributeError:
                        armed = False
                    if armed:
                        raise AssertionError(
                            "verified authority subclass dispatched before exact admission"
                        )
                return super().__getattribute__(name)

        hostile = HostileAuthority(
            identity=authority.identity,
            selection_ids=authority.selection_ids,
            roster_basis=authority.roster_basis,
            settlement_semantics=authority.settlement_semantics,
            source_revision=authority.source_revision,
            causal_cutoff=authority.causal_cutoff,
            observed_at=authority.observed_at,
            roster_provenance_sha256=authority.roster_provenance_sha256,
            settlement_rules_sha256=authority.settlement_rules_sha256,
            verification_protocol_sha256=authority.verification_protocol_sha256,
            _verification_token=authority._verification_token,
        )
        object.__setattr__(hostile, "_armed", True)

        with self.assertRaisesRegex(
            ValueError,
            "requires separately verified source authority",
        ):
            MarketSettlementOutcomeAuthority.from_dict(
                authority.to_dict(),
                verified_authority=hostile,
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

    def test_settlement_authority_rejects_identity_subclass_before_member_dispatch(self):
        authority = self._authority(("away", "home"))

        class HostileIdentity(MarketOutcomeIdentity):
            __slots__ = ("_armed",)

            def __getattribute__(self, name):
                if name in {"market_type", "quote_key"}:
                    try:
                        armed = object.__getattribute__(self, "_armed")
                    except AttributeError:
                        armed = False
                    if armed:
                        raise AssertionError(
                            "market identity member dispatch must not execute before exact admission"
                        )
                return super().__getattribute__(name)

        base_identity = authority.identity
        hostile_identity = HostileIdentity(
            sport=base_identity.sport,
            event_id=base_identity.event_id,
            market_id=base_identity.market_id,
            source_id=base_identity.source_id,
            market_type=base_identity.market_type,
        )
        object.__setattr__(hostile_identity, "_armed", True)

        with self.assertRaisesRegex(TypeError, "identity must be MarketOutcomeIdentity"):
            MarketSettlementOutcomeAuthority(
                identity=hostile_identity,
                selection_ids=authority.selection_ids,
                roster_basis=authority.roster_basis,
                settlement_semantics=authority.settlement_semantics,
                source_revision=authority.source_revision,
                causal_cutoff=authority.causal_cutoff,
                observed_at=authority.observed_at,
                roster_provenance_sha256=authority.roster_provenance_sha256,
                settlement_rules_sha256=authority.settlement_rules_sha256,
                verification_protocol_sha256=authority.verification_protocol_sha256,
                _verification_token=authority._verification_token,
            )

    def test_raw_assessment_rejects_identity_subclass_before_market_type_dispatch(self):
        base = self._raw_identity()

        class HostileIdentity(MarketOutcomeIdentity):
            __slots__ = ("_armed",)

            def __getattribute__(self, name):
                if name == "market_type":
                    try:
                        armed = object.__getattribute__(self, "_armed")
                    except AttributeError:
                        armed = False
                    if armed:
                        raise AssertionError(
                            "assessment identity member dispatch must not execute before exact admission"
                        )
                return super().__getattribute__(name)

        hostile = HostileIdentity(
            sport=base.sport,
            event_id=base.event_id,
            market_id=base.market_id,
            source_id=base.source_id,
            market_type=base.market_type,
        )
        object.__setattr__(hostile, "_armed", True)

        with self.assertRaisesRegex(TypeError, "identity must be MarketOutcomeIdentity"):
            assess_market_outcome_authority(
                identity=hostile,
                selection_ids=("away", "home"),
                roster_basis=OutcomeRosterBasis.PROVIDER_MARKET_DEFINITION,
                settlement_semantics=SettlementSemantics.EXCLUSIVE_SINGLE_WINNER,
                source_revision="revision-1",
                causal_cutoff="2026-09-18T15:00:00Z",
                observed_at="2026-09-18T15:00:01Z",
                roster_provenance_sha256="a" * 64,
                settlement_rules_sha256="b" * 64,
                verification_protocol_sha256="c" * 64,
            )

    def test_assessment_revalidates_mutated_exact_identity_before_status_logic(self):
        assessment = self._raw_assessment()

        class HostileSourceId(str):
            def strip(self, *args, **kwargs):
                raise AssertionError(
                    "mutated assessment identity must fail before virtual strip dispatch"
                )

        object.__setattr__(
            assessment.identity,
            "source_id",
            HostileSourceId(assessment.identity.source_id),
        )

        with self.assertRaisesRegex(
            ValueError,
            "market outcome source_id must be a non-empty canonical string",
        ):
            type(assessment).__post_init__(assessment)

    def test_authoritative_search_revalidates_mutated_exact_identity_before_sort(self):
        authority = self._authority(("away", "home"))

        class HostileSourceId(str):
            def strip(self, *args, **kwargs):
                raise AssertionError(
                    "mutated source identity must be rejected before virtual strip dispatch"
                )

            def __lt__(self, other):
                raise AssertionError(
                    "mutated source identity must be rejected before sort dispatch"
                )

            def __hash__(self):
                raise AssertionError(
                    "mutated source identity must be rejected before hash dispatch"
                )

        object.__setattr__(
            authority.identity,
            "source_id",
            HostileSourceId(authority.identity.source_id),
        )

        with self.assertRaisesRegex(
            ValueError,
            "market outcome source_id must be a non-empty canonical string",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [],
                [authority],
                decision_as_of=self.DECISION_AS_OF,
            )

    def test_authoritative_search_rejects_authority_subclass_before_identity_dispatch(self):
        authority = self._authority(("away", "home"))

        class HostileAuthority(MarketSettlementOutcomeAuthority):
            __slots__ = ("_armed",)

            def __getattribute__(self, name):
                if name in {"identity", "authority_sha256"}:
                    try:
                        armed = object.__getattribute__(self, "_armed")
                    except AttributeError:
                        armed = False
                    if armed:
                        raise AssertionError(
                            "authority member dispatch must not execute before exact admission"
                        )
                return super().__getattribute__(name)

        hostile = HostileAuthority(
            identity=authority.identity,
            selection_ids=authority.selection_ids,
            roster_basis=authority.roster_basis,
            settlement_semantics=authority.settlement_semantics,
            source_revision=authority.source_revision,
            causal_cutoff=authority.causal_cutoff,
            observed_at=authority.observed_at,
            roster_provenance_sha256=authority.roster_provenance_sha256,
            settlement_rules_sha256=authority.settlement_rules_sha256,
            verification_protocol_sha256=authority.verification_protocol_sha256,
            _verification_token=authority._verification_token,
        )
        object.__setattr__(hostile, "_armed", True)

        with self.assertRaisesRegex(
            ValueError,
            "authoritative outcome analysis requires canonical market authorities",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [],
                [hostile],
                decision_as_of=self.DECISION_AS_OF,
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

    def test_cross_provider_rule_disagreement_cannot_share_state_axis(self):
        authority_a = self._authority(("away", "home"))
        authority_b = copy.copy(authority_a)
        object.__setattr__(
            authority_b,
            "identity",
            MarketOutcomeIdentity(
                sport="table_tennis",
                event_id="event-1",
                market_id="match_odds",
                source_id="provider-b",
                market_type=MarketType.WINNER,
            ),
        )
        object.__setattr__(
            authority_b,
            "settlement_rules_sha256",
            "d" * 64,
        )

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
            "provider authorities disagree on canonical settlement rules",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [ticket],
                [authority_a, authority_b],
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


def test_market_outcome_authority_rejects_nonzero_submicrosecond_timestamp() -> None:
    with unittest.TestCase().assertRaisesRegex(ValueError, "precision finer than microseconds"):
        market_outcomes_module._canonical_timestamp(
            "observed_at",
            "2026-09-18T15:00:01.1234561Z",
        )
