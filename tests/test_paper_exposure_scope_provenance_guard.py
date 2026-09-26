from __future__ import annotations

import hashlib
import importlib
import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import autosport._paper_execution_decision_origin_callsite_guard as decision_callsite
import autosport._paper_exposure_scope_provenance_guard as scope_guard
from autosport.agents import AgentContext
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionRuntime,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.paper_strategy import Forecast, PaperValueAgent
from autosport.risk import PaperRiskPolicy


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
        source_ts="2026-09-25T11:59:59+00:00",
        ingest_ts=QUOTE_AT,
        sport="soccer",
    )


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="scope-product-goal",
        revision=1,
        bankroll_id="bankroll-eur",
        currency="EUR",
        max_stake_fraction=Decimal("0.10"),
        max_capital_at_risk_fraction=Decimal("0.50"),
        max_risk_of_ruin=Decimal("1"),
        max_concurrent_positions=2,
        max_quote_age_seconds=Decimal("5"),
        minimum_data_quality=Decimal("0"),
    )


def _fixture(root: Path):
    event = _event()
    book = PaperBook("100.00")
    execution_ledger = PaperExecutionLedger(root / "paper-execution.jsonl")
    runtime = PaperExecutionAdoptionRuntime(
        book=book,
        ledger=execution_ledger,
        config=_config(),
        max_quote_age=timedelta(seconds=5),
        paper_book_path=root / "paper-book.json",
    )
    decision_ledger = JsonlDecisionLedger(root / "decisions.jsonl")
    context = AgentContext(
        book,
        latest_quotes={event.quote_key: event},
        replay_run_id="scope-product-run",
        decision_ledger=decision_ledger,
        paper_execution=runtime,
        paper_provider_accounts=((event.source_id, "paper-account"),),
    )
    agent = PaperValueAgent(
        {
            event.quote_key: Forecast(
                quote_key=event.quote_key,
                probability=Decimal("0.75"),
                model_id="scope-product-model",
                as_of_ts="2026-09-25T11:59:58+00:00",
            )
        },
        stake=Decimal("5.00"),
        minimum_expected_profit_per_unit=Decimal("0"),
        risk_policy=PaperRiskPolicy(economic_goal=_goal()),
    )
    return event, agent, context, runtime, decision_ledger


def _run_product(root: Path):
    event, agent, context, runtime, decision_ledger = _fixture(root)
    agent.on_market_event(event, context)
    return event, agent, context, runtime, decision_ledger


