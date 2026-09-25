from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)


QUOTE_AT = "2026-09-25T12:00:00+00:00"


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
    return runtime.prepare_paper_value_action(
        event=MarketEvent(
            event_id="event-1",
            market_id="winner",
            selection_id="home",
            decimal_odds=Decimal("2.00"),
            observed_ts=QUOTE_AT,
            source_id="paper-venue",
            sequence=1,
            source_ts=QUOTE_AT,
            ingest_ts=QUOTE_AT,
            sport="soccer",
        ),
        stake=Decimal("5.00"),
        decision_id="scope-decision",
        account_id="paper-account",
        bankroll_id="bankroll-eur",
        currency="EUR",
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

    def test_direct_mint_is_not_an_ambient_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _ledger, runtime = self._runtime(Path(tmp))
            canonical = _prepared(runtime)
            caller_authored = PreparedPaperExecution(
                execution_plan=canonical.execution_plan,
                exposure_bindings=canonical.exposure_bindings,
                intent_evidence_json=canonical.intent_evidence_json,
            )
            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "reserved for canonical preparation authority",
            ):
                runtime._mint_prepared(caller_authored)

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
                        "action_id": prepared.execution_plan.actions[0].action_id,
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
            run_id = runtime.expected_run_id(minted, "scope-trigger")
            unminted = PreparedPaperExecution(
                execution_plan=minted.execution_plan,
                exposure_bindings=minted.exposure_bindings,
                intent_evidence_json=minted.intent_evidence_json,
            )

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "was not minted by this runtime",
            ):
                runtime._publish_exposure_scope(prepared=unminted, run_id=run_id)


if __name__ == "__main__":
    unittest.main()
