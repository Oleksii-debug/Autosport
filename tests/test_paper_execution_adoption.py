from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from decimal import Decimal
from unittest.mock import patch
from pathlib import Path

import autosport._paper_execution_lay_adoption_guard as lay_guard
import autosport.paper_execution_adoption as adoption_module
from autosport.domain import MarketEvent
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T06:00:00+00:00"
STARTED_AT = "2026-09-20T06:00:00.100000+00:00"
EXPIRES_AT = "2026-09-20T06:01:00+00:00"


class _MutatingObservationMapping(Mapping):
    def __init__(self, key: str, value, prepared_value: PreparedPaperExecution) -> None:
        self.key = key
        self.value = value
        self.prepared_value = prepared_value
        self.mutations = 0

    def __iter__(self):
        if self.mutations == 0:
            self.mutations += 1
            object.__setattr__(
                self.prepared_value.exposure_bindings[0],
                "bankroll_id",
                "mutated-during-mapping",
            )
        return iter((self.key,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key):
        if key != self.key:
            raise KeyError(key)
        return self.value


class _HostilePaperBook(PaperBook):
    authority_reads = 0

    def __getattribute__(self, name):
        if name in {
            "initial_bankroll",
            "balance",
            "tickets",
            "_lifecycle",
            "_settlement_times",
        }:
            type(self).authority_reads += 1
        return super().__getattribute__(name)


class _HostileExchangeSide(str):
    comparisons = 0

    def __hash__(self) -> int:
        type(self).comparisons += 1
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)


class _HostileProtocolText(str):
    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__ne__(other)


def action(
    action_id: str,
    *,
    odds: str = "2.50",
    stake: str = "10.00",
    side: str = "BACK",
):
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id=f"event-{action_id}",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side=side,
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def prepared(
    runtime: PaperExecutionAdoptionRuntime,
    *actions: ExecutionAction,
) -> PreparedPaperExecution:
    return runtime._mint_prepared(
        PreparedPaperExecution(
            execution_plan=ExecutionPlan(
                plan_id="adoption-plan-1",
                bookmaker_profile_version="paper-profile-v1",
                decision_id="decision-1",
                approval_id="paper-only-no-real-money",
                created_at=QUOTE_AT,
                actions=tuple(actions),
            ),
            exposure_bindings=tuple(
                PaperExposureBinding(
                    action_id=item.action_id,
                    sport="soccer",
                    bankroll_id="paper-bankroll",
                    currency="EUR",
                )
                for item in actions
            ),
            intent_evidence_json='{"schema":"test-intent-evidence"}',
        )
    )


def config(**overrides) -> PaperExecutionModelConfig:
    values = {
        "model_id": "paper-reality",
        "model_version": "2",
        "evidence_grade": EvidenceGrade.SYNTHETIC,
        "evidence_source": "test-seeded-model",
        "seed": "fixed-seed",
        "max_quote_age_ms": 5_000,
        "min_delay_ms": 100,
        "max_delay_ms": 100,
        "rejected_bps": 0,
        "partial_bps": 0,
        "unknown_bps": 0,
        "partial_fill_bps": 5_000,
        "max_slippage_bps": 0,
    }
    values.update(overrides)
    return PaperExecutionModelConfig(**values)


def evidence(
    current: ExecutionAction,
    outcome: PaperAttemptOutcome,
    *,
    odds: str | None = None,
    stake: str | None = None,
    grade: EvidenceGrade = EvidenceGrade.CONFIGURED,
):
    return PaperExecutionEvidenceRecord(
        action_id=current.action_id,
        bookmaker_id=current.bookmaker_id,
        account_id=current.account_id,
        event_id=current.event_id,
        market_id=current.market_id,
        selection_id=current.selection_id,
        side=current.side,
        quote_id=current.quote_id,
        outcome=outcome,
        observed_at=STARTED_AT,
        evidence_grade=grade,
        evidence_source="fixture-observation",
        accepted_odds=odds,
        accepted_stake=stake,
    )


def market_event(exchange_side: str | None) -> MarketEvent:
    return MarketEvent(
        event_id="event-side",
        market_id="market-side",
        selection_id="selection-side",
        decimal_odds=Decimal("2.50"),
        observed_ts=QUOTE_AT,
        source_id="paper-venue",
        sequence=1,
        source_ts=QUOTE_AT,
        ingest_ts=QUOTE_AT,
        sport="soccer",
        exchange_side=exchange_side,
    )


