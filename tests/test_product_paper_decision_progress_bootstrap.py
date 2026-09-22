from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from autosport.domain import TicketLeg
from autosport.live_decision_loop import _GATE_NORMAL, _PHASE_PENDING, _Progress
from autosport.paper import PaperBook
from autosport.product_paper_decision_cycle import (
    ProductPaperDecisionCycle,
    ProductPaperDecisionCycleError,
)


DECISION_TS = "2026-09-22T12:00:00+00:00"


def _cycle_for(workspace: Path) -> ProductPaperDecisionCycle:
    cycle = object.__new__(ProductPaperDecisionCycle)
    cycle.runtime = SimpleNamespace(
        workspace=workspace,
        manifest=SimpleNamespace(
            source_id="provider-a",
            initial_bankroll="100",
        ),
    )
    return cycle


def _write_pending_progress(workspace: Path) -> None:
    progress = _Progress(
        loop_id="product-paper-loop",
        phase=_PHASE_PENDING,
        decision_ts=DECISION_TS,
        market_state_sha256="1" * 64,
        decision_context_sha256="2" * 64,
        affected_input_ids=("selection-input",),
        registered_input_ids=("selection-input",),
        decision_id=None,
        plan_sha256=None,
        ledger_offset=None,
        gate=_GATE_NORMAL,
    )
    (workspace / "live_decision_progress.json").write_text(
        json.dumps(
            progress.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


class ProductPaperDecisionProgressBootstrapTests(unittest.TestCase):
    def test_pending_recovery_cursor_blocks_pristine_book_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            pre_action = PaperBook("100")
            pre_action.open_ticket(
                (
                    TicketLeg(
                        "event-1",
                        "market-1",
                        "selection-1",
                        Decimal("2.00"),
                    ),
                ),
                Decimal("10"),
                reason="surviving pre-action recovery witness",
                placed_at=DECISION_TS,
            )
            pre_action.save(workspace / "live_decision_pre_action_book.json")
            self.assertEqual(pre_action.balance, Decimal("90"))
            _write_pending_progress(workspace)

            cycle = _cycle_for(workspace)

            with self.assertRaises(
                ProductPaperDecisionCycleError,
                msg=(
                    "surviving live decision recovery authority must prevent "
                    "initial-bankroll bootstrap"
                ),
            ):
                cycle._load_current_book()

            self.assertFalse(
                (workspace / "paper_book.json").exists(),
                "recovery validation failure must not first publish a reset PaperBook",
            )

    def test_unverifiable_recovery_cursor_fails_before_book_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "live_decision_progress.json").write_text(
                "{not-valid-json",
                encoding="utf-8",
            )

            cycle = _cycle_for(workspace)

            with self.assertRaises(
                ProductPaperDecisionCycleError,
                msg="unverifiable recovery authority must fail closed",
            ):
                cycle._load_current_book()

            self.assertFalse(
                (workspace / "paper_book.json").exists(),
                "malformed recovery evidence must not be overwritten by bootstrap",
            )

    def test_truly_pristine_workspace_may_bootstrap_initial_book(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            cycle = _cycle_for(workspace)

            book = cycle._load_current_book()

            self.assertEqual(book.initial_bankroll, Decimal("100"))
            self.assertEqual(book.balance, Decimal("100"))
            self.assertTrue((workspace / "paper_book.json").exists())


if __name__ == "__main__":
    unittest.main()
