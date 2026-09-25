from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import FunctionType

import autosport._paper_exposure_scope_provenance_guard as scope_guard
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


def _reachable_functions(root: FunctionType) -> tuple[FunctionType, ...]:
    """Enumerate callable capability references exposed by ordinary function metadata."""

    pending: list[object] = [root]
    seen: set[int] = set()
    found: list[FunctionType] = []
    while pending:
        value = pending.pop()
        if not isinstance(value, FunctionType) or id(value) in seen:
            continue
        seen.add(id(value))
        found.append(value)
        wrapped = getattr(value, "__wrapped__", None)
        if wrapped is not None:
            pending.append(wrapped)
        if value.__defaults__:
            pending.extend(value.__defaults__)
        if value.__kwdefaults__:
            pending.extend(value.__kwdefaults__.values())
        if value.__closure__:
            for cell in value.__closure__:
                try:
                    pending.append(cell.cell_contents)
                except ValueError:
                    pass
    return tuple(found)


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

    def test_guard_does_not_publish_original_bypass_callables(self) -> None:
        for owner, names in (
            (
                PaperExecutionLedger,
                ("_autosport_exposure_scope_original_append_event",),
            ),
            (
                PaperExecutionAdoptionRuntime,
                (
                    "_autosport_exposure_scope_original_publish",
                    "_autosport_exposure_scope_original_mint_prepared",
                    "_autosport_exposure_scope_original_prepare",
                    "_autosport_exposure_scope_original_prepare_paper_value_action",
                ),
            ),
        ):
            for name in names:
                self.assertFalse(hasattr(owner, name), name)
        for name in (
            "_ORIGINAL_LEDGER_APPEND",
            "_ORIGINAL_RUNTIME_PUBLISH",
            "_ORIGINAL_RUNTIME_MINT",
            "_ORIGINAL_RUNTIME_PREPARE",
            "_ORIGINAL_RUNTIME_PREPARE_PAPER_VALUE",
            "_MINT_AUTHORITY",
        ):
            self.assertFalse(hasattr(scope_guard, name), name)

    def test_function_metadata_exposes_no_generic_append_or_mint_bypass(self) -> None:
        roots = (
            PaperExecutionLedger._append_event,
            PaperExecutionAdoptionRuntime._mint_prepared,
            PaperExecutionAdoptionRuntime.prepare,
            PaperExecutionAdoptionRuntime.prepare_paper_value_action,
            PaperExecutionAdoptionRuntime._publish_exposure_scope,
        )
        reachable = {
            id(function): function
            for root in roots
            for function in _reachable_functions(root)
        }
        bypasses = sorted(
            function.__name__
            for function in reachable.values()
            if function.__name__ in {"_append_event", "_mint_prepared"}
        )
        self.assertEqual(bypasses, [])

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

    def test_guarded_generic_append_preserves_non_reserved_ledger_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            ledger._append_event(
                event_type="FOCUSED_NON_RESERVED_TEST",
                run_id="generic-run",
                key="generic-run:event",
                payload={"value": 1},
            )
            ledger._append_event(
                event_type="FOCUSED_NON_RESERVED_TEST",
                run_id="generic-run",
                key="generic-run:event",
                payload={"value": 1},
            )
            events = ledger.events("generic-run")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "FOCUSED_NON_RESERVED_TEST")
            self.assertEqual(events[0]["payload"], {"value": 1})

    def test_inherited_lower_append_cannot_bypass_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            append_owner = next(
                base
                for base in PaperExecutionLedger.__mro__[1:]
                if "_append_event" in base.__dict__
            )
            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "reserved for canonical adoption authority",
            ):
                append_owner._append_event(
                    ledger,
                    event_type="PAPER_EXPOSURE_SCOPE_BOUND",
                    run_id="forged-lower-run",
                    key="forged-lower-run:exposure-scope",
                    payload={"forged": True},
                )
            self.assertEqual(ledger.events(), ())

    def test_public_ledger_dispatch_rebind_fails_before_reserved_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)
            run_id = runtime.expected_run_id(prepared, "scope-trigger")
            guarded_append = PaperExecutionLedger._append_event

            def forged_append(*args: object, **kwargs: object) -> None:
                raise AssertionError("forged append must never be invoked")

            PaperExecutionLedger._append_event = forged_append  # type: ignore[method-assign]
            try:
                with self.assertRaisesRegex(
                    PaperExecutionIntegrityError,
                    "ledger dispatch was rebound",
                ):
                    runtime._publish_exposure_scope(prepared=prepared, run_id=run_id)
            finally:
                PaperExecutionLedger._append_event = guarded_append  # type: ignore[method-assign]
            self.assertEqual(ledger.events(), ())

    def test_lower_event_constructor_rebind_fails_before_reserved_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)
            run_id = runtime.expected_run_id(prepared, "scope-trigger")
            append_owner = next(
                base
                for base in PaperExecutionLedger.__mro__[1:]
                if "_append_event" in base.__dict__
            )
            original_descriptor = append_owner.__dict__["_event"]
            self.assertIsInstance(original_descriptor, staticmethod)
            forged_calls: list[str] = []

            def forged_event(**kwargs: object) -> dict[str, object]:
                forged_calls.append("event")
                changed = dict(kwargs)
                changed["payload"] = {"forged": True}
                return original_descriptor.__func__(**changed)

            append_owner._event = staticmethod(forged_event)
            try:
                with self.assertRaisesRegex(
                    PaperExecutionIntegrityError,
                    "event constructor dispatch was rebound",
                ):
                    runtime._publish_exposure_scope(prepared=prepared, run_id=run_id)
            finally:
                append_owner._event = original_descriptor

            self.assertEqual(forged_calls, [])
            self.assertEqual(ledger.events(), ())

    def test_payload_classmethod_rebind_fails_before_forged_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)
            run_id = runtime.expected_run_id(prepared, "scope-trigger")
            original_descriptor = PaperExecutionAdoptionRuntime.__dict__[
                "_exposure_scope_payload"
            ]
            forged_calls: list[str] = []

            def forged_scope(
                _cls: type[PaperExecutionAdoptionRuntime],
                _prepared: PreparedPaperExecution,
            ) -> dict[str, object]:
                forged_calls.append("scope")
                return {"forged": True}

            PaperExecutionAdoptionRuntime._exposure_scope_payload = classmethod(forged_scope)
            try:
                with self.assertRaisesRegex(
                    PaperExecutionIntegrityError,
                    "payload authority was rebound",
                ):
                    runtime._publish_exposure_scope(prepared=prepared, run_id=run_id)
            finally:
                PaperExecutionAdoptionRuntime._exposure_scope_payload = original_descriptor

            self.assertEqual(forged_calls, [])
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
