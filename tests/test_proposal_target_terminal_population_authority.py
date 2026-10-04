from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.causal_integrity import contains_forbidden_future_key
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent, MarketType, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.market_outcomes import (
    MarketSettlementOutcomeAuthority,
    OutcomeAuthorityStatus,
    assess_betfair_historical_market_definition_authority,
)
from autosport.paper import PaperBook
from autosport.proposal_risk_target_authority import (
    issue_product_proposal_risk_target,
)
import autosport.proposal_target_terminal_population_authority as terminal_population_authority
from autosport.proposal_target_terminal_population_authority import (
    ProductProposalTargetTerminalPopulation,
    ProductProposalTargetTerminalPopulationError,
    issue_product_proposal_target_terminal_population,
    resolve_product_proposal_target_terminal_population,
)
from autosport.risk import ProposedTicketRiskContext


class ProductProposalTargetTerminalPopulationTests(unittest.TestCase):
    SOURCE_ID = "betfair_exchange_historical"
    DECISION_TS = "2026-09-18T13:20:00+00:00"
    QUOTE_TS = "2026-09-18T13:19:59+00:00"

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name).resolve()
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.authority_root = root / "machine-authority"
        self._env = patch.dict(
            os.environ,
            {"AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(self.authority_root)},
        )
        self._env.start()
        self.goal = EconomicGoalContract(
            goal_id="goal-target-terminal-population",
            revision=1,
            bankroll_id="paper-bankroll",
            currency="USD",
            max_stake_fraction=Decimal("0.10"),
            max_session_loss_fraction=Decimal("1"),
            max_day_loss_fraction=Decimal("1"),
            max_drawdown_fraction=Decimal("1"),
            max_capital_at_risk_fraction=Decimal("1"),
            max_event_concentration_fraction=Decimal("1"),
            max_market_concentration_fraction=Decimal("1"),
            max_provider_concentration_fraction=Decimal("1"),
            max_sport_concentration_fraction=Decimal("1"),
            max_turnover_fraction=Decimal("1000"),
            max_risk_of_ruin=Decimal("0.01"),
            max_execution_slippage_fraction=Decimal("1"),
            max_quote_age_seconds=Decimal("3600"),
            minimum_data_quality=Decimal("0"),
            max_concurrent_positions=10,
            max_parlay_legs=1,
        )
        EconomicGoalStore(self.workspace).initialize_owner(self.goal)
        PaperBook(Decimal("1000")).save(self.workspace / "paper_book.json")
        self.target = issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("1"),),
            contexts=(self._context(),),
        )

    def tearDown(self) -> None:
        self._env.stop()
        self._temp.cleanup()

    def _context(
        self,
        *,
        event_id: str = "event-1",
        market_id: str = "match_odds",
        selection_id: str = "home",
        exchange_side: str | None = None,
    ) -> ProposedTicketRiskContext:
        leg = TicketLeg(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            locked_odds=Decimal("2.3"),
            sport="table_tennis",
            exchange_side=exchange_side,
        )
        quote = MarketEvent(
            event_id=leg.event_id,
            market_id=leg.market_id,
            selection_id=leg.selection_id,
            decimal_odds=Decimal("2.3"),
            observed_ts=self.QUOTE_TS,
            source_id=self.SOURCE_ID,
            sequence=1,
            market_type=MarketType.WINNER,
            source_ts=self.QUOTE_TS,
            ingest_ts=self.QUOTE_TS,
            sport="table_tennis",
            exchange_side=exchange_side,
        )
        return ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(quote,),
            bankroll_id=self.goal.bankroll_id,
            currency=self.goal.currency,
            proposal_ts=self.DECISION_TS,
        )

    @staticmethod
    def _market_definition(
        selection_ids: tuple[str, ...] = ("away", "draw", "home"),
        *,
        event_id: str = "event-1",
    ) -> dict[str, object]:
        return {
            "eventId": event_id,
            "eventTypeId": "2593174",
            "marketType": "MATCH_ODDS",
            "status": "OPEN",
            "runners": [{"id": selection_id} for selection_id in selection_ids],
        }

    def _authority(
        self,
        selection_ids: tuple[str, ...] = ("away", "draw", "home"),
        *,
        event_id: str = "event-1",
        market_id: str = "match_odds",
        provider_publish_at: str = "2026-09-18T13:19:57+00:00",
        observed_at: str = "2026-09-18T13:19:58+00:00",
    ) -> MarketSettlementOutcomeAuthority:
        assessment = assess_betfair_historical_market_definition_authority(
            market_id=market_id,
            market_definition=self._market_definition(
                selection_ids,
                event_id=event_id,
            ),
            provider_publish_at=provider_publish_at,
            observed_at=observed_at,
        )
        self.assertEqual(
            assessment.status,
            OutcomeAuthorityStatus.PROVEN_EXHAUSTIVE,
        )
        self.assertIsNotNone(assessment.authority)
        return assessment.authority  # type: ignore[return-value]

    def test_issue_and_restart_reresolve_provider_verified_terminal_population(self) -> None:
        authority = self._authority()
        issued = issue_product_proposal_target_terminal_population(
            self.workspace,
            target_sha256=self.target.target_sha256,
            authorities=(authority,),
        )

        self.assertIsInstance(issued, ProductProposalTargetTerminalPopulation)
        self.assertTrue(issued.population_identity_proven)
        self.assertTrue(issued.provider_terminal_authority_proven)
        self.assertTrue(issued.terminal_space_exhaustive)
        self.assertEqual(issued.target_sha256, self.target.target_sha256)
        self.assertEqual(
            issued.candidate_vector_sha256,
            self.target.candidate_vector_sha256,
        )
        self.assertEqual(
            issued.market_authority_sha256s,
            (authority.authority_sha256,),
        )
        self.assertEqual(issued.terminal_market_count, 1)
        self.assertEqual(issued.terminal_state_count, 27)
        self.assertFalse(issued.terminal_space_exact)
        self.assertFalse(issued.probability_model_bound)
        self.assertFalse(issued.scientific_precommit_bound)
        self.assertFalse(issued.iid_member_mapping_proven)
        self.assertFalse(issued.proposal_target_counterfactual_execution_proven)
        self.assertFalse(issued.risk_upper_bound_for_target)
        self.assertFalse(issued.grants_ticket_authority)
        self.assertFalse(issued.grants_real_money_authority)

        resolved = resolve_product_proposal_target_terminal_population(
            self.workspace,
            target_sha256=self.target.target_sha256,
            authorities=(self._authority(),),
        )
        self.assertEqual(resolved, issued)
        self.assertTrue(resolved.population_identity_proven)

        ledger = JsonlDecisionLedger(self.workspace / "decisions.jsonl")
        matching = [
            record
            for record in ledger.verified_records()
            if record.action == "PROPOSAL_TARGET_TERMINAL_POPULATION_PRECOMMIT"
        ]
        self.assertEqual(len(matching), 1)
        payload = matching[0].payload
        self.assertNotIn("result", payload)
        self.assertNotIn("outcome", payload)
        self.assertFalse(payload["probability_model_bound"])
        self.assertFalse(payload["iid_member_mapping_proven"])
        self.assertFalse(contains_forbidden_future_key(payload))

    def test_public_constructor_cannot_mint_population_authority(self) -> None:
        with self.assertRaisesRegex(TypeError, "product-issued"):
            ProductProposalTargetTerminalPopulation()

    def test_missing_target_selection_in_verified_roster_fails_closed(self) -> None:
        authority = self._authority(("away", "draw"))
        with self.assertRaisesRegex(
            ProductProposalTargetTerminalPopulationError,
            "exactly one provider terminal authority",
        ):
            issue_product_proposal_target_terminal_population(
                self.workspace,
                target_sha256=self.target.target_sha256,
                authorities=(authority,),
            )

    def test_future_provider_evidence_fails_before_precommit(self) -> None:
        future = self._authority(
            provider_publish_at="2026-09-18T13:20:01+00:00",
            observed_at="2026-09-18T13:20:02+00:00",
        )
        with self.assertRaisesRegex(
            ProductProposalTargetTerminalPopulationError,
            "cannot be verified at proposal time",
        ):
            issue_product_proposal_target_terminal_population(
                self.workspace,
                target_sha256=self.target.target_sha256,
                authorities=(future,),
            )

    def test_unrelated_verified_authority_is_rejected(self) -> None:
        target_authority = self._authority()
        unrelated = self._authority(event_id="event-2")
        with self.assertRaisesRegex(
            ProductProposalTargetTerminalPopulationError,
            "unrelated to the proposal target",
        ):
            issue_product_proposal_target_terminal_population(
                self.workspace,
                target_sha256=self.target.target_sha256,
                authorities=(target_authority, unrelated),
            )

    def test_restart_requires_same_reverified_terminal_population(self) -> None:
        original = self._authority()
        issued = issue_product_proposal_target_terminal_population(
            self.workspace,
            target_sha256=self.target.target_sha256,
            authorities=(original,),
        )
        changed_verified_roster = self._authority(("away", "home"))
        with self.assertRaisesRegex(
            ProductProposalTargetTerminalPopulationError,
            "does not re-resolve",
        ):
            resolve_product_proposal_target_terminal_population(
                self.workspace,
                target_sha256=self.target.target_sha256,
                authorities=(changed_verified_roster,),
            )
        same = resolve_product_proposal_target_terminal_population(
            self.workspace,
            target_sha256=self.target.target_sha256,
            authorities=(self._authority(),),
        )
        self.assertEqual(same.population_sha256, issued.population_sha256)

    def test_multi_market_population_is_conservative_without_joint_state_authority(self) -> None:
        second_target = issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("1"), Decimal("0.8")),
            contexts=(
                self._context(),
                self._context(event_id="event-2", market_id="match_odds-2"),
            ),
        )
        population = issue_product_proposal_target_terminal_population(
            self.workspace,
            target_sha256=second_target.target_sha256,
            authorities=(
                self._authority(),
                self._authority(event_id="event-2", market_id="match_odds-2"),
            ),
        )
        self.assertEqual(population.terminal_market_count, 2)
        self.assertEqual(population.terminal_state_count, 27 * 27)
        self.assertTrue(population.terminal_space_exhaustive)
        self.assertFalse(population.terminal_space_exact)

    def test_exchange_side_target_remains_outside_terminal_authority(self) -> None:
        exchange_target = issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("1"),),
            contexts=(self._context(exchange_side="back"),),
        )
        with self.assertRaisesRegex(
            ProductProposalTargetTerminalPopulationError,
            "does not yet prove exchange-side semantics",
        ):
            issue_product_proposal_target_terminal_population(
                self.workspace,
                target_sha256=exchange_target.target_sha256,
                authorities=(self._authority(),),
            )

    def test_target_resolver_rebinding_is_rejected_before_dispatch(self) -> None:
        authority = self._authority()
        with patch.object(
            terminal_population_authority,
            "resolve_product_proposal_risk_target",
            lambda *_args, **_kwargs: self.target,
        ):
            with self.assertRaisesRegex(
                ProductProposalTargetTerminalPopulationError,
                "dispatch changed",
            ):
                issue_product_proposal_target_terminal_population(
                    self.workspace,
                    target_sha256=self.target.target_sha256,
                    authorities=(authority,),
                )

    def test_market_authority_method_rebinding_is_rejected(self) -> None:
        authority = self._authority()
        with patch.object(
            MarketSettlementOutcomeAuthority,
            "to_dict",
            lambda _self: {"authority_sha256": authority.authority_sha256},
        ):
            with self.assertRaisesRegex(
                ProductProposalTargetTerminalPopulationError,
                "dispatch changed",
            ):
                issue_product_proposal_target_terminal_population(
                    self.workspace,
                    target_sha256=self.target.target_sha256,
                    authorities=(authority,),
                )

    def test_duplicate_authority_identity_is_rejected(self) -> None:
        authority = self._authority()
        with self.assertRaisesRegex(
            ProductProposalTargetTerminalPopulationError,
            "duplicate authority digest",
        ):
            issue_product_proposal_target_terminal_population(
                self.workspace,
                target_sha256=self.target.target_sha256,
                authorities=(authority, authority),
            )

    def test_population_internal_helper_rebinding_fails_closed(self) -> None:
        authority = self._authority()
        original = terminal_population_authority._material

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        with patch.object(terminal_population_authority, "_material", fake):
            with self.assertRaisesRegex(
                ProductProposalTargetTerminalPopulationError,
                "dispatch changed",
            ):
                issue_product_proposal_target_terminal_population(
                    self.workspace,
                    target_sha256=self.target.target_sha256,
                    authorities=(authority,),
                )

    def test_population_result_class_rebinding_fails_closed(self) -> None:
        with patch.object(
            terminal_population_authority,
            "ProductProposalTargetTerminalPopulation",
            object,
        ):
            with self.assertRaisesRegex(
                ProductProposalTargetTerminalPopulationError,
                "dispatch changed",
            ):
                terminal_population_authority._require_dispatch()

    def test_population_authority_property_rebinding_fails_closed(self) -> None:
        forged = property(lambda _self: True)
        with patch.object(
            ProductProposalTargetTerminalPopulation,
            "risk_upper_bound_for_target",
            forged,
        ):
            with self.assertRaisesRegex(
                ProductProposalTargetTerminalPopulationError,
                "dispatch changed",
            ):
                terminal_population_authority._require_dispatch()

    def test_population_false_authority_getter_code_mutation_fails_closed(
        self,
    ) -> None:
        false_names = (
            "probability_model_bound",
            "scientific_precommit_bound",
            "iid_member_mapping_proven",
            "proposal_target_counterfactual_execution_proven",
            "risk_upper_bound_for_target",
            "grants_ticket_authority",
            "grants_real_money_authority",
        )

        def forged(_self: object) -> bool:
            return True

        for name in false_names:
            descriptor = ProductProposalTargetTerminalPopulation.__dict__[name]
            getter = descriptor.fget
            self.assertIsNotNone(getter)
            original_code = getter.__code__
            try:
                getter.__code__ = forged.__code__
                with self.assertRaisesRegex(
                    ProductProposalTargetTerminalPopulationError,
                    "dispatch changed",
                    msg=name,
                ):
                    terminal_population_authority._require_dispatch()
            finally:
                getter.__code__ = original_code

    def test_population_json_dump_root_and_code_recheck_after_dispatch(self) -> None:
        original = terminal_population_authority.json.dumps

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        terminal_population_authority._require_dispatch()
        with (
            patch.object(terminal_population_authority, "_JSON_DUMPS", fake),
            patch.object(terminal_population_authority.json, "dumps", fake),
        ):
            with self.assertRaisesRegex(
                ProductProposalTargetTerminalPopulationError,
                "dispatch changed",
            ):
                terminal_population_authority._canonical_json({"safe": True})

        serializer = terminal_population_authority.json.dumps
        original_code = serializer.__code__

        def forged(*args: object, **kwargs: object) -> str:
            return '{"forged":true}'

        terminal_population_authority._require_dispatch()
        try:
            serializer.__code__ = forged.__code__
            with self.assertRaisesRegex(
                ProductProposalTargetTerminalPopulationError,
                "dispatch changed",
            ):
                terminal_population_authority._canonical_json({"safe": True})
        finally:
            serializer.__code__ = original_code

    def test_population_json_load_root_and_code_recheck_after_dispatch(self) -> None:
        original = terminal_population_authority.json.loads

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        terminal_population_authority._require_dispatch()
        with (
            patch.object(terminal_population_authority, "_JSON_LOADS", fake),
            patch.object(terminal_population_authority.json, "loads", fake),
        ):
            with self.assertRaisesRegex(
                ProductProposalTargetTerminalPopulationError,
                "dispatch changed",
            ):
                terminal_population_authority._candidate_context("{}")

        parser = terminal_population_authority.json.loads
        original_code = parser.__code__

        def forged(*args: object, **kwargs: object) -> object:
            return {}

        terminal_population_authority._require_dispatch()
        try:
            parser.__code__ = forged.__code__
            with self.assertRaisesRegex(
                ProductProposalTargetTerminalPopulationError,
                "dispatch changed",
            ):
                terminal_population_authority._candidate_context("{}")
        finally:
            parser.__code__ = original_code

    def test_population_sha_root_rechecks_after_dispatch(self) -> None:
        original = terminal_population_authority.hashlib.sha256

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        terminal_population_authority._require_dispatch()
        with (
            patch.object(terminal_population_authority, "_HASHLIB_SHA256", fake),
            patch.object(terminal_population_authority.hashlib, "sha256", fake),
        ):
            with self.assertRaisesRegex(
                ProductProposalTargetTerminalPopulationError,
                "dispatch changed",
            ):
                terminal_population_authority._digest({"safe": True})


if __name__ == "__main__":
    unittest.main()
