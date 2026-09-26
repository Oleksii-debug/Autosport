from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import FunctionType

import autosport._paper_exposure_scope_provenance_guard as scope_guard
import autosport._paper_value_execution_authority as value_authority
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


def _event() -> MarketEvent:
    return MarketEvent(
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
    )


def _prepared(runtime: PaperExecutionAdoptionRuntime) -> PreparedPaperExecution:
    return runtime.prepare_paper_value_action(
        event=_event(),
        stake=Decimal("5.00"),
        decision_id="scope-decision",
        account_id="paper-account",
        bankroll_id="bankroll-eur",
        currency="EUR",
    )


def _execute(
    runtime: PaperExecutionAdoptionRuntime,
    prepared: PreparedPaperExecution,
    *,
    trigger_id: str = "scope-trigger",
) -> object:
    return runtime.execute(
        prepared=prepared,
        trigger_id=trigger_id,
        started_at=QUOTE_AT,
        materialize_exposure=False,
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

    def test_hidden_original_prepare_cannot_mint_without_owned_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _ledger, runtime = self._runtime(Path(tmp))
            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "reserved for canonical preparation authority",
            ):
                value_authority._ORIGINAL_PREPARE_PAPER_VALUE_ACTION(
                    runtime,
                    event=_event(),
                    stake=Decimal("5.00"),
                    decision_id="scope-hidden-original",
                    account_id="paper-account",
                    bankroll_id="bankroll-eur",
                    currency="EUR",
                )

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

    def test_hidden_prepare_globals_mutation_cannot_mint_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _ledger, runtime = self._runtime(Path(tmp))
            guarded = PaperExecutionAdoptionRuntime.prepare_paper_value_action
            original = guarded.__wrapped__
            original_globals = original.__globals__
            canonical_digest = original_globals["_digest"]
            original_globals["_digest"] = lambda _value: "forged-digest"
            try:
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "preparation globals were rebound",
                ):
                    original(
                        runtime,
                        event=_event(),
                        stake=Decimal("5.00"),
                        decision_id="scope-decision",
                        account_id="paper-account",
                        bankroll_id="bankroll-eur",
                        currency="EUR",
                    )
            finally:
                original_globals["_digest"] = canonical_digest

    def test_hidden_prepare_code_rebinding_cannot_mint_prepared_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            canonical = _prepared(runtime)
            caller_authored = PreparedPaperExecution(
                execution_plan=canonical.execution_plan,
                exposure_bindings=canonical.exposure_bindings,
                intent_evidence_json=canonical.intent_evidence_json,
            )
            guarded = PaperExecutionAdoptionRuntime.prepare_paper_value_action
            original = guarded.__wrapped__
            original_code = original.__code__

            def forged_prepare(
                self,
                *,
                event,
                stake,
                decision_id,
                account_id,
                bankroll_id,
                currency,
            ):
                return self._mint_prepared(event)

            # Preserve the canonical FunctionType identity and globals dictionary
            # while replacing only executable metadata.  Before this regression,
            # the owned wrapper set mint_context after a globals-only check and
            # guarded_mint_prepared compared against the now-mutated live
            # original.__code__, allowing this forged caller to mint arbitrary
            # PreparedPaperExecution.
            original.__code__ = forged_prepare.__code__
            try:
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "preparation metadata were rebound",
                ):
                    guarded(
                        runtime,
                        event=caller_authored,
                        stake=Decimal("5.00"),
                        decision_id="forged-code-mint",
                        account_id="paper-account",
                        bankroll_id="bankroll-eur",
                        currency="EUR",
                    )
            finally:
                original.__code__ = original_code

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "was not minted by this runtime",
            ):
                _execute(runtime, caller_authored)
            self.assertEqual(ledger.events(), ())

    def test_hidden_scope_payload_globals_mutation_fails_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)
            descriptor = PaperExecutionAdoptionRuntime.__dict__["_exposure_scope_payload"]
            self.assertIsInstance(descriptor, classmethod)
            original_scope = descriptor.__func__
            original_globals = original_scope.__globals__
            canonical_digest = original_globals["_digest"]
            original_globals["_digest"] = lambda _value: "forged-binding-sha256"
            try:
                with self.assertRaisesRegex(
                    PaperExecutionIntegrityError,
                    "payload globals were rebound",
                ):
                    _execute(runtime, prepared)
            finally:
                original_globals["_digest"] = canonical_digest
            self.assertEqual(ledger.events(), ())

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

    def test_reserved_append_has_no_mutable_publisher_code_cell(self) -> None:
        guarded = PaperExecutionLedger._append_event
        cells = dict(
            zip(
                guarded.__code__.co_freevars,
                guarded.__closure__ or (),
                strict=True,
            )
        )
        self.assertNotIn("publisher_code", cells)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "reserved for canonical adoption authority",
            ):
                guarded(
                    ledger,
                    event_type="PAPER_EXPOSURE_SCOPE_BOUND",
                    run_id="forged-closure-run",
                    key="forged-closure-run:exposure-scope",
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

    def test_direct_unlocked_execution_cannot_publish_reserved_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "reserved for canonical execute authority",
            ):
                runtime._execute_unlocked(
                    prepared=prepared,
                    trigger_id="direct-unlocked-trigger",
                    started_at=QUOTE_AT,
                    materialize_exposure=False,
                )

            self.assertEqual(ledger.events(), ())

    def test_wrapped_execute_cannot_bypass_execution_authority_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)
            hidden_execute = PaperExecutionAdoptionRuntime.execute.__wrapped__

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "reserved for canonical execute authority",
            ):
                hidden_execute(
                    runtime,
                    prepared=prepared,
                    trigger_id="hidden-execute-trigger",
                    started_at=QUOTE_AT,
                    materialize_exposure=False,
                )

            self.assertEqual(ledger.events(), ())

    def test_wrapped_unlocked_execute_cannot_bypass_public_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)
            hidden_unlocked = PaperExecutionAdoptionRuntime._execute_unlocked.__wrapped__

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "reserved for canonical execution authority",
            ):
                hidden_unlocked(
                    runtime,
                    prepared=prepared,
                    trigger_id="hidden-unlocked-trigger",
                    started_at=QUOTE_AT,
                    materialize_exposure=False,
                )

            self.assertEqual(ledger.events(), ())

    def test_direct_scope_publisher_cannot_choose_reserved_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "reserved for canonical execution authority",
            ):
                runtime._publish_exposure_scope(
                    prepared=prepared,
                    run_id="caller-selected-run",
                )

            self.assertEqual(ledger.events(), ())

    def test_public_ledger_dispatch_rebind_fails_before_reserved_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)
            guarded_append = PaperExecutionLedger._append_event

            def forged_append(*args: object, **kwargs: object) -> None:
                raise AssertionError("forged append must never be invoked")

            PaperExecutionLedger._append_event = forged_append  # type: ignore[method-assign]
            try:
                with self.assertRaisesRegex(
                    PaperExecutionIntegrityError,
                    "ledger dispatch was rebound",
                ):
                    _execute(runtime, prepared)
            finally:
                PaperExecutionLedger._append_event = guarded_append  # type: ignore[method-assign]
            self.assertEqual(ledger.events(), ())

    def test_lower_event_constructor_rebind_fails_before_reserved_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)
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
                    _execute(runtime, prepared)
            finally:
                append_owner._event = original_descriptor

            self.assertEqual(forged_calls, [])
            self.assertEqual(ledger.events(), ())

    def test_payload_classmethod_rebind_fails_before_forged_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)
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
                    _execute(runtime, prepared)
            finally:
                PaperExecutionAdoptionRuntime._exposure_scope_payload = original_descriptor

            self.assertEqual(forged_calls, [])
            self.assertEqual(ledger.events(), ())

    def test_canonical_execute_publishes_reserved_scope_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _prepared(runtime)
            run_id = runtime.expected_run_id(prepared, "scope-trigger")

            _execute(runtime, prepared)
            _execute(runtime, prepared)

            events = ledger.events(run_id)
            scope_events = [
                event
                for event in events
                if event["event_type"] == "PAPER_EXPOSURE_SCOPE_BOUND"
            ]
            self.assertEqual(len(scope_events), 1)
            scope = scope_events[0]
            self.assertEqual(scope["event_key"], f"{run_id}:exposure-scope")
            self.assertEqual(
                scope["payload"]["schema"],
                "autosport.paper_execution.exposure_scope_binding",
            )
            self.assertEqual(scope["payload"]["schema_version"], 1)
            self.assertEqual(
                scope["payload"]["bindings"],
                [
                    {
                        "action_id": prepared.execution_plan.actions[0].action_id,
                        "sport": "soccer",
                        "bankroll_id": "bankroll-eur",
                        "currency": "EUR",
                    }
                ],
            )
            payload = scope["payload"]
            self.assertEqual(
                payload["intent_evidence_sha256"],
                hashlib.sha256(
                    prepared.intent_evidence_json.encode("utf-8")
                ).hexdigest(),
            )
            body = dict(payload)
            binding_sha256 = body.pop("binding_sha256")
            encoded = json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            self.assertEqual(
                binding_sha256,
                hashlib.sha256(encoded).hexdigest(),
            )

    def test_unminted_prepared_cannot_enter_canonical_execute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            minted = _prepared(runtime)
            unminted = PreparedPaperExecution(
                execution_plan=minted.execution_plan,
                exposure_bindings=minted.exposure_bindings,
                intent_evidence_json=minted.intent_evidence_json,
            )

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "was not minted by this runtime",
            ):
                _execute(runtime, unminted)
            self.assertEqual(ledger.events(), ())


if __name__ == "__main__":
    unittest.main()
