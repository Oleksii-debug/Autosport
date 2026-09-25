from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-25T12:00:00+00:00"
EXPIRES_AT = "2026-09-25T12:01:00+00:00"


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="exposure-scope-provenance-test",
        model_version="1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="focused-regression",
        seed="fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=0,
        max_delay_ms=0,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _prepared(runtime: PaperExecutionAdoptionRuntime) -> PreparedPaperExecution:
    action = ExecutionAction(
        action_id="scope-action",
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-1",
        market_id="winner",
        selection_id="home",
        side="BACK",
        requested_odds="2.00",
        requested_stake="5.00",
        quote_id="quote-1",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )
    plan = ExecutionPlan(
        plan_id="scope-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="scope-decision",
        approval_id="paper-only-no-real-money",
        created_at=QUOTE_AT,
        actions=(action,),
    )
    return runtime._mint_prepared(
        PreparedPaperExecution(
            execution_plan=plan,
            exposure_bindings=(
                PaperExposureBinding(
                    action_id=action.action_id,
                    sport="soccer",
                    bankroll_id="bankroll-eur",
                    currency="EUR",
                ),
            ),
            intent_evidence_json='{"schema":"focused-regression"}',
        )
    )


class PaperExposureScopeProvenanceGuardTests(unittest.TestCase):
    def _runtime(self, root: Path) -> tuple[PaperExecutionLedger, PaperExecutionAdoptionRuntime]:
        ledger = PaperExecutionLedger(root / "paper-execution.jsonl")
        runtime = PaperExecutionAdoptionRuntime(
            book=PaperBook("100.00"),
            ledger=ledger,
            config=_config(),
            max_quote_age=timedelta(seconds=5),
            paper_book_path=root / "paper-book.json",
        )
        return ledger, runtime

    def test_generic_append_cannot_mint_reserved_exposure_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "reserved for canonical adoption authority",
            ):
                ledger._append_event(
                    event_type="PAPER_EXPOSURE_SCOPE_BOUND",
                    run_id="forged-run",
                    key="forged-run:exposure-scope",
                    payload={"forged": True},
                )
            self.assertEqual(ledger.events(), ())

    def test_canonical_runtime_can_publish_reserved_scope_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)
            run_id = runtime.expected_run_id(prepared, "scope-trigger")

            runtime._publish_exposure_scope(prepared=prepared, run_id=run_id)
            runtime._publish_exposure_scope(prepared=prepared, run_id=run_id)

            events = ledger.events(run_id)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "PAPER_EXPOSURE_SCOPE_BOUND")
            self.assertEqual(events[0]["event_key"], f"{run_id}:exposure-scope")
            self.assertEqual(
                events[0]["payload"]["schema"],
                "autosport.paper_execution.exposure_scope_binding",
            )
            self.assertEqual(events[0]["payload"]["schema_version"], 1)
            self.assertEqual(
                events[0]["payload"]["bindings"],
                [
                    {
                        "action_id": "scope-action",
                        "sport": "soccer",
                        "bankroll_id": "bankroll-eur",
                        "currency": "EUR",
                    }
                ],
            )

    def test_unminted_prepared_cannot_use_canonical_scope_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _ledger, runtime = self._runtime(Path(tmp))
            minted = _prepared(runtime)
            unminted = PreparedPaperExecution(
                execution_plan=minted.execution_plan,
                exposure_bindings=minted.exposure_bindings,
                intent_evidence_json=minted.intent_evidence_json,
            )
            run_id = runtime.expected_run_id(unminted, "scope-trigger")

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "was not minted by this runtime",
            ):
                runtime._publish_exposure_scope(prepared=unminted, run_id=run_id)


if __name__ == "__main__":
    unittest.main()
