from __future__ import annotations

from contextlib import contextmanager
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import autosport.product_paper_decision_cycle as cycle_module
from autosport.decision_ledger import EconomicDecisionAuthority
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.live_decision_loop import LiveCycleStatus, LiveDecisionProgressError
from autosport.paper import PaperBook
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionModelConfig,
)
from autosport.product_paper_decision_cycle import (
    ProductDecisionInput,
    ProductPaperDecisionCycle,
    ProductPaperDecisionCycleError,
)
from autosport.risk import PaperRiskPolicy
from autosport.scientific_registry import ScientificRegistry, StrategyVersion
from autosport.continuous_session import SessionState
from autosport.storage import SQLiteMarketStore


START = datetime(2026, 9, 22, 5, 0, tzinfo=timezone.utc)
SOURCE_SHA = hashlib.sha256(b"context-redecision-test-source-v1").hexdigest()
ENV_SHA = hashlib.sha256(b"context-redecision-test-env-v1").hexdigest()
CONFIG_SHA = hashlib.sha256(b"context-redecision-test-config-v1").hexdigest()


class _Runtime:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.manifest = SimpleNamespace(
            source_id="provider-a",
            initial_bankroll="1000",
        )

    def status(self):
        return SimpleNamespace(state=SessionState.RUNNING)

    @contextmanager
    def decision_commit_fence(self):
        yield


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _EmptyIntentFactory:
    strategy_version_id = "context-redecision-strategy-v1"

    def __call__(self, _input_id, _snapshot):
        return ()


def _registry(workspace: Path) -> ScientificRegistry:
    registry = ScientificRegistry.initialize_pristine(
        workspace / "scientific_registry.json"
    )
    registry.append(
        StrategyVersion(
            strategy_version_id=_EmptyIntentFactory.strategy_version_id,
            canonical_strategy_id="context-redecision-strategy",
            source_sha256=SOURCE_SHA,
            environment_sha256=ENV_SHA,
            config_sha256=CONFIG_SHA,
            created_at=START.isoformat(),
        )
    )
    return registry


def _authority() -> EconomicDecisionAuthority:
    goal = EconomicGoalContract(
        goal_id="context-redecision-goal",
        revision=1,
        bankroll_id="context-redecision-bankroll",
        currency="EUR",
    )
    return EconomicDecisionAuthority(
        goal,
        PaperRiskPolicy(economic_goal=goal),
    )


def _execution_config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="context-redecision-paper-model",
        model_version="1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="context-redecision-test",
        seed="context-redecision-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=0,
        max_delay_ms=0,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _seed_market(workspace: Path) -> None:
    event = MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-a",
        decimal_odds=Decimal("2.00"),
        observed_ts=START.isoformat(),
        source_id="provider-a",
        sequence=1,
        status="open",
        source_ts=START.isoformat(),
        ingest_ts=START.isoformat(),
    )
    store = SQLiteMarketStore(workspace / "market.db")
    try:
        store.append(event)
    finally:
        store.close()


def _cycle(
    workspace: Path,
    registry: ScientificRegistry,
    clock: _Clock,
) -> ProductPaperDecisionCycle:
    return ProductPaperDecisionCycle(
        _Runtime(workspace),
        loop_id="product-paper-context-redecision",
        authority=_authority(),
        intent_factory=_EmptyIntentFactory(),
        scientific_registry=registry,
        execution_config=_execution_config(),
        max_quote_age=timedelta(seconds=5),
        inputs=(
            ProductDecisionInput(
                "input-a",
                source_ids=("provider-a",),
                selection_ids=("selection-a",),
            ),
        ),
        clock=clock,
    )


class ProductPaperDecisionContextRestartFalsifierTests(unittest.TestCase):
    def test_committed_restart_cannot_ignore_changed_paper_book_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            _seed_market(workspace)
            registry = _registry(workspace)
            clock = _Clock(START + timedelta(seconds=1))

            with patch.object(cycle_module, "AutonomousProductRuntime", _Runtime):
                first = _cycle(workspace, registry, clock)
                first_result = first._run_decision_cycle()

                self.assertEqual(first_result.status, LiveCycleStatus.DECIDED)
                book_path = workspace / "paper_book.json"
                before = PaperBook.load(book_path)
                before_context = _authority().risk_policy.risk_of_ruin_portfolio_sha256(
                    before
                )
                self.assertIsNotNone(before_context)

                changed = PaperBook.load(book_path)
                changed.open_ticket(
                    [
                        TicketLeg(
                            "event-1",
                            "market-1",
                            "selection-a",
                            Decimal("2.00"),
                        )
                    ],
                    Decimal("100"),
                    reason="valid durable context-change fixture",
                    placed_at=(START + timedelta(seconds=2)).isoformat(),
                )
                changed.save(book_path)
                changed_context = _authority().risk_policy.risk_of_ruin_portfolio_sha256(
                    changed
                )
                self.assertIsNotNone(changed_context)
                self.assertNotEqual(before_context, changed_context)

                clock.value = START + timedelta(seconds=3)
                resumed = _cycle(workspace, registry, clock)
                try:
                    resumed_result = resumed._run_decision_cycle()
                except (
                    ProductPaperDecisionCycleError,
                    LiveDecisionProgressError,
                ):
                    return

            self.assertNotEqual(
                resumed_result.status,
                LiveCycleStatus.NO_CHANGE,
                "a changed durable PAPER risk context must trigger re-decision "
                "or explicit fail-closed recovery; unchanged market bytes alone "
                "cannot authorize clean-restart NO_CHANGE",
            )


if __name__ == "__main__":
    unittest.main()