def _caller_prepared(runtime: PaperExecutionAdoptionRuntime) -> PreparedPaperExecution:
    descriptor = runtime.prepare_paper_value_action(
        event=_event(),
        stake=Decimal("5.00"),
        decision_id="caller-authored-scope",
        account_id="paper-account",
        bankroll_id="bankroll-eur",
        currency="EUR",
    )
    return PreparedPaperExecution(
        execution_plan=descriptor.execution_plan,
        exposure_bindings=descriptor.exposure_bindings,
        intent_evidence_json=descriptor.intent_evidence_json,
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

    def test_scope_guard_reuses_existing_execution_and_preparation_authorities(self) -> None:
        canonical = decision_callsite._execute_with_exact_product_callsite
        self.assertIs(scope_guard.bind_canonical_execute(canonical), canonical)
        self.assertFalse(hasattr(canonical, "__wrapped__"))
        self.assertFalse(
            hasattr(PaperExecutionAdoptionRuntime, "_autosport_exposure_scope_preparation_guard")
        )
        self.assertFalse(hasattr(scope_guard, "_install_preparation_guard"))

    def test_callsite_reload_preserves_installed_exposure_scope_binding(self) -> None:
        installed_execute = PaperExecutionAdoptionRuntime.execute
        installed_publisher = PaperExecutionAdoptionRuntime._publish_exposure_scope

        reloaded = importlib.reload(decision_callsite)

        self.assertIs(PaperExecutionAdoptionRuntime.execute, installed_execute)
        self.assertIs(reloaded._execute_with_exact_product_callsite, installed_execute)
        self.assertIs(
            PaperExecutionAdoptionRuntime._publish_exposure_scope,
            installed_publisher,
        )

    def test_guard_does_not_publish_original_bypass_callables(self) -> None:
        for owner, names in (
            (PaperExecutionLedger, ("_autosport_exposure_scope_original_append_event",)),
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

    def test_direct_mint_does_not_grant_scope_publication_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _caller_prepared(runtime)
            runtime._mint_prepared(prepared)
            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "reserved for canonical execution authority",
            ):
                runtime._publish_exposure_scope(
                    prepared=prepared,
                    run_id="caller-selected-run",
                )
            self.assertEqual(ledger.events(), ())

    def test_publisher_has_no_positive_frame_or_code_authority_cells(self) -> None:
        publisher = PaperExecutionAdoptionRuntime._publish_exposure_scope
        freevars = set(publisher.__code__.co_freevars)
        for forbidden in {
            "getframe",
            "sys_module",
            "canonical_execute_code",
            "baseline_execute",
            "unlocked_execute",
            "paper_value_execute",
        }:
            self.assertNotIn(forbidden, freevars)

    def test_direct_mint_does_not_enter_scope_authority_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, runtime = self._runtime(Path(tmp))
            prepared = _caller_prepared(runtime)
            runtime._mint_prepared(prepared)
            self.assertIs(runtime._prepared_authorities[id(prepared)], prepared)
            self.assertNotIn(id(prepared), runtime._exposure_scope_authorities)
            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "lacks canonical PAPER exposure-scope authority",
            ):
                runtime._require_exposure_scope_authority(prepared)
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

    def test_product_path_publishes_exact_reserved_scope_before_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event, agent, context, runtime, decision_ledger = _run_product(Path(tmp))
            records = decision_ledger.verified_records()
            self.assertEqual(len(records), 1)
            run_id = records[0].payload["execution_run_id"]
            self.assertIsInstance(run_id, str)
            events = runtime.ledger.events(run_id)
            event_types = [item["event_type"] for item in events]
            self.assertEqual(event_types[0], "PAPER_EXPOSURE_SCOPE_BOUND")
            self.assertIn("RUN_RESERVED", event_types)
            self.assertIn("ATTEMPT_RECORDED", event_types)
            self.assertLess(
                event_types.index("PAPER_EXPOSURE_SCOPE_BOUND"),
                event_types.index("RUN_RESERVED"),
            )
            scope = events[0]
            payload = scope["payload"]
            self.assertEqual(
                payload["schema"],
                "autosport.paper_execution.exposure_scope_binding",
            )
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["bindings"][0]["sport"], "soccer")
            self.assertEqual(payload["bindings"][0]["bankroll_id"], "bankroll-eur")
            self.assertEqual(payload["bindings"][0]["currency"], "EUR")
            body = dict(payload)
            binding_sha256 = body.pop("binding_sha256")
            encoded = json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            self.assertEqual(binding_sha256, hashlib.sha256(encoded).hexdigest())

            agent.on_market_event(event, context)
            scope_events = [
                item
                for item in runtime.ledger.events(run_id)
                if item["event_type"] == "PAPER_EXPOSURE_SCOPE_BOUND"
            ]
            self.assertEqual(len(scope_events), 1)

    def test_public_ledger_dispatch_rebind_fails_before_reserved_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event, agent, context, runtime, _decision_ledger = _fixture(Path(tmp))
            guarded_append = PaperExecutionLedger._append_event
            forged_calls: list[str] = []

            def forged_append(*args: object, **kwargs: object) -> None:
                forged_calls.append("append")

            PaperExecutionLedger._append_event = forged_append  # type: ignore[method-assign]
            try:
                with self.assertRaisesRegex(
                    PaperExecutionIntegrityError,
                    "ledger dispatch was rebound",
                ):
                    agent.on_market_event(event, context)
            finally:
                PaperExecutionLedger._append_event = guarded_append  # type: ignore[method-assign]
            self.assertEqual(forged_calls, [])
            self.assertEqual(runtime.ledger.events(), ())

    def test_lower_event_constructor_rebind_fails_before_reserved_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event, agent, context, runtime, _decision_ledger = _fixture(Path(tmp))
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
                    agent.on_market_event(event, context)
            finally:
                append_owner._event = original_descriptor
            self.assertEqual(forged_calls, [])
            self.assertEqual(runtime.ledger.events(), ())

    def test_payload_classmethod_rebind_fails_before_forged_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event, agent, context, runtime, _decision_ledger = _fixture(Path(tmp))
            original_descriptor = PaperExecutionAdoptionRuntime.__dict__[
                "_exposure_scope_payload"
            ]
            self.assertIsInstance(original_descriptor, classmethod)
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
                    agent.on_market_event(event, context)
            finally:
                PaperExecutionAdoptionRuntime._exposure_scope_payload = original_descriptor
            self.assertEqual(forged_calls, [])
            self.assertEqual(runtime.ledger.events(), ())

    def test_hidden_require_minted_code_rebinding_fails_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event, agent, context, runtime, _decision_ledger = _fixture(Path(tmp))
            hidden = PaperExecutionAdoptionRuntime._require_minted
            original_code = hidden.__code__

            def forged_require_minted(_self, _prepared):
                return None

            hidden.__code__ = forged_require_minted.__code__
            try:
                with self.assertRaisesRegex(
                    Exception,
                    "prepared-execution verification metadata were rebound",
                ):
                    agent.on_market_event(event, context)
            finally:
                hidden.__code__ = original_code
            self.assertEqual(runtime.ledger.events(), ())


if __name__ == "__main__":
    unittest.main()
