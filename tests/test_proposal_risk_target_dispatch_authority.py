from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import autosport.proposal_risk_target_authority as authority
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_store import EconomicGoalStore
from autosport.paper import PaperBook
from autosport.risk import ProposedTicketRiskContext


class ProposalRiskTargetDispatchAuthorityFalsifiers(unittest.TestCase):
    """Expected-red authority falsifiers stacked on canonical PR #2133.

    A product-issued proposal target is a durable precondition for the downstream
    proposal-specific ruin evaluation.  Rebinding Python dispatch after module
    import must therefore fail closed even when the replacement delegates to the
    original implementation and returns byte-for-byte equivalent values.  A
    delegating replacement is intentional: these tests isolate *dispatch identity*
    rather than depending on a forged economic payload.
    """

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
    def _goal() -> EconomicGoalContract:
        return EconomicGoalContract(
            goal_id="goal-proposal-target-dispatch",
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

    def _context(self, suffix: str) -> ProposedTicketRiskContext:
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
            observed_ts=self.QUOTE_TS,
            source_id=f"provider-{suffix}",
            sequence=1,
            source_ts=self.QUOTE_TS,
            ingest_ts=self.QUOTE_TS,
            metadata={},
            sport="soccer",
        )
        return ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(quote,),
            bankroll_id=self.goal.bankroll_id,
            currency=self.goal.currency,
            proposal_ts=self.DECISION_TS,
        )

    def _contexts(self) -> tuple[ProposedTicketRiskContext, ...]:
        return (self._context("a"), self._context("b"))

    def _issue(self):
        return authority.issue_product_proposal_risk_target(
            self.workspace,
            signal_strengths=(Decimal("1"), Decimal("0.8")),
            contexts=self._contexts(),
        )

    def test_issue_rejects_dispatch_guard_rebinding(self) -> None:
        # Current #2133 looks up _require_dispatch through writable module globals.
        with patch.object(authority, "_require_dispatch", lambda: None):
            with self.assertRaises(authority.ProductProposalRiskTargetError):
                self._issue()

    def test_issue_rejects_internal_derive_rebinding(self) -> None:
        original = authority._derive

        def rebound(policy, book, signals, contexts):
            return original(policy, book, signals, contexts)

        with patch.object(authority, "_derive", rebound):
            with self.assertRaises(authority.ProductProposalRiskTargetError):
                self._issue()

    def test_issue_rejects_decision_ledger_integrity_dispatch_rebinding(self) -> None:
        original = authority.JsonlDecisionLedger.verify_integrity

        def rebound(ledger, *args, **kwargs):
            return original(ledger, *args, **kwargs)

        with patch.object(authority.JsonlDecisionLedger, "verify_integrity", rebound):
            with self.assertRaises(authority.ProductProposalRiskTargetError):
                self._issue()

    def test_issue_rejects_monotonic_prepare_dispatch_rebinding(self) -> None:
        original = authority.MonotonicWorkspaceAuthority.prepare

        def rebound(instance, *args, **kwargs):
            return original(instance, *args, **kwargs)

        with patch.object(authority.MonotonicWorkspaceAuthority, "prepare", rebound):
            with self.assertRaises(authority.ProductProposalRiskTargetError):
                self._issue()

    def test_issue_rejects_market_event_serialization_dispatch_rebinding(self) -> None:
        original = authority.MarketEvent.to_dict

        def rebound(instance, *args, **kwargs):
            return original(instance, *args, **kwargs)

        with patch.object(authority.MarketEvent, "to_dict", rebound):
            with self.assertRaises(authority.ProductProposalRiskTargetError):
                self._issue()

    def test_resolve_rejects_target_builder_rebinding(self) -> None:
        issued = self._issue()
        original = authority._build_target

        def rebound(*args, **kwargs):
            return original(*args, **kwargs)

        with patch.object(authority, "_build_target", rebound):
            with self.assertRaises(authority.ProductProposalRiskTargetError):
                authority.resolve_product_proposal_risk_target(
                    self.workspace,
                    issued.target_sha256,
                )

    def test_resolve_rejects_market_event_parser_dispatch_rebinding(self) -> None:
        issued = self._issue()
        descriptor = authority.MarketEvent.__dict__["from_dict"]
        if isinstance(descriptor, classmethod):
            original = descriptor.__func__

            def rebound(cls, *args, **kwargs):
                return original(cls, *args, **kwargs)

            replacement = classmethod(rebound)
        elif isinstance(descriptor, staticmethod):
            original = descriptor.__func__

            def rebound(*args, **kwargs):
                return original(*args, **kwargs)

            replacement = staticmethod(rebound)
        else:
            original = descriptor

            def rebound(*args, **kwargs):
                return original(*args, **kwargs)

            replacement = rebound

        with patch.object(authority.MarketEvent, "from_dict", replacement):
            with self.assertRaises(authority.ProductProposalRiskTargetError):
                authority.resolve_product_proposal_risk_target(
                    self.workspace,
                    issued.target_sha256,
                )


if __name__ == "__main__":
    unittest.main()
