from __future__ import annotations

import copy
import unittest

import autosport.market_outcomes as market_outcomes_module
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from autosport.domain import MarketType, TicketLeg
from autosport.market_outcomes import (
    MarketOutcomeAuthorityAssessment,
    MarketOutcomeIdentity,
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    OutcomeRosterBasis,
    SettlementSemantics,
)
from autosport.paper import PaperBook
from autosport.scenario_search import ScenarioSearchEngine


class MarketOutcomeAuthorityIntegrityTests(unittest.TestCase):
    SOURCE_ID = "betfair_exchange_historical"
    DECISION_AS_OF = datetime(2026, 9, 18, 15, 0, 2, tzinfo=timezone.utc)

    @staticmethod
    def _market_definition() -> dict[str, object]:
        return {
            "eventId": "event-1",
            "eventTypeId": "2593174",
            "marketType": "MATCH_ODDS",
            "status": "OPEN",
            "complete": True,
            "runners": [{"id": "away"}, {"id": "home"}],
        }

    def _authority(self) -> MarketSettlementOutcomeAuthority:
        """Issue an internal fixture for copy/mutation integrity mechanics only."""

        identity = MarketOutcomeIdentity(
            sport="table_tennis",
            event_id="event-1",
            market_id="match_odds",
            source_id=self.SOURCE_ID,
            market_type=MarketType.WINNER,
        )
        issuance = market_outcomes_module._VERIFIED_AUTHORITY_ISSUANCE.set(True)
        try:
            return MarketSettlementOutcomeAuthority(
                identity=identity,
                selection_ids=("away", "home"),
                roster_basis=OutcomeRosterBasis.GOVERNED_DATASET_MARKET_DEFINITION,
                settlement_semantics=SettlementSemantics.CANONICAL_WIN_LOSS_VOID_SUPERSET,
                source_revision="test-only-governed-dataset-revision",
                causal_cutoff="2026-09-18T15:00:00Z",
                observed_at="2026-09-18T15:00:01Z",
                roster_provenance_sha256="a" * 64,
                settlement_rules_sha256="b" * 64,
                verification_protocol_sha256="c" * 64,
                _verification_token=market_outcomes_module._VERIFIED_AUTHORITY_TOKEN,
            )
        finally:
            market_outcomes_module._VERIFIED_AUTHORITY_ISSUANCE.reset(issuance)

    def test_internal_issued_fixture_has_usable_integrity_binding(self) -> None:
        authority = self._authority()

        authority.assert_issued_integrity()
        self.assertEqual(len(authority.authority_sha256), 64)
        self.assertEqual(authority.terminal_state_count, 9)
        self.assertEqual(len(authority.terminal_states), 9)

    def test_shallow_copy_does_not_inherit_product_issuance(self) -> None:
        authority = self._authority()
        copied = copy.copy(authority)

        self.assertIsNot(copied, authority)
        with self.assertRaisesRegex(
            ValueError,
            "not the exact product-issued instance",
        ):
            copied.assert_issued_integrity()
        with self.assertRaisesRegex(
            ValueError,
            "not the exact product-issued instance",
        ):
            _ = copied.authority_sha256

    def test_dataclasses_replace_cannot_reissue_verified_authority(self) -> None:
        authority = self._authority()

        with self.assertRaisesRegex(
            TypeError,
            "must come from verified evidence",
        ):
            replace(
                authority,
                source_revision="caller-rewritten-source-revision",
            )

    def test_in_place_semantic_mutation_revokes_issued_integrity(self) -> None:
        authority = self._authority()
        original_digest = authority.authority_sha256

        object.__setattr__(
            authority,
            "settlement_rules_sha256",
            "d" * 64,
        )

        self.assertEqual(len(original_digest), 64)
        with self.assertRaisesRegex(
            ValueError,
            "mutated after verified issuance",
        ):
            authority.assert_issued_integrity()
        with self.assertRaisesRegex(
            ValueError,
            "mutated after verified issuance",
        ):
            _ = authority.terminal_states
        with self.assertRaisesRegex(
            ValueError,
            "mutated after verified issuance",
        ):
            authority.to_dict()

    def test_authoritative_scenario_consumer_rejects_copied_authority(self) -> None:
        authority = self._authority()
        copied = copy.copy(authority)
        book = PaperBook("100")
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2.2"),
            sport="table_tennis",
        )
        ticket = book.open_ticket(
            [home],
            "10",
            provider_source_ids=(self.SOURCE_ID,),
        )

        with self.assertRaisesRegex(
            ValueError,
            "not the exact product-issued instance",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [ticket],
                [copied],
                decision_as_of=self.DECISION_AS_OF,
            )

    def test_authoritative_scenario_consumer_rejects_in_place_tamper(self) -> None:
        authority = self._authority()
        book = PaperBook("100")
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2.2"),
            sport="table_tennis",
        )
        ticket = book.open_ticket(
            [home],
            "10",
            provider_source_ids=(self.SOURCE_ID,),
        )
        object.__setattr__(
            authority,
            "selection_ids",
            ("away", "forged", "home"),
        )

        with self.assertRaisesRegex(
            ValueError,
            "mutated after verified issuance",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [ticket],
                [authority],
                decision_as_of=self.DECISION_AS_OF,
            )


    def _open_home_ticket(self, book: PaperBook):
        home = TicketLeg(
            "event-1",
            "match_odds",
            "home",
            Decimal("2.2"),
            sport="table_tennis",
        )
        return book.open_ticket(
            [home],
            "10",
            provider_source_ids=(self.SOURCE_ID,),
        )

    def test_normal_authority_access_rejects_class_method_rebinding(self) -> None:
        authority = self._authority()
        original = vars(MarketSettlementOutcomeAuthority)["assert_issued_integrity"]
        hostile_calls: list[object] = []

        def hostile(instance):
            hostile_calls.append(instance)

        type.__setattr__(
            MarketSettlementOutcomeAuthority,
            "assert_issued_integrity",
            hostile,
        )
        try:
            with self.assertRaisesRegex(
                ValueError,
                "canonical market outcome authority class dispatch was replaced",
            ):
                _ = authority.authority_sha256
        finally:
            type.__setattr__(
                MarketSettlementOutcomeAuthority,
                "assert_issued_integrity",
                original,
            )

        self.assertEqual(hostile_calls, [])
        authority.assert_issued_integrity()

    def test_normal_authority_access_rejects_property_rebinding(self) -> None:
        authority = self._authority()
        original = vars(MarketSettlementOutcomeAuthority)["terminal_states"]
        hostile_calls: list[object] = []

        def hostile(instance):
            hostile_calls.append(instance)
            return ()

        type.__setattr__(
            MarketSettlementOutcomeAuthority,
            "terminal_states",
            property(hostile),
        )
        try:
            with self.assertRaisesRegex(
                ValueError,
                "canonical market outcome authority class dispatch was replaced",
            ):
                _ = authority.terminal_states
        finally:
            type.__setattr__(
                MarketSettlementOutcomeAuthority,
                "terminal_states",
                original,
            )

        self.assertEqual(hostile_calls, [])
        self.assertEqual(len(authority.terminal_states), 9)

    def test_normal_authority_access_rejects_in_place_method_code_replacement(
        self,
    ) -> None:
        authority = self._authority()
        target = vars(MarketSettlementOutcomeAuthority)["assert_issued_integrity"]
        original_code = target.__code__

        def hostile(instance):
            del instance
            raise AssertionError("hostile integrity code executed")

        self.assertEqual(original_code.co_freevars, ())
        self.assertEqual(hostile.__code__.co_freevars, ())
        target.__code__ = hostile.__code__
        try:
            with self.assertRaisesRegex(
                ValueError,
                "canonical market outcome authority class dispatch was replaced",
            ):
                _ = authority.terminal_state_count
        finally:
            target.__code__ = original_code

        self.assertEqual(authority.terminal_state_count, 9)

    def test_authoritative_scenario_rejects_getattribute_root_rebinding(
        self,
    ) -> None:
        authority = self._authority()
        book = PaperBook("100")
        ticket = self._open_home_ticket(book)
        original = vars(MarketSettlementOutcomeAuthority)["__getattribute__"]
        hostile_calls: list[str] = []

        def hostile(instance, name):
            hostile_calls.append(name)
            return object.__getattribute__(instance, name)

        type.__setattr__(
            MarketSettlementOutcomeAuthority,
            "__getattribute__",
            hostile,
        )
        try:
            with self.assertRaisesRegex(
                ValueError,
                "canonical market outcome authority class dispatch was replaced",
            ):
                ScenarioSearchEngine().analyse_authoritative(
                    [ticket],
                    [authority],
                    decision_as_of=self.DECISION_AS_OF,
                )
        finally:
            type.__setattr__(
                MarketSettlementOutcomeAuthority,
                "__getattribute__",
                original,
            )

        self.assertEqual(hostile_calls, [])

    def test_authoritative_scenario_rejects_dispatch_guard_rebinding(self) -> None:
        authority = self._authority()
        book = PaperBook("100")
        ticket = self._open_home_ticket(book)
        original = (
            market_outcomes_module._assert_canonical_market_outcome_authority_dispatch
        )
        hostile_calls: list[bool] = []

        def hostile_guard():
            hostile_calls.append(True)

        market_outcomes_module._assert_canonical_market_outcome_authority_dispatch = (
            hostile_guard
        )
        try:
            with self.assertRaisesRegex(
                ValueError,
                "canonical market outcome authority dispatch guard was replaced",
            ):
                ScenarioSearchEngine().analyse_authoritative(
                    [ticket],
                    [authority],
                    decision_as_of=self.DECISION_AS_OF,
                )
        finally:
            market_outcomes_module._assert_canonical_market_outcome_authority_dispatch = (
                original
            )

        self.assertEqual(hostile_calls, [])

    def test_authoritative_scenario_rejects_dispatch_guard_code_replacement(
        self,
    ) -> None:
        authority = self._authority()
        book = PaperBook("100")
        ticket = self._open_home_ticket(book)
        guard = market_outcomes_module._assert_canonical_market_outcome_authority_dispatch
        original_code = guard.__code__
        hostile_calls: list[bool] = []

        def hostile_guard():
            hostile_calls.append(True)

        self.assertEqual(original_code.co_freevars, ())
        self.assertEqual(hostile_guard.__code__.co_freevars, ())
        guard.__code__ = hostile_guard.__code__
        try:
            with self.assertRaisesRegex(
                ValueError,
                "canonical market outcome authority dispatch guard was replaced",
            ):
                ScenarioSearchEngine().analyse_authoritative(
                    [ticket],
                    [authority],
                    decision_as_of=self.DECISION_AS_OF,
                )
        finally:
            guard.__code__ = original_code

        self.assertEqual(hostile_calls, [])


    def test_authoritative_scenario_rejects_subclass_before_hostile_dispatch(
        self,
    ) -> None:
        authority = self._authority()
        book = PaperBook("100")
        ticket = self._open_home_ticket(book)
        hostile_calls: list[str] = []

        class HostileAuthority(MarketSettlementOutcomeAuthority):
            def __getattribute__(self, name: str):
                hostile_calls.append(name)
                raise AssertionError("hostile subclass dispatch executed")

        forged = object.__new__(HostileAuthority)
        with self.assertRaisesRegex(
            ValueError,
            "exact canonical market authorities",
        ):
            ScenarioSearchEngine().analyse_authoritative(
                [ticket],
                [forged],
                decision_as_of=self.DECISION_AS_OF,
            )

        self.assertEqual(hostile_calls, [])
        authority.assert_issued_integrity()

    def test_proven_assessment_rejects_subclass_before_hostile_dispatch(
        self,
    ) -> None:
        authority = self._authority()
        identity = authority.identity
        hostile_calls: list[str] = []

        class HostileAuthority(MarketSettlementOutcomeAuthority):
            def __getattribute__(self, name: str):
                hostile_calls.append(name)
                raise AssertionError("hostile subclass dispatch executed")

        forged = object.__new__(HostileAuthority)
        with self.assertRaisesRegex(
            ValueError,
            "exact canonical authority",
        ):
            MarketOutcomeAuthorityAssessment(
                identity=identity,
                status=OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
                authority=forged,
                refusal_reason=None,
            )

        self.assertEqual(hostile_calls, [])


    def test_durable_readback_rejects_subclass_before_hostile_dispatch(
        self,
    ) -> None:
        authority = self._authority()
        raw = authority.to_dict()
        hostile_calls: list[str] = []

        class HostileAuthority(MarketSettlementOutcomeAuthority):
            def __getattribute__(self, name: str):
                hostile_calls.append(name)
                raise AssertionError("hostile subclass dispatch executed")

        forged = object.__new__(HostileAuthority)
        with self.assertRaisesRegex(
            ValueError,
            "exact canonical source authority",
        ):
            MarketSettlementOutcomeAuthority.from_dict(
                raw,
                verified_authority=forged,
            )

        self.assertEqual(hostile_calls, [])


if __name__ == "__main__":
    unittest.main()