class PaperExecutionAdoptionTests(unittest.TestCase):
    def runtime(self, tmp: str, *, model=None):
        book = PaperBook("100.00")
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        runtime = PaperExecutionAdoptionRuntime(
            book=book,
            ledger=ledger,
            config=model or config(),
            max_quote_age=__import__("datetime").timedelta(seconds=5),
            paper_book_path=Path(tmp) / "paper-book.json",
        )
        return book, ledger, runtime

    def test_lay_recovery_rejects_paperbook_subclass_before_state_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, runtime = self.runtime(tmp)
            current = action("recovery-book-subclass", side="LAY")
            current_prepared = prepared(runtime, current)
            hostile = _HostilePaperBook("100.00")
            _HostilePaperBook.authority_reads = 0

            with self.assertRaisesRegex(TypeError, "exact PaperBook"):
                runtime.assert_recoverable_book_state(
                    pre_action_book=hostile,
                    prepared=current_prepared,
                    trigger_id="recovery-book-subclass",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

            self.assertEqual(_HostilePaperBook.authority_reads, 0)

    def test_recovery_dispatch_rejects_mutated_action_side_without_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("recovery-hostile-side", side="BACK")
            current_prepared = prepared(runtime, current)
            _HostileExchangeSide.comparisons = 0
            object.__setattr__(
                current,
                "side",
                _HostileExchangeSide("LAY"),
            )

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "prepared action side|canonical ExecutionAction side authority",
            ):
                runtime.assert_recoverable_book_state(
                    pre_action_book=book,
                    prepared=current_prepared,
                    trigger_id="recovery-hostile-side",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

            self.assertEqual(_HostileExchangeSide.comparisons, 0)
            self.assertEqual(ledger.events(), [])
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_binding_mutation_after_execution_cannot_redirect_materialized_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("post-run-binding-mutation", side="BACK")
            current_prepared = prepared(runtime, current)
            binding = current_prepared.exposure_bindings[0]
            original_execute = adoption_module.execute_paper_plan

            def execute_then_mutate(**kwargs):
                run = original_execute(**kwargs)
                object.__setattr__(binding, "bankroll_id", "redirected-bankroll")
                return run

            with patch.object(
                adoption_module,
                "execute_paper_plan",
                execute_then_mutate,
            ):
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "authority changed after mint",
                ):
                    runtime.execute(
                        prepared=current_prepared,
                        trigger_id="post-run-binding-mutation",
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))
            run_events = [
                event
                for event in ledger.events()
                if event["event_type"] == "RUN_COMPLETED"
            ]
            self.assertEqual(len(run_events), 1)

    def test_decision_id_mutation_after_execution_cannot_rewrite_materialized_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("post-run-decision-mutation", side="BACK")
            current_prepared = prepared(runtime, current)
            original_execute = adoption_module.execute_paper_plan

            def execute_then_mutate(**kwargs):
                run = original_execute(**kwargs)
                object.__setattr__(
                    current_prepared.execution_plan,
                    "decision_id",
                    "redirected-decision",
                )
                return run

            with patch.object(
                adoption_module,
                "execute_paper_plan",
                execute_then_mutate,
            ):
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "authority changed after mint",
                ):
                    runtime.execute(
                        prepared=current_prepared,
                        trigger_id="post-run-decision-mutation",
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))
            run_events = [
                event
                for event in ledger.events()
                if event["event_type"] == "RUN_COMPLETED"
            ]
            self.assertEqual(len(run_events), 1)

    def test_preflight_decision_mutation_fails_before_live_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            current = action("preflight-decision-mutation", side="BACK")
            current_prepared = prepared(runtime, current)
            original_preflight = lay_guard._preflight_materialization_batch
            original_materialize = runtime._materialize_attempt
            materialize_calls = 0

            def preflight_then_mutate(*args, **kwargs):
                result = original_preflight(*args, **kwargs)
                object.__setattr__(
                    current_prepared.execution_plan,
                    "decision_id",
                    "redirected-after-preflight",
                )
                return result

            def count_materialize(*args, **kwargs):
                nonlocal materialize_calls
                materialize_calls += 1
                return original_materialize(*args, **kwargs)

            with patch.object(
                lay_guard,
                "_preflight_materialization_batch",
                preflight_then_mutate,
            ), patch.object(
                runtime,
                "_materialize_attempt",
                count_materialize,
            ):
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "authority changed after mint",
                ):
                    runtime.execute(
                        prepared=current_prepared,
                        trigger_id="preflight-decision-mutation",
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

            self.assertEqual(materialize_calls, 0)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_existing_ticket_with_forged_decision_provenance_is_not_restart_equivalent(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            current = action("forged-ticket-decision", side="BACK")
            current_prepared = prepared(runtime, current)

            first = runtime.execute(
                prepared=current_prepared,
                trigger_id="forged-ticket-decision",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            self.assertEqual(len(first.ticket_ids), 1)
            ticket = next(iter(book.tickets.values()))
            object.__setattr__(
                ticket,
                "strategy_reason",
                ticket.strategy_reason.replace(
                    "decision_id=decision-1",
                    "decision_id=forged-decision",
                ),
            )

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "existing PaperBook exposure conflicts with durable execution attempt",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="forged-ticket-decision",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

            self.assertEqual(len(book.tickets), 1)
            self.assertEqual(book.balance, Decimal("90.00"))

    def test_runtime_book_replacement_after_attempt_fails_before_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("runtime-book-replacement", side="BACK")
            current_prepared = prepared(runtime, current)
            replacement = PaperBook("100.00")
            original_execute = adoption_module.execute_paper_plan

            def execute_then_replace(**kwargs):
                run = original_execute(**kwargs)
                runtime.book = replacement
                return run

            with patch.object(
                adoption_module,
                "execute_paper_plan",
                execute_then_replace,
            ):
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "runtime authority object changed after construction",
                ):
                    runtime.execute(
                        prepared=current_prepared,
                        trigger_id="runtime-book-replacement",
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

            self.assertEqual(book.tickets, {})
            self.assertEqual(replacement.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))
            self.assertEqual(replacement.balance, Decimal("100.00"))

    def test_save_callback_cannot_redirect_durable_verification_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            current = action("save-path-redirection", side="BACK")
            current_prepared = prepared(runtime, current)
            original_save = PaperBook.save
            canonical_path = runtime.paper_book_path
            redirected_path = Path(tmp) / "redirected-paper-book.json"

            def save_then_redirect(target, path):
                result = original_save(target, path)
                redirected_path.write_bytes(Path(path).read_bytes())
                runtime.paper_book_path = redirected_path
                return result

            with patch.object(PaperBook, "save", save_then_redirect):
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "runtime configuration changed after construction",
                ):
                    runtime.execute(
                        prepared=current_prepared,
                        trigger_id="save-path-redirection",
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

            self.assertTrue(Path(canonical_path).exists())
            self.assertTrue(redirected_path.exists())
            self.assertEqual(len(book.tickets), 1)
            self.assertEqual(book.balance, Decimal("90.00"))

    def test_post_run_ticket_marker_mutation_cannot_redirect_restart_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            current = action("post-run-ticket-marker", side="BACK")
            current_prepared = prepared(runtime, current)
            original_execute = adoption_module.execute_paper_plan
            canonical_marker = PaperExecutionAdoptionRuntime._TICKET_MARKER

            def execute_then_mutate_protocol(**kwargs):
                run = original_execute(**kwargs)
                PaperExecutionAdoptionRuntime._TICKET_MARKER = "forged_attempt_id="
                return run

            try:
                with patch.object(
                    adoption_module,
                    "execute_paper_plan",
                    execute_then_mutate_protocol,
                ):
                    with self.assertRaisesRegex(
                        PaperExecutionAdoptionError,
                        "runtime configuration changed after construction",
                    ):
                        runtime.execute(
                            prepared=current_prepared,
                            trigger_id="post-run-ticket-marker",
                            started_at=STARTED_AT,
                            materialize_exposure=True,
                        )
            finally:
                PaperExecutionAdoptionRuntime._TICKET_MARKER = canonical_marker

            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_hostile_protocol_marker_is_rejected_without_comparison_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(
                runtime,
                action("hostile-protocol-marker", side="BACK"),
            )
            canonical_marker = PaperExecutionAdoptionRuntime._TICKET_MARKER
            _HostileProtocolText.comparisons = 0

            try:
                PaperExecutionAdoptionRuntime._TICKET_MARKER = _HostileProtocolText(
                    canonical_marker
                )
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "runtime configuration changed after construction",
                ):
                    runtime.execute(
                        prepared=current_prepared,
                        trigger_id="hostile-protocol-marker",
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )
            finally:
                PaperExecutionAdoptionRuntime._TICKET_MARKER = canonical_marker

            self.assertEqual(_HostileProtocolText.comparisons, 0)
            self.assertEqual(ledger.events(), [])
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_post_mint_binding_mutation_fails_before_durable_scope_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("binding-mutation", side="BACK")
            current_prepared = prepared(runtime, current)
            binding = current_prepared.exposure_bindings[0]
            object.__setattr__(binding, "bankroll_id", "other-bankroll")
            events_before = list(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "authority changed after mint",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="binding-mutation",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                )

            self.assertEqual(ledger.events(), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_post_mint_valid_action_mutation_fails_before_durable_scope_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("action-mutation", odds="2.50", stake="10.00", side="BACK")
            current_prepared = prepared(runtime, current)
            object.__setattr__(current, "requested_stake", Decimal("11.00"))
            events_before = list(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "authority changed after mint",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="action-mutation",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                )

            self.assertEqual(ledger.events(), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_post_init_config_mutation_fails_before_durable_scope_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("config-mutation", side="BACK"))
            object.__setattr__(runtime.config, "model_version", "mutated-model-version")
            events_before = list(ledger.events())

            with self.assertRaisesRegex(
                Exception,
                "execution config|canonical|changed",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="config-mutation",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                )

            self.assertEqual(ledger.events(), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_runtime_ledger_replacement_fails_before_any_redirected_durable_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("ledger-replacement", side="BACK"))
            redirected = PaperExecutionLedger(Path(tmp) / "redirected-paper-execution.jsonl")
            runtime.ledger = redirected
            original_events = list(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "runtime authority object changed",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="ledger-replacement",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                )

            self.assertEqual(ledger.events(), original_events)
            self.assertEqual(redirected.events(), [])
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_runtime_book_replacement_fails_before_execution_or_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("book-replacement", side="BACK"))
            runtime.book = PaperBook("100.00")
            events_before = list(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "runtime authority object changed",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="book-replacement",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

            self.assertEqual(ledger.events(), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_forged_runtime_witness_attribute_cannot_authorize_ledger_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("forged-runtime-witness", side="BACK"))
            redirected = PaperExecutionLedger(Path(tmp) / "forged-redirect.jsonl")
            runtime.ledger = redirected
            runtime._autosport_lay_runtime_authority_witness = {
                "book": runtime.book,
                "ledger": runtime.ledger,
                "config": runtime.config,
                "config_fingerprint": runtime.config.fingerprint,
                "paper_book_path": runtime.paper_book_path,
                "max_quote_age": runtime.max_quote_age,
                "execution_lock": runtime._execution_lock,
                "prepared_authorities": runtime._prepared_authorities,
            }
            original_events = list(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "runtime authority object changed",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="forged-runtime-witness",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                )

            self.assertEqual(ledger.events(), original_events)
            self.assertEqual(redirected.events(), [])
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_forged_prepared_witness_attribute_cannot_authorize_post_mint_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("forged-prepared-witness", odds="2.50", stake="10.00", side="BACK")
            current_prepared = prepared(runtime, current)
            object.__setattr__(current, "requested_stake", Decimal("11.00"))
            runtime._autosport_lay_prepared_authority_witnesses = {
                id(current_prepared): "forged-current-witness"
            }
            events_before = list(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "authority changed after mint",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="forged-prepared-witness",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                )

            self.assertEqual(ledger.events(), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_invalid_started_at_fails_before_durable_scope_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("invalid-started-at", side="BACK"))
            events_before = list(ledger.events())

            with self.assertRaisesRegex(Exception, "started_at|ISO-8601|canonical"):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="invalid-started-at",
                    started_at="not-a-timestamp",
                    materialize_exposure=False,
                )

            self.assertEqual(ledger.events(), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_observation_mapping_callback_cannot_mutate_minted_binding_before_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("mapping-callback-mutation", side="BACK")
            current_prepared = prepared(runtime, current)
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.50",
                stake="10.00",
                grade=EvidenceGrade.EMPIRICAL,
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)
            events_before = list(ledger.events())
            observations = _MutatingObservationMapping(
                current.action_id,
                registered.as_observation(),
                current_prepared,
            )

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "authority changed after mint",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="mapping-callback-mutation",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                    observations=observations,
                    evidence_registry=registry,
                )

            self.assertEqual(observations.mutations, 1)
            self.assertEqual(ledger.events(), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_adoption_uses_durable_observation_snapshot_after_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("adoption-observation-snapshot", side="BACK")
            current_prepared = prepared(runtime, current)
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.50",
                stake="10.00",
                grade=EvidenceGrade.EMPIRICAL,
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)
            observation = registered.as_observation()
            original_verify = lay_guard._reality._impl._verify_observation_authority

            def verify_then_mutate(*, action, observation: object, registry):
                record = original_verify(
                    action=action,
                    observation=observation,
                    registry=registry,
                )
                object.__setattr__(observation, "accepted_odds", Decimal("99.00"))
                object.__setattr__(observation, "accepted_stake", Decimal("1.00"))
                return record

            with patch.object(
                lay_guard._reality._impl,
                "_verify_observation_authority",
                verify_then_mutate,
            ):
                result = runtime.execute(
                    prepared=current_prepared,
                    trigger_id="adoption-observation-snapshot",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                    observations={current.action_id: observation},
                    evidence_registry=registry,
                )

            self.assertEqual(result.run.attempts[0].execution_odds, Decimal("2.50"))
            self.assertEqual(result.run.attempts[0].execution_stake, Decimal("10.00"))
            self.assertEqual(book.balance, Decimal("90.00"))
            self.assertEqual(len(book.tickets), 1)

    def test_unknown_observation_key_fails_before_durable_scope_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("known-action", side="BACK"))
            events_before = list(ledger.events())

            with self.assertRaisesRegex(
                Exception,
                "observation.*outside execution plan",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="unknown-observation",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                    observations={"foreign-action": object()},
                )

            self.assertEqual(ledger.events(), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_unknown_suspension_fails_before_durable_scope_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("known-suspension", side="BACK"))
            events_before = list(ledger.events())

            with self.assertRaisesRegex(
                Exception,
                "suspended_action_ids.*outside execution plan",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="unknown-suspension",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                    suspended_action_ids=frozenset({"foreign-action"}),
                )

            self.assertEqual(ledger.events(), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_post_mint_decimal_resource_bomb_fails_before_digest_or_durable_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("resource-bomb", odds="2.50", stake="10.00", side="BACK")
            current_prepared = prepared(runtime, current)
            object.__setattr__(current, "requested_odds", Decimal("1E+9000"))
            events_before = list(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "Decimal resource bounds",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="resource-bomb",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                )

            self.assertEqual(ledger.events(), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_cross_ledger_observation_fails_before_exposure_scope_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("cross-ledger-scope", side="BACK")
            current_prepared = prepared(runtime, current)
            evidence_ledger = PaperExecutionLedger(
                Path(tmp) / "other-paper-execution.jsonl"
            )
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.50",
                stake="10.00",
                grade=EvidenceGrade.EMPIRICAL,
            )
            registry = PaperExecutionEvidenceRegistry(evidence_ledger)
            registry.register(registered)
            runtime_events_before = list(ledger.events())

            with self.assertRaisesRegex(
                Exception,
                "bound to the exact runtime ledger",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="cross-ledger-scope",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                    observations={current.action_id: registered.as_observation()},
                    evidence_registry=registry,
                )

            self.assertEqual(ledger.events(), runtime_events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_observation_digest_mismatch_fails_before_exposure_scope_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("digest-mismatch-scope", side="BACK")
            current_prepared = prepared(runtime, current)
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.50",
                stake="10.00",
                grade=EvidenceGrade.EMPIRICAL,
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)
            observation = registered.as_observation()
            object.__setattr__(observation, "evidence_sha256", "0" * 64)
            events_before = list(ledger.events())

            with self.assertRaisesRegex(
                Exception,
                "digest mismatch",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="digest-mismatch-scope",
                    started_at=STARTED_AT,
                    materialize_exposure=False,
                    observations={current.action_id: observation},
                    evidence_registry=registry,
                )

            self.assertEqual(ledger.events(), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_paper_value_rejects_mutated_side_subclass_without_comparison_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            event = market_event("back")
            _HostileExchangeSide.comparisons = 0
            object.__setattr__(
                event,
                "exchange_side",
                _HostileExchangeSide("lay"),
            )
            events_before = len(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "exact canonical string",
            ):
                runtime.prepare_paper_value_action(
                    event=event,
                    stake=Decimal("10.00"),
                    decision_id="decision-hostile-side",
                    account_id="paper-account",
                    bankroll_id="paper-bankroll",
                    currency="EUR",
                )

            self.assertEqual(_HostileExchangeSide.comparisons, 0)
            self.assertEqual(len(ledger.events()), events_before)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_paper_value_lay_prepares_canonical_lay_without_book_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            prepared_lay = runtime.prepare_paper_value_action(
                event=market_event("lay"),
                stake=Decimal("10.00"),
                decision_id="decision-lay",
                account_id="paper-account",
                bankroll_id="paper-bankroll",
                currency="EUR",
            )

            self.assertEqual(prepared_lay.execution_plan.actions[0].side, "LAY")
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))

    def test_paper_value_lay_executes_through_empirical_attempt_into_paperbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            prepared_lay = runtime.prepare_paper_value_action(
                event=market_event("lay"),
                stake=Decimal("10.00"),
                decision_id="decision-lay-e2e",
                account_id="paper-account",
                bankroll_id="paper-bankroll",
                currency="EUR",
            )
            current = prepared_lay.execution_plan.actions[0]
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds=str(current.requested_odds),
                stake=str(current.requested_stake),
                grade=EvidenceGrade.EMPIRICAL,
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)

            result = runtime.execute(
                prepared=prepared_lay,
                trigger_id="trigger-lay-e2e",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={current.action_id: registered.as_observation()},
                evidence_registry=registry,
            )

            self.assertEqual(result.run.attempts[0].side, "LAY")
            self.assertEqual(len(result.ticket_ids), 1)
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(ticket.legs[0].exchange_side, "lay")
            self.assertEqual(ticket.legs[0].locked_odds, Decimal("2.50"))
            self.assertEqual(ticket.stake, Decimal("10.00"))
            self.assertEqual(book.balance, Decimal("85.00"))
            self.assertEqual(book.committed_capital, Decimal("15.00"))

    def test_empirical_accepted_lay_materializes_liability_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("lay-adoption", odds="5.00", stake="10.00", side="LAY")
            current_prepared = prepared(runtime, current)
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
                grade=EvidenceGrade.EMPIRICAL,
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)

            first = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-lay-adoption",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={current.action_id: registered.as_observation()},
                evidence_registry=registry,
            )
            second = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-lay-adoption",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={current.action_id: registered.as_observation()},
                evidence_registry=registry,
            )

            self.assertEqual(first.run, second.run)
            self.assertEqual(first.ticket_ids, second.ticket_ids)
            self.assertEqual(len(book.tickets), 1)
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(ticket.legs[0].exchange_side, "lay")
            self.assertEqual(ticket.legs[0].locked_odds, Decimal("5.00"))
            self.assertEqual(ticket.stake, Decimal("10.00"))
            self.assertEqual(book.balance, Decimal("60.00"))
            self.assertEqual(book.committed_capital, Decimal("40.00"))

    def test_empirical_accepted_lay_liability_uses_accepted_odds_not_requested_odds(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action(
                "lay-accepted-odds-move",
                odds="5.00",
                stake="10.00",
                side="LAY",
            )
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="4.00",
                stake="10.00",
                grade=EvidenceGrade.EMPIRICAL,
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)

            result = runtime.execute(
                prepared=prepared(runtime, current),
                trigger_id="trigger-lay-accepted-odds-move",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={current.action_id: registered.as_observation()},
                evidence_registry=registry,
            )

            self.assertEqual(result.run.worst_case_exposure, Decimal("30.00"))
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(ticket.legs[0].locked_odds, Decimal("4.00"))
            self.assertEqual(ticket.stake, Decimal("10.00"))
            self.assertEqual(book.balance, Decimal("70.00"))
            self.assertEqual(book.committed_capital, Decimal("30.00"))

    def test_lay_recovery_revalidates_live_book_after_reconstruction_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            pre_action_book = PaperBook("100.00")
            current = action("lay-recovery-final-boundary", odds="5.00", stake="10.00", side="LAY")
            current_prepared = prepared(runtime, current)

            runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-lay-recovery-final-boundary",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            original_open_ticket = PaperBook.open_ticket

            def open_then_mutate(target, *args, **kwargs):
                result = original_open_ticket(target, *args, **kwargs)
                if target is not book:
                    book.balance = Decimal("59.00")
                return result

            with patch.object(PaperBook, "open_ticket", open_then_mutate):
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "changed or became invalid during reconstruction",
                ):
                    runtime.assert_recoverable_book_state(
                        pre_action_book=pre_action_book,
                        prepared=current_prepared,
                        trigger_id="trigger-lay-recovery-final-boundary",
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

            self.assertEqual(len(book.tickets), 1)
            self.assertEqual(book.balance, Decimal("59.00"))

    def test_lay_recovery_reproves_minted_authority_after_ledger_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            pre_action_book = PaperBook("100.00")
            current = action("lay-recovery-ledger-callback", odds="5.00", stake="10.00", side="LAY")
            current_prepared = prepared(runtime, current)

            runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-lay-recovery-ledger-callback",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            original_load_run = ledger.load_run
            binding = current_prepared.exposure_bindings[0]

            def load_then_mutate(*args, **kwargs):
                run = original_load_run(*args, **kwargs)
                object.__setattr__(binding, "bankroll_id", "mutated-after-ledger-read")
                return run

            with patch.object(ledger, "load_run", load_then_mutate):
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "authority changed after mint",
                ):
                    runtime.assert_recoverable_book_state(
                        pre_action_book=pre_action_book,
                        prepared=current_prepared,
                        trigger_id="trigger-lay-recovery-ledger-callback",
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

            self.assertEqual(len(book.tickets), 1)
            self.assertEqual(book.balance, Decimal("60.00"))

    def test_empirical_lay_recovery_replays_durable_observation_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            pre_action_book = PaperBook("100.00")
            current = action("lay-recovery-evidence", odds="5.00", stake="10.00", side="LAY")
            current_prepared = prepared(runtime, current)
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
                grade=EvidenceGrade.EMPIRICAL,
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)

            runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-lay-recovery-evidence",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={current.action_id: registered.as_observation()},
                evidence_registry=registry,
            )

            runtime.assert_recoverable_book_state(
                pre_action_book=pre_action_book,
                prepared=current_prepared,
                trigger_id="trigger-lay-recovery-evidence",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )

            self.assertEqual(book.balance, Decimal("60.00"))
            self.assertEqual(book.committed_capital, Decimal("40.00"))
            self.assertEqual(len(book.tickets), 1)

    def test_empirical_accepted_lay_restart_reuses_same_durable_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            book_path = Path(tmp) / "paper-book.json"
            current = action("lay-restart", odds="5.00", stake="10.00", side="LAY")
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
                grade=EvidenceGrade.EMPIRICAL,
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)
            first = runtime.execute(
                prepared=prepared(runtime, current),
                trigger_id="trigger-lay-restart",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={current.action_id: registered.as_observation()},
                evidence_registry=registry,
            )

            reloaded_book = PaperBook.load(book_path)
            restarted = PaperExecutionAdoptionRuntime(
                book=reloaded_book,
                ledger=ledger,
                config=runtime.config,
                max_quote_age=runtime.max_quote_age,
                paper_book_path=book_path,
            )
            second = restarted.execute(
                prepared=prepared(restarted, current),
                trigger_id="trigger-lay-restart",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={current.action_id: registered.as_observation()},
                evidence_registry=registry,
            )

            self.assertEqual(first.run, second.run)
            self.assertEqual(first.ticket_ids, second.ticket_ids)
            self.assertEqual(len(reloaded_book.tickets), 1)
            ticket = next(iter(reloaded_book.tickets.values()))
            self.assertEqual(ticket.legs[0].exchange_side, "lay")
            self.assertEqual(reloaded_book.balance, Decimal("60.00"))
            self.assertEqual(reloaded_book.committed_capital, Decimal("40.00"))

    def test_empirical_partial_lay_materializes_only_partial_liability(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("lay-partial", odds="5.00", stake="10.00", side="LAY")
            current_prepared = prepared(runtime, current)
            registered = evidence(
                current,
                PaperAttemptOutcome.PARTIAL,
                odds="4.50",
                stake="4.00",
                grade=EvidenceGrade.EMPIRICAL,
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)

            result = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-lay-partial",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={current.action_id: registered.as_observation()},
                evidence_registry=registry,
            )

            self.assertEqual(len(result.ticket_ids), 1)
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(ticket.legs[0].exchange_side, "lay")
            self.assertEqual(ticket.legs[0].locked_odds, Decimal("4.50"))
            self.assertEqual(ticket.stake, Decimal("4.00"))
            self.assertEqual(book.balance, Decimal("86.00"))
            self.assertEqual(book.committed_capital, Decimal("14.00"))

    def test_paper_value_back_and_legacy_side_remain_back_compatible(self):
        for exchange_side in ("back", None):
            with self.subTest(exchange_side=exchange_side), tempfile.TemporaryDirectory() as tmp:
                _book, _ledger, runtime = self.runtime(tmp)
                current = runtime.prepare_paper_value_action(
                    event=market_event(exchange_side),
                    stake=Decimal("10.00"),
                    decision_id=f"decision-{exchange_side or 'legacy'}",
                    account_id="paper-account",
                    bankroll_id="paper-bankroll",
                    currency="EUR",
                )
                self.assertEqual(current.execution_plan.actions[0].side, "BACK")

    def test_materializer_rejects_noncanonical_action_side_before_attempt_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            _book, _ledger, runtime = self.runtime(tmp)
            binding = PaperExposureBinding(
                action_id="bad-side-action",
                sport="soccer",
                bankroll_id="paper-bankroll",
                currency="EUR",
            )
            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "canonical BACK or LAY",
            ):
                runtime._materialize_attempt(
                    attempt=object(),
                    action=action("bad-side-action", side="SIDEWAYS"),
                    binding=binding,
                    decision_id="decision-bad-side",
                )

    def test_moved_accepted_quote_materializes_execution_truth_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("a1", odds="2.50", stake="10.00")
            current_prepared = prepared(runtime, current)
            registered = evidence(
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.25",
                stake="10.00",
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)

            first = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-1",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={"a1": registered.as_observation()},
                evidence_registry=registry,
            )
            second = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-1",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={"a1": registered.as_observation()},
                evidence_registry=registry,
            )

            self.assertEqual(first.run.run_id, second.run.run_id)
            self.assertEqual(first.ticket_ids, second.ticket_ids)
            self.assertEqual(len(book.tickets), 1)
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(str(ticket.stake), "10.00")
            self.assertEqual(str(ticket.legs[0].locked_odds), "2.25")
            self.assertEqual(ticket.placed_at, STARTED_AT)
            self.assertEqual(book.balance, __import__("decimal").Decimal("90.00"))

    def test_rejected_and_unknown_never_create_paper_exposure(self):
        for outcome, override in (
            (PaperAttemptOutcome.REJECTED, {"rejected_bps": 10_000}),
            (PaperAttemptOutcome.UNKNOWN, {"unknown_bps": 10_000}),
        ):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                book, _ledger, runtime = self.runtime(tmp, model=config(**override))
                current_prepared = prepared(runtime, action("a1"))
                first = runtime.execute(
                    prepared=current_prepared,
                    trigger_id=f"trigger-{outcome.value}",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )
                second = runtime.execute(
                    prepared=current_prepared,
                    trigger_id=f"trigger-{outcome.value}",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )
                self.assertEqual(first.run.run_id, second.run.run_id)
                self.assertEqual(first.run.attempts[0].outcome, outcome)
                self.assertEqual(first.ticket_ids, ())
                self.assertEqual(second.ticket_ids, ())
                self.assertEqual(book.tickets, {})

    def test_partial_materializes_only_exact_partial_stake(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current = action("a1", stake="10.00")
            registered = evidence(
                current,
                PaperAttemptOutcome.PARTIAL,
                odds="2.40",
                stake="4.00",
            )
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(registered)
            result = runtime.execute(
                prepared=prepared(runtime, current),
                trigger_id="trigger-partial",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={"a1": registered.as_observation()},
                evidence_registry=registry,
            )
            self.assertEqual(result.run.attempts[0].outcome, PaperAttemptOutcome.PARTIAL)
            self.assertEqual(len(book.tickets), 1)
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(str(ticket.stake), "4.00")
            self.assertEqual(str(ticket.legs[0].locked_odds), "2.40")
            self.assertEqual(book.balance, __import__("decimal").Decimal("96.00"))

    def test_attempt_before_ticket_restart_materializes_same_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            book_path = Path(tmp) / "paper-book.json"
            current_action = action("a1")
            current_prepared = prepared(runtime, current_action)
            shadow = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-crash-window",
                started_at=STARTED_AT,
                materialize_exposure=False,
            )
            self.assertEqual(book.tickets, {})
            self.assertEqual(PaperBook.load(book_path).tickets, {})

            # Simulate a fresh process after the durable #623 attempt but before
            # any exposure was published. Restart must re-mint authority from the
            # same canonical action instead of reusing an in-process capability.
            reloaded_book = PaperBook.load(book_path)
            restarted = PaperExecutionAdoptionRuntime(
                book=reloaded_book,
                ledger=ledger,
                config=runtime.config,
                max_quote_age=runtime.max_quote_age,
                paper_book_path=book_path,
            )
            restarted_prepared = prepared(restarted, current_action)
            resumed = restarted.execute(
                prepared=restarted_prepared,
                trigger_id="trigger-crash-window",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            self.assertEqual(shadow.run, resumed.run)
            self.assertEqual(len(reloaded_book.tickets), 1)

            # Simulate a second fresh process after durable PaperBook publication
            # but before the caller could publish its own COMMITTED progress.
            committed_book = PaperBook.load(book_path)
            restarted_again = PaperExecutionAdoptionRuntime(
                book=committed_book,
                ledger=ledger,
                config=runtime.config,
                max_quote_age=runtime.max_quote_age,
                paper_book_path=book_path,
            )
            restarted_again_prepared = prepared(restarted_again, current_action)
            again = restarted_again.execute(
                prepared=restarted_again_prepared,
                trigger_id="trigger-crash-window",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            self.assertEqual(resumed.ticket_ids, again.ticket_ids)
            self.assertEqual(len(committed_book.tickets), 1)
            self.assertEqual(PaperBook.load(book_path).tickets, committed_book.tickets)

    def test_single_live_materialization_corruption_rolls_back_economic_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            current = action("single-live-corruption", stake="10.00")
            current_prepared = prepared(runtime, current)
            original_open_ticket = PaperBook.open_ticket

            def open_then_corrupt_live_balance(target, *args, **kwargs):
                ticket = original_open_ticket(target, *args, **kwargs)
                if target is book:
                    target.balance = Decimal("89.00")
                return ticket

            with patch.object(
                PaperBook,
                "open_ticket",
                open_then_corrupt_live_balance,
            ):
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "invalid after batch materialization",
                ):
                    runtime.execute(
                        prepared=current_prepared,
                        trigger_id="trigger-single-live-corruption",
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))
            self.assertEqual(book.committed_capital, Decimal("0"))

    def test_live_batch_runtime_book_swap_restores_pinned_economic_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            first = action("batch-book-swap-a1", stake="10.00")
            second = action("batch-book-swap-a2", stake="10.00")
            current_prepared = prepared(runtime, first, second)
            original_open_ticket = PaperBook.open_ticket
            replacement = PaperBook("999.00")
            live_calls = 0

            def open_then_swap_runtime_book(target, *args, **kwargs):
                nonlocal live_calls
                ticket = original_open_ticket(target, *args, **kwargs)
                if target is book:
                    live_calls += 1
                    if live_calls == 1:
                        runtime.book = replacement
                return ticket

            with patch.object(
                PaperBook,
                "open_ticket",
                open_then_swap_runtime_book,
            ):
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "runtime authority object changed",
                ):
                    runtime.execute(
                        prepared=current_prepared,
                        trigger_id="trigger-batch-book-swap",
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

            self.assertEqual(live_calls, 1)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))
            self.assertEqual(book.committed_capital, Decimal("0"))

    def test_live_batch_callback_mutation_rolls_back_prior_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            first = action("batch-atomic-a1", stake="10.00")
            second = action("batch-atomic-a2", stake="10.00")
            current_prepared = prepared(runtime, first, second)
            second_binding = current_prepared.exposure_bindings[1]
            original_open_ticket = PaperBook.open_ticket
            live_calls = 0

            def open_then_mutate_later_authority(target, *args, **kwargs):
                nonlocal live_calls
                ticket = original_open_ticket(target, *args, **kwargs)
                if target is book:
                    live_calls += 1
                    if live_calls == 1:
                        object.__setattr__(
                            second_binding,
                            "bankroll_id",
                            "mutated-after-first-live-open",
                        )
                return ticket

            with patch.object(
                PaperBook,
                "open_ticket",
                open_then_mutate_later_authority,
            ):
                with self.assertRaisesRegex(
                    PaperExecutionAdoptionError,
                    "authority changed after mint",
                ):
                    runtime.execute(
                        prepared=current_prepared,
                        trigger_id="trigger-batch-atomic-callback",
                        started_at=STARTED_AT,
                        materialize_exposure=True,
                    )

            self.assertEqual(live_calls, 1)
            self.assertEqual(book.tickets, {})
            self.assertEqual(book.balance, Decimal("100.00"))
            self.assertEqual(book.committed_capital, Decimal("0"))

    def test_second_action_rejection_keeps_only_first_accepted_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            a1, a2 = action("a1"), action("a2")
            e1 = evidence(
                a1,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.45",
                stake="10.00",
            )
            e2 = evidence(a2, PaperAttemptOutcome.REJECTED)
            registry = PaperExecutionEvidenceRegistry(ledger)
            registry.register(e1)
            registry.register(e2)
            result = runtime.execute(
                prepared=prepared(runtime, a1, a2),
                trigger_id="trigger-second-reject",
                started_at=STARTED_AT,
                materialize_exposure=True,
                observations={
                    "a1": e1.as_observation(),
                    "a2": e2.as_observation(),
                },
                evidence_registry=registry,
            )
            self.assertEqual(
                [item.outcome for item in result.run.attempts],
                [PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.REJECTED],
            )
            self.assertEqual(len(book.tickets), 1)
            ticket = next(iter(book.tickets.values()))
            self.assertEqual(ticket.legs[0].event_id, a1.event_id)
            self.assertEqual(
                result.run.pending_action_ids,
                (),
            )

    def test_stale_quote_attempt_is_durable_rejection_without_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            result = runtime.execute(
                prepared=prepared(runtime, action("a1")),
                trigger_id="trigger-stale",
                started_at="2026-09-20T06:00:06+00:00",
                materialize_exposure=True,
            )
            self.assertEqual(
                result.run.attempts[0].outcome,
                PaperAttemptOutcome.REJECTED,
            )
            self.assertEqual(result.ticket_ids, ())
            self.assertEqual(book.tickets, {})

    def test_suspended_action_is_durable_rejection_without_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            result = runtime.execute(
                prepared=prepared(runtime, action("a1")),
                trigger_id="trigger-suspended",
                started_at=STARTED_AT,
                materialize_exposure=True,
                suspended_action_ids=frozenset({"a1"}),
            )
            self.assertEqual(
                result.run.attempts[0].outcome,
                PaperAttemptOutcome.REJECTED,
            )
            self.assertTrue(result.run.attempts[0].suspended)
            self.assertEqual(result.ticket_ids, ())
            self.assertEqual(book.tickets, {})

    def test_duplicate_attempt_marker_fails_closed_after_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            book_path = Path(tmp) / "paper-book.json"
            current_action = action("a1")
            current_prepared = prepared(runtime, current_action)
            first = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-marker-conflict",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            self.assertEqual(len(first.ticket_ids), 1)
            original = next(iter(book.tickets.values()))
            book.open_ticket(
                original.legs,
                original.stake,
                reason=original.strategy_reason,
                placed_at=original.placed_at,
                provider_source_ids=original.provider_source_ids,
                provider_accounts=original.provider_accounts,
                bankroll_id=original.bankroll_id,
                currency=original.currency,
            )
            book.save(book_path)

            reloaded = PaperBook.load(book_path)
            restarted = PaperExecutionAdoptionRuntime(
                book=reloaded,
                ledger=ledger,
                config=runtime.config,
                max_quote_age=runtime.max_quote_age,
                paper_book_path=book_path,
            )
            restarted_prepared = prepared(restarted, current_action)
            with self.assertRaisesRegex(
                Exception,
                "duplicate exposure",
            ):
                restarted.execute(
                    prepared=restarted_prepared,
                    trigger_id="trigger-marker-conflict",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

    def test_shadow_execution_keeps_attempt_evidence_without_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            result = runtime.execute(
                prepared=prepared(runtime, action("a1")),
                trigger_id="trigger-shadow",
                started_at=STARTED_AT,
                materialize_exposure=False,
            )
            self.assertTrue(result.run.completed)
            self.assertEqual(result.ticket_ids, ())
            self.assertEqual(book.tickets, {})


    def test_restart_ticket_matching_rejects_mutated_ticket_before_hostile_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            current = action("restart-hostile-ticket")
            current_prepared = prepared(runtime, current)
            first = runtime.execute(
                prepared=current_prepared,
                trigger_id="trigger-restart-hostile-ticket",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )
            self.assertEqual(len(first.ticket_ids), 1)
            ticket = next(iter(book.tickets.values()))
            object.__setattr__(
                ticket,
                "bankroll_id",
                _HostileExchangeSide(ticket.bankroll_id),
            )
            _HostileExchangeSide.comparisons = 0

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "PaperBook state is invalid before execution materialization",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="trigger-restart-hostile-ticket",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

            self.assertEqual(_HostileExchangeSide.comparisons, 0)
            self.assertEqual(len(book.tickets), 1)


    def test_post_execution_callback_mutation_fails_before_hostile_action_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            current = action("post-exec-action")
            current_prepared = prepared(runtime, current)
            original_execute = adoption_module.execute_paper_plan

            def mutate_after_execution(**kwargs):
                run = original_execute(**kwargs)
                object.__setattr__(
                    current_prepared.execution_plan.actions[0],
                    "action_id",
                    _HostileExchangeSide("post-exec-action"),
                )
                _HostileExchangeSide.comparisons = 0
                return run

            with patch.object(
                adoption_module,
                "execute_paper_plan",
                side_effect=mutate_after_execution,
            ), self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "prepared action action_id must retain exact canonical text authority",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="trigger-post-exec-action",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

            self.assertEqual(_HostileExchangeSide.comparisons, 0)
            self.assertEqual(book.tickets, {})

    def test_post_execution_callback_binding_mutation_fails_before_hostile_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            current = action("post-exec-binding")
            current_prepared = prepared(runtime, current)
            original_execute = adoption_module.execute_paper_plan

            def mutate_after_execution(**kwargs):
                run = original_execute(**kwargs)
                object.__setattr__(
                    current_prepared.exposure_bindings[0],
                    "action_id",
                    _HostileExchangeSide("post-exec-binding"),
                )
                _HostileExchangeSide.comparisons = 0
                return run

            with patch.object(
                adoption_module,
                "execute_paper_plan",
                side_effect=mutate_after_execution,
            ), self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "prepared binding action_id must retain exact canonical text authority",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="trigger-post-exec-binding",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

            self.assertEqual(_HostileExchangeSide.comparisons, 0)
            self.assertEqual(book.tickets, {})

    def test_post_execution_runtime_redirect_fails_before_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, ledger, runtime = self.runtime(tmp)
            current_prepared = prepared(runtime, action("post-exec-ledger"))
            original_execute = adoption_module.execute_paper_plan

            def redirect_after_execution(**kwargs):
                run = original_execute(**kwargs)
                object.__setattr__(
                    runtime,
                    "ledger",
                    PaperExecutionLedger(Path(tmp) / "redirected-ledger.jsonl"),
                )
                return run

            with patch.object(
                adoption_module,
                "execute_paper_plan",
                side_effect=redirect_after_execution,
            ), self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "runtime authority object changed after construction",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="trigger-post-exec-ledger",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

            self.assertEqual(book.tickets, {})
            self.assertIsNot(runtime.ledger, ledger)


    def test_multi_accept_batch_insufficient_bankroll_is_atomic_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            a1 = action("atomic-a1", stake="60.00")
            a2 = action("atomic-a2", stake="60.00")
            current_prepared = prepared(runtime, a1, a2)

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "accepted PAPER batch cannot be materialized atomically",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="trigger-atomic-batch",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

            self.assertEqual(book.balance, Decimal("100.00"))
            self.assertEqual(book.committed_capital, Decimal("0"))
            self.assertEqual(book.tickets, {})
            self.assertFalse((Path(tmp) / "paper-book.json").exists())

    def test_multi_accept_lay_batch_preflights_aggregate_liability(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            # Each 20 @ 4.0 LAY consumes 60 liability. The second accepted fill
            # would exceed the 100 bankroll if live mutation happened first.
            a1 = action("atomic-lay-a1", odds="4.00", stake="20.00", side="LAY")
            a2 = action("atomic-lay-a2", odds="4.00", stake="20.00", side="LAY")
            current_prepared = prepared(runtime, a1, a2)

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "accepted PAPER batch cannot be materialized atomically",
            ):
                runtime.execute(
                    prepared=current_prepared,
                    trigger_id="trigger-atomic-lay-batch",
                    started_at=STARTED_AT,
                    materialize_exposure=True,
                )

            self.assertEqual(book.balance, Decimal("100.00"))
            self.assertEqual(book.committed_capital, Decimal("0"))
            self.assertEqual(book.tickets, {})
            self.assertFalse((Path(tmp) / "paper-book.json").exists())


    def test_multi_accept_batch_preflight_keeps_authorized_success_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ledger, runtime = self.runtime(tmp)
            a1 = action("atomic-ok-a1", stake="20.00")
            a2 = action("atomic-ok-a2", stake="20.00")

            result = runtime.execute(
                prepared=prepared(runtime, a1, a2),
                trigger_id="trigger-atomic-ok",
                started_at=STARTED_AT,
                materialize_exposure=True,
            )

            self.assertEqual(len(result.ticket_ids), 2)
            self.assertEqual(len(book.tickets), 2)
            self.assertEqual(book.balance, Decimal("60.00"))
            self.assertEqual(book.committed_capital, Decimal("40.00"))
            durable = PaperBook.load(Path(tmp) / "paper-book.json")
            self.assertEqual(durable.balance, Decimal("60.00"))
            self.assertEqual(len(durable.tickets), 2)


if __name__ == "__main__":
    unittest.main()