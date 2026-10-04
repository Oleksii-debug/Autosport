from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.paper import PaperBook
from autosport.proposal_risk_target_authority import (
    ProductProposalRiskTarget,
    ProductProposalRiskTargetError,
    issue_product_proposal_risk_target,
    resolve_product_proposal_risk_target,
)
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


class ProductProposalRiskTargetTests(unittest.TestCase):
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
        self.goal = self._goal()
        EconomicGoalStore(self.workspace).initialize_owner(self.goal)
        PaperBook(Decimal("1000")).save(self.workspace / "paper_book.json")

    def tearDown(self) -> None:
        self._env.stop()
        self._temp.cleanup()

    @staticmethod
    def _goal(**overrides: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "goal-proposal-target",
            "revision": 1,
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "max_stake_fraction": Decimal("0.10"),
            "max_session_loss_fraction": Decimal("1"),
            "max_day_loss_fraction": Decimal("1"),
            "max_drawdown_fraction": Decimal("1"),
            "max_capital_at_risk_fraction": Decimal("1"),
            "max_event_concentration_fraction": Decimal("1"),
            "max_market_concentration_fraction": Decimal("1"),
            "max_provider_concentration_fraction": Decimal("1"),
            "max_sport_concentration_fraction": Decimal("1"),
            "max_turnover_fraction": Decimal("1000"),
            "max_risk_of_ruin": Decimal("0.01"),
            "max_execution_slippage_fraction": Decimal("1"),
            "max_quote_age_seconds": Decimal("3600"),
            "minimum_data_quality": Decimal("0"),
            "max_concurrent_positions": 10,
            "max_parlay_legs": 1,
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @classmethod
    def _context(
        cls,
        goal: EconomicGoalContract,
        suffix: str,
        *,
        proposal_ts: str | None = None,
        scalar_ruin_bound: Decimal | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ProposedTicketRiskContext:
        leg = TicketLeg(
            event_id=f"event-{suffix}",
            market_id=f"market-{suffix}",
            selection_id=f"selection-{suffix}",
            locked_odds=Decimal("2"),
            sport="soccer",
        )
        quote = MarketEvent(
            event_id=leg.event_id,
            market_id=leg.market_id,
            selection_id=leg.selection_id,
            decimal_odds=Decimal("2"),
            observed_ts=cls.QUOTE_TS,
            source_id=f"provider-{suffix}",
            sequence=1,
            source_ts=cls.QUOTE_TS,
            ingest_ts=cls.QUOTE_TS,
            metadata=metadata or {},
            sport="soccer",
        )
        return ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(quote,),
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            proposal_ts=proposal_ts or cls.DECISION_TS,
            risk_of_ruin_upper_bound=scalar_ruin_bound,
        )

    def _contexts(self) -> tuple[ProposedTicketRiskContext, ...]:
        return (
            self._context(self.goal, "a"),
            self._context(self.goal, "b"),
        )

    def _issue(self) -> ProductProposalRiskTarget:
        return issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("1"), Decimal("0.8")),
            contexts=self._contexts(),
        )

    def test_direct_construction_is_not_authority(self) -> None:
        with self.assertRaisesRegex(TypeError, "product-issued"):
            ProductProposalRiskTarget()

    def test_product_issues_and_reresolves_exact_non_authorizing_target(self) -> None:
        issued = self._issue()

        self.assertTrue(issued.proposal_target_identity_proven)
        self.assertFalse(issued.proposal_target_counterfactual_execution_proven)
        self.assertFalse(issued.risk_upper_bound_for_target)
        self.assertFalse(issued.grants_ticket_authority)
        self.assertFalse(issued.grants_real_money_authority)
        self.assertEqual(issued.decision_ts, self.DECISION_TS)
        self.assertEqual(issued.bankroll_id, self.goal.bankroll_id)
        self.assertEqual(issued.currency, self.goal.currency)
        self.assertEqual(len(issued.candidate_sha256s), 2)
        self.assertEqual(len(issued.evaluated_stakes), 2)
        self.assertTrue(any(stake > 0 for stake in issued.evaluated_stakes))

        resolved = resolve_product_proposal_risk_target(
            self.workspace, issued.target_sha256
        )
        self.assertEqual(resolved, issued)

    def test_retry_is_idempotent_and_does_not_append_duplicate_target(self) -> None:
        first = self._issue()
        second = self._issue()

        self.assertEqual(second, first)
        records = JsonlDecisionLedger(
            self.workspace / "decisions.jsonl"
        ).verified_records()
        target_records = [
            record
            for record in records
            if record.action == "PROPOSAL_RISK_TARGET_PRECOMMIT"
        ]
        self.assertEqual(len(target_records), 1)
        self.assertEqual(
            target_records[0].payload["target_sha256"],
            first.target_sha256,
        )

    def test_ledger_precommit_contains_no_positive_ruin_result(self) -> None:
        issued = self._issue()
        record = JsonlDecisionLedger(
            self.workspace / "decisions.jsonl"
        ).verified_records()[0]

        self.assertEqual(record.payload["target_sha256"], issued.target_sha256)
        self.assertNotIn("upper_bound", record.payload)
        self.assertNotIn("risk_of_ruin_evidence", record.payload)
        self.assertIs(record.payload["risk_upper_bound_for_target"], False)
        self.assertIs(record.payload["grants_ticket_authority"], False)
        self.assertIs(record.payload["grants_real_money_authority"], False)

    def test_scalar_or_preexisting_ruin_evidence_cannot_enter_target_inputs(self) -> None:
        contexts = (
            self._context(
                self.goal,
                "a",
                scalar_ruin_bound=Decimal("0"),
            ),
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "exclude risk-of-ruin result evidence",
        ):
            issue_product_proposal_risk_target(
                self.workspace,
                signal_strengths=(Decimal("1"),),
                contexts=contexts,
            )

    def test_target_requires_positive_vector_from_existing_allocator(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "no positive canonical pre-risk stake vector",
        ):
            issue_product_proposal_risk_target(
                self.workspace,
                signal_strengths=(Decimal("0"), Decimal("-1")),
                contexts=self._contexts(),
            )

    def test_target_requires_one_exact_shared_proposal_timestamp(self) -> None:
        contexts = (
            self._context(self.goal, "a"),
            self._context(
                self.goal,
                "b",
                proposal_ts="2026-09-18T13:20:01+00:00",
            ),
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "one exact shared proposal timestamp",
        ):
            issue_product_proposal_risk_target(
                self.workspace,
                signal_strengths=(Decimal("1"), Decimal("0.8")),
                contexts=contexts,
            )

    def test_target_is_stale_after_paperbook_mutation(self) -> None:
        issued = self._issue()
        context = self._contexts()[0]
        book = PaperBook.load(self.workspace / "paper_book.json")
        book.open_ticket(
            context.legs,
            Decimal("1"),
            reason="post-target-mutation",
            placed_at=context.proposal_ts,
            provider_source_ids=tuple(sorted(context.source_ids)),
            bankroll_id=context.bankroll_id,
            currency=context.currency,
        )
        book.save(self.workspace / "paper_book.json")

        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "base portfolio is stale",
        ):
            resolve_product_proposal_risk_target(
                self.workspace, issued.target_sha256
            )

    def test_target_is_stale_after_owner_goal_tightens(self) -> None:
        issued = self._issue()
        successor = replace(
            self.goal,
            revision=2,
            max_risk_of_ruin=Decimal("0.005"),
        )
        EconomicGoalStore(self.workspace).persist_automatic_successor(successor)

        with self.assertRaises(ProductProposalRiskTargetError):
            resolve_product_proposal_risk_target(
                self.workspace, issued.target_sha256
            )

    def test_copied_workspace_cannot_reuse_original_machine_authority(self) -> None:
        issued = self._issue()
        copied = self.workspace.parent / "copied-workspace"
        shutil.copytree(self.workspace, copied)

        with self.assertRaises(ProductProposalRiskTargetError):
            resolve_product_proposal_risk_target(copied, issued.target_sha256)

    def test_deleted_ledger_record_cannot_be_bootstrapped_from_target_dto(self) -> None:
        issued = self._issue()
        (self.workspace / "decisions.jsonl").write_bytes(b"")

        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "missing from the canonical Decision Ledger",
        ):
            resolve_product_proposal_risk_target(
                self.workspace, issued.target_sha256
            )

    def test_future_result_metadata_cannot_cross_precommit_ledger_boundary(self) -> None:
        contexts = (
            self._context(
                self.goal,
                "a",
                metadata={"outcome": "future"},
            ),
        )
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "Decision Ledger append failed",
        ):
            issue_product_proposal_risk_target(
                self.workspace,
                signal_strengths=(Decimal("1"),),
                contexts=contexts,
            )

    def test_dispatch_rebinding_of_existing_allocator_fails_closed(self) -> None:
        original = PaperRiskPolicy.derive_goal_stake_vector

        def fake(*args: object, **kwargs: object) -> object:
            return original(*args, **kwargs)

        with patch.object(
            PaperRiskPolicy,
            "derive_goal_stake_vector",
            fake,
        ):
            with self.assertRaisesRegex(
                ProductProposalRiskTargetError,
                "dispatch authority changed",
            ):
                self._issue()

    def test_relative_workspace_path_is_not_product_authority(self) -> None:
        relative = Path(os.path.relpath(self.workspace, Path.cwd()))
        with self.assertRaisesRegex(
            ProductProposalRiskTargetError,
            "exact absolute pathlib.Path",
        ):
            issue_product_proposal_risk_target(
                relative,
                signal_strengths=(Decimal("1"), Decimal("0.8")),
                contexts=self._contexts(),
            )

    def test_signal_vector_is_bound_into_target_identity(self) -> None:
        first = self._issue()
        second = issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("0.9"), Decimal("0.8")),
            contexts=self._contexts(),
        )
        self.assertNotEqual(first.target_sha256, second.target_sha256)
        self.assertNotEqual(first.signal_strengths, second.signal_strengths)


if __name__ == "__main__":
    unittest.main()
