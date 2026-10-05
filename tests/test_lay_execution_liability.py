from __future__ import annotations

import tempfile
from collections.abc import Mapping
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

import autosport.paper as paper_module
from autosport.domain import TicketLeg, TicketStatus
from autosport.exchange_exposure import locked_capital_for_exchange_side
from autosport.paper import PaperBook
import autosport.paper_execution_reality as paper_reality
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperLegAttempt,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
    RecoveryDecision,
    execute_paper_plan,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T03:00:00+00:00"
STARTED_AT = "2026-09-20T03:00:00.100000+00:00"
EXPIRES_AT = "2026-09-20T03:01:00+00:00"


def _action(
    action_id: str = "lay-1",
    *,
    side: str = "LAY",
    odds: str = "5.00",
    stake: str = "10.00",
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-exchange",
        account_id="paper-account",
        event_id="event-1",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side=side,
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def _plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="lay-paper-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="decision-1",
        approval_id="paper-only",
        created_at=QUOTE_AT,
        actions=tuple(actions),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="lay-liability-1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="test-seeded-model",
        seed="fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _registered_observation(
    ledger: PaperExecutionLedger,
    source_action: ExecutionAction,
    outcome: PaperAttemptOutcome,
    *,
    odds: str | None = None,
    stake: str | None = None,
    grade: EvidenceGrade = EvidenceGrade.EMPIRICAL,
    suspended: bool = False,
):
    record = PaperExecutionEvidenceRecord(
        action_id=source_action.action_id,
        bookmaker_id=source_action.bookmaker_id,
        account_id=source_action.account_id,
        event_id=source_action.event_id,
        market_id=source_action.market_id,
        selection_id=source_action.selection_id,
        side=source_action.side,
        quote_id=source_action.quote_id,
        outcome=outcome,
        observed_at="2026-09-20T03:00:00.250000+00:00",
        evidence_grade=grade,
        evidence_source="captured-paper-observation-v1",
        accepted_odds=odds,
        accepted_stake=stake,
        suspended=suspended,
        reason=f"observed {outcome.value.lower()}",
    )
    registry = PaperExecutionEvidenceRegistry(ledger)
    registry.register(record)
    return record.as_observation(), registry


class ExchangeLockedCapitalTests(unittest.TestCase):
    def test_back_locks_stake_exactly(self):
        stake = Decimal("10.000")
        self.assertEqual(
            locked_capital_for_exchange_side(
                stake=stake,
                odds=Decimal("5.00"),
                exchange_side="BACK",
            ),
            stake,
        )

    def test_lay_locks_exact_liability_without_quantization(self):
        self.assertEqual(
            locked_capital_for_exchange_side(
                stake=Decimal("10.00"),
                odds=Decimal("5.00"),
                exchange_side="lay",
            ),
            Decimal("40.0000"),
        )

    def test_canonical_lay_liability_matrix(self):
        for odds, expected in (
            ("1.5", "5.0"),
            ("2", "10"),
            ("5", "40"),
        ):
            with self.subTest(odds=odds):
                self.assertEqual(
                    locked_capital_for_exchange_side(
                        stake=Decimal("10"),
                        odds=Decimal(odds),
                        exchange_side="LAY",
                    ),
                    Decimal(expected),
                )

    def test_invalid_or_inexact_inputs_fail_closed(self):
        with self.assertRaises(TypeError):
            locked_capital_for_exchange_side(
                stake=10.0,
                odds=Decimal("2"),
                exchange_side="LAY",
            )
        with self.assertRaises(ValueError):
            locked_capital_for_exchange_side(
                stake=Decimal("10"),
                odds=Decimal("2"),
                exchange_side="UNKNOWN",
            )
        with self.assertRaises(ValueError):
            locked_capital_for_exchange_side(
                stake=Decimal("10"),
                odds=Decimal("NaN"),
                exchange_side="LAY",
            )


class DurableEvidenceBindingTests(unittest.TestCase):
    @staticmethod
    def _reserved_empirical_attempt(tmp: str):
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        source_action = _action(
            "evidence-binding",
            side="BACK",
            odds="2.00",
            stake="10.00",
        )
        plan = _plan(source_action)
        config = _config()
        trigger_id = "durable-evidence-binding"
        run_id = paper_reality._impl._run_id(plan, trigger_id, config)
        record = PaperExecutionEvidenceRecord(
            action_id=source_action.action_id,
            bookmaker_id=source_action.bookmaker_id,
            account_id=source_action.account_id,
            event_id=source_action.event_id,
            market_id=source_action.market_id,
            selection_id=source_action.selection_id,
            side=source_action.side,
            quote_id=source_action.quote_id,
            outcome=PaperAttemptOutcome.ACCEPTED,
            observed_at=STARTED_AT,
            evidence_grade=EvidenceGrade.EMPIRICAL,
            evidence_source="captured-paper-observation-v1",
            accepted_odds="2.00",
            accepted_stake="10.00",
            reason="observed accepted",
        )
        registry = PaperExecutionEvidenceRegistry(ledger)
        registry.register(record)
        ledger.reserve_run(
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=STARTED_AT,
            observation_evidence_ids={source_action.action_id: record.evidence_id},
        )
        attempt = paper_reality._impl._observed_attempt(
            run_id=run_id,
            plan=plan,
            action=source_action,
            sequence=0,
            config=config,
            observation=record.as_observation(),
            started_at=STARTED_AT,
        )
        return ledger, attempt

    def test_durable_attempt_cannot_substitute_unreserved_evidence_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, attempt = self._reserved_empirical_attempt(tmp)
            before = len(ledger.events())
            object.__setattr__(attempt, "evidence_id", "forged-evidence-id")

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "evidence identity is not authorized",
            ):
                ledger.record_attempt(attempt)

            self.assertEqual(len(ledger.events()), before)

    def test_durable_attempt_cannot_change_execution_odds_from_registered_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, attempt = self._reserved_empirical_attempt(tmp)
            before = len(ledger.events())
            object.__setattr__(attempt, "execution_odds", Decimal("3.00"))

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "execution truth does not match durable observation evidence",
            ):
                ledger.record_attempt(attempt)

            self.assertEqual(len(ledger.events()), before)


class DurableSideIntegrityTests(unittest.TestCase):
    @staticmethod
    def _attempt(*, outcome: PaperAttemptOutcome, side: str) -> PaperLegAttempt:
        return PaperLegAttempt(
            attempt_id="attempt-1",
            run_id="run-1",
            plan_id="plan-1",
            action_id="action-1",
            sequence=0,
            bookmaker_id="paper-exchange",
            account_id="paper-account",
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            side=side,
            decision_quote_id="quote-1",
            decision_odds=Decimal("2.00"),
            requested_stake=Decimal("10.00"),
            decision_observed_at=QUOTE_AT,
            execution_observed_at=STARTED_AT,
            delay_ms=100,
            quote_age_ms=100,
            outcome=outcome,
            execution_odds=None,
            execution_stake=None,
            suspended=False,
            evidence_grade=EvidenceGrade.SYNTHETIC,
            evidence_source="test",
            evidence_id=None,
            evidence_sha256=None,
            model_fingerprint="model-1",
            reason="test",
        )

    def test_rejected_durable_attempt_does_not_normalize_side(self):
        attempt = self._attempt(
            outcome=PaperAttemptOutcome.REJECTED,
            side="BACK",
        )
        object.__setattr__(attempt, "side", " back ")
        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "noncanonical exchange side",
        ):
            paper_reality._derive_run_economics(("action-1",), (attempt,))

    def test_durable_attempt_rejects_numeric_decimal_token_laundering(self):
        attempt = self._attempt(
            outcome=PaperAttemptOutcome.REJECTED,
            side="BACK",
        )
        payload = attempt.to_dict()
        payload["decision_odds"] = 2

        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "canonical serialized decimal string",
        ):
            PaperLegAttempt.from_dict(payload)

    def test_registered_evidence_rejects_numeric_decimal_token_laundering(self):
        source_action = _action()
        record = PaperExecutionEvidenceRecord(
            action_id=source_action.action_id,
            bookmaker_id=source_action.bookmaker_id,
            account_id=source_action.account_id,
            event_id=source_action.event_id,
            market_id=source_action.market_id,
            selection_id=source_action.selection_id,
            side=source_action.side,
            quote_id=source_action.quote_id,
            outcome=PaperAttemptOutcome.ACCEPTED,
            observed_at=STARTED_AT,
            evidence_grade=EvidenceGrade.EMPIRICAL,
            evidence_source="captured-paper-observation-v1",
            accepted_odds="5.00",
            accepted_stake="10.00",
        )
        payload = record.to_dict()
        payload["accepted_stake"] = 10.0

        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "canonical serialized decimal string",
        ):
            PaperExecutionEvidenceRecord.from_dict(payload)

    def test_unknown_durable_attempt_does_not_normalize_lay_side(self):
        attempt = self._attempt(
            outcome=PaperAttemptOutcome.UNKNOWN,
            side="LAY",
        )
        object.__setattr__(attempt, "side", " lay ")
        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "noncanonical exchange side",
        ):
            paper_reality._derive_run_economics(("action-1",), (attempt,))


    def test_durable_unknown_back_rejects_oversized_requested_stake(self):
        attempt = self._attempt(
            outcome=PaperAttemptOutcome.UNKNOWN,
            side="BACK",
        )
        object.__setattr__(attempt, "requested_stake", Decimal("1E+9000"))

        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "canonical resource bounds",
        ):
            paper_reality._derive_run_economics(("action-1",), (attempt,))

    def test_durable_side_subclass_is_rejected_without_hash_or_equality_hooks(self):
        class HostileSide(str):
            calls = 0

            def __hash__(self) -> int:
                type(self).calls += 1
                return super().__hash__()

            def __eq__(self, other: object) -> bool:
                type(self).calls += 1
                return super().__eq__(other)

        attempt = self._attempt(
            outcome=PaperAttemptOutcome.UNKNOWN,
            side="BACK",
        )
        object.__setattr__(attempt, "side", HostileSide("BACK"))

        with self.assertRaisesRegex(
            PaperExecutionIntegrityError,
            "noncanonical exchange side",
        ):
            paper_reality._derive_run_economics(("action-1",), (attempt,))

        self.assertEqual(HostileSide.calls, 0)


class _OneShotObservationMapping(Mapping):
    def __init__(self, action_id: str, observation) -> None:
        self._action_id = action_id
        self._observation = observation
        self.reads = 0

    def __iter__(self):
        return iter((self._action_id,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key):
        if key != self._action_id:
            raise KeyError(key)
        self.reads += 1
        if self.reads > 1:
            raise AssertionError("observation mapping was read more than once")
        return self._observation


class _EvidenceRegistrySubclass(PaperExecutionEvidenceRegistry):
    pass


class LayExecutionLiabilityTests(unittest.TestCase):
    def test_stateful_observation_mapping_is_snapshotted_once_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            observations = _OneShotObservationMapping(
                source_action.action_id,
                observation,
            )

            run = execute_paper_plan(
                plan=_plan(source_action),
                trigger_id="one-shot-observations",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations=observations,
                evidence_registry=registry,
            )

            self.assertEqual(observations.reads, 1)
            self.assertTrue(run.completed)
            self.assertEqual(run.attempts[0].outcome, PaperAttemptOutcome.ACCEPTED)

    def test_verified_observation_is_replaced_by_durable_snapshot_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            original_verify = paper_reality._impl._verify_observation_authority

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
                paper_reality._impl,
                "_verify_observation_authority",
                verify_then_mutate,
            ):
                run = execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="durable-observation-snapshot",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: observation},
                    evidence_registry=registry,
                )

            self.assertTrue(run.completed)
            self.assertEqual(run.attempts[0].execution_odds, Decimal("5.00"))
            self.assertEqual(run.attempts[0].execution_stake, Decimal("10.00"))
            self.assertEqual(run.worst_case_exposure, Decimal("40.0000"))

    def test_evidence_registry_subclass_cannot_replace_durable_resolver_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            canonical_registry = PaperExecutionEvidenceRegistry(ledger)
            record = PaperExecutionEvidenceRecord(
                action_id=source_action.action_id,
                bookmaker_id=source_action.bookmaker_id,
                account_id=source_action.account_id,
                event_id=source_action.event_id,
                market_id=source_action.market_id,
                selection_id=source_action.selection_id,
                side=source_action.side,
                quote_id=source_action.quote_id,
                outcome=PaperAttemptOutcome.ACCEPTED,
                observed_at=STARTED_AT,
                evidence_grade=EvidenceGrade.EMPIRICAL,
                evidence_source="captured-paper-observation-v1",
                accepted_odds="5.00",
                accepted_stake="10.00",
            )
            canonical_registry.register(record)
            before = len(ledger.events())
            subclass_registry = _EvidenceRegistrySubclass(ledger)

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "exact durable evidence registry authority",
            ):
                execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="registry-subclass",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: record.as_observation()},
                    evidence_registry=subclass_registry,
                )

            self.assertEqual(len(ledger.events()), before)

    def test_registry_bypasses_instance_authority_and_ledger_method_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            registry = PaperExecutionEvidenceRegistry(ledger)
            source_action = _action()
            record = PaperExecutionEvidenceRecord(
                action_id=source_action.action_id,
                bookmaker_id=source_action.bookmaker_id,
                account_id=source_action.account_id,
                event_id=source_action.event_id,
                market_id=source_action.market_id,
                selection_id=source_action.selection_id,
                side=source_action.side,
                quote_id=source_action.quote_id,
                outcome=PaperAttemptOutcome.ACCEPTED,
                observed_at=STARTED_AT,
                evidence_grade=EvidenceGrade.EMPIRICAL,
                evidence_source="captured-paper-observation-v1",
                accepted_odds="5.00",
                accepted_stake="10.00",
            )
            hostile_calls = 0

            def forbidden(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("instance evidence authority override executed")

            registry._require_authority = forbidden
            ledger.register_observation_evidence = forbidden
            ledger.resolve_observation_evidence = forbidden
            try:
                evidence_id = registry.register(record)
                resolved = registry.resolve(evidence_id)
                authority = registry.authority_ledger
            finally:
                registry.__dict__.pop("_require_authority", None)
                ledger.__dict__.pop("register_observation_evidence", None)
                ledger.__dict__.pop("resolve_observation_evidence", None)

            self.assertEqual(hostile_calls, 0)
            self.assertIs(authority, ledger)
            self.assertEqual(resolved, record)

    def test_execute_observation_verification_bypasses_registry_instance_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            registry = PaperExecutionEvidenceRegistry(ledger)
            source_action = _action(
                "sealed-observation-dispatch",
                side="LAY",
                odds="5.00",
                stake="10.00",
            )
            record = PaperExecutionEvidenceRecord(
                action_id=source_action.action_id,
                bookmaker_id=source_action.bookmaker_id,
                account_id=source_action.account_id,
                event_id=source_action.event_id,
                market_id=source_action.market_id,
                selection_id=source_action.selection_id,
                side=source_action.side,
                quote_id=source_action.quote_id,
                outcome=PaperAttemptOutcome.ACCEPTED,
                observed_at=STARTED_AT,
                evidence_grade=EvidenceGrade.EMPIRICAL,
                evidence_source="captured-paper-observation-v1",
                accepted_odds="5.00",
                accepted_stake="10.00",
            )
            registry.register(record)
            observation = record.as_observation()
            hostile_calls = 0

            def forbidden(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("observation authority instance override executed")

            registry._require_authority = forbidden
            registry.resolve = forbidden
            ledger.resolve_observation_evidence = forbidden
            try:
                run = execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="sealed-observation-dispatch",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: observation},
                    evidence_registry=registry,
                )
            finally:
                registry.__dict__.pop("_require_authority", None)
                registry.__dict__.pop("resolve", None)
                ledger.__dict__.pop("resolve_observation_evidence", None)

            self.assertEqual(hostile_calls, 0)
            self.assertTrue(run.completed)
            self.assertEqual(run.attempts[0].outcome, PaperAttemptOutcome.ACCEPTED)
            self.assertEqual(run.worst_case_exposure, Decimal("40.0000"))

    def test_registry_bypasses_legacy_ledger_class_dispatch_after_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            registry = PaperExecutionEvidenceRegistry(ledger)
            source_action = _action("sealed-legacy-ledger-dispatch")
            record = PaperExecutionEvidenceRecord(
                action_id=source_action.action_id,
                bookmaker_id=source_action.bookmaker_id,
                account_id=source_action.account_id,
                event_id=source_action.event_id,
                market_id=source_action.market_id,
                selection_id=source_action.selection_id,
                side=source_action.side,
                quote_id=source_action.quote_id,
                outcome=PaperAttemptOutcome.ACCEPTED,
                observed_at=STARTED_AT,
                evidence_grade=EvidenceGrade.EMPIRICAL,
                evidence_source="captured-paper-observation-v1",
                accepted_odds="5.00",
                accepted_stake="10.00",
            )
            hostile_calls = 0

            def forbidden(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("legacy ledger class dispatch executed")

            with patch.object(
                paper_reality._impl.PaperExecutionLedger,
                "register_observation_evidence",
                forbidden,
            ), patch.object(
                paper_reality._impl.PaperExecutionLedger,
                "resolve_observation_evidence",
                forbidden,
            ):
                evidence_id = registry.register(record)
                resolved = registry.resolve(evidence_id)

            self.assertEqual(hostile_calls, 0)
            self.assertEqual(resolved, record)

    def test_execution_rejects_mutated_ledger_storage_identity_before_durable_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mutations = (
                ("path", root / "redirect.jsonl"),
                ("_lock_path", root / "redirect.lock"),
                ("_anchor_path", root / "redirect.anchor.json"),
                ("_lock", object()),
            )
            for index, (attribute, replacement) in enumerate(mutations):
                ledger = PaperExecutionLedger(root / f"canonical-{index}.jsonl")
                setattr(ledger, attribute, replacement)
                with self.subTest(attribute=attribute):
                    with self.assertRaisesRegex(
                        PaperExecutionStateError,
                        "storage authority changed after construction",
                    ):
                        execute_paper_plan(
                            plan=_plan(_action(f"storage-{index}", side="BACK")),
                            trigger_id=f"storage-{index}",
                            config=_config(),
                            ledger=ledger,
                            started_at=STARTED_AT,
                        )
                    if attribute == "path":
                        self.assertFalse(replacement.exists())

    def test_execution_bypasses_instance_storage_witness_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            hostile_calls = 0

            def forbidden(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("instance storage-witness override executed")

            ledger._require_storage_authority = forbidden
            try:
                run = execute_paper_plan(
                    plan=_plan(_action("sealed-storage-witness", side="BACK")),
                    trigger_id="sealed-storage-witness",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                )
            finally:
                ledger.__dict__.pop("_require_storage_authority", None)

            self.assertEqual(hostile_calls, 0)
            self.assertTrue(run.completed)

    def test_forged_path_durable_flag_cannot_skip_directory_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            execute_paper_plan(
                plan=_plan(_action("durability-seed", side="BACK")),
                trigger_id="durability-seed",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )
            ledger._path_durable = True
            sync_calls = 0
            original_sync = paper_reality._CANONICAL_LEDGER_SYNC_PARENT_DIRECTORY

            def counting_sync(current):
                nonlocal sync_calls
                sync_calls += 1
                return original_sync(current)

            with patch.object(
                paper_reality,
                "_CANONICAL_LEDGER_SYNC_PARENT_DIRECTORY",
                counting_sync,
            ):
                run = execute_paper_plan(
                    plan=_plan(_action("durability-followup", side="BACK")),
                    trigger_id="durability-followup",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                )

            self.assertTrue(run.completed)
            self.assertGreater(sync_calls, 0)

    def test_execute_bypasses_post_import_execution_helper_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            registry = PaperExecutionEvidenceRegistry(ledger)
            source_action = _action(
                "sealed-helper-graph",
                side="LAY",
                odds="5.00",
                stake="10.00",
            )
            record = PaperExecutionEvidenceRecord(
                action_id=source_action.action_id,
                bookmaker_id=source_action.bookmaker_id,
                account_id=source_action.account_id,
                event_id=source_action.event_id,
                market_id=source_action.market_id,
                selection_id=source_action.selection_id,
                side=source_action.side,
                quote_id=source_action.quote_id,
                outcome=PaperAttemptOutcome.ACCEPTED,
                observed_at=STARTED_AT,
                evidence_grade=EvidenceGrade.EMPIRICAL,
                evidence_source="captured-paper-observation-v1",
                accepted_odds="5.00",
                accepted_stake="10.00",
            )
            registry.register(record)
            hostile_calls = 0

            def forbidden(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("post-import execution helper replacement executed")

            targets = (
                (paper_reality, "_snapshot_execution_plan"),
                (paper_reality, "_snapshot_execution_config"),
                (paper_reality, "_validate_lay_execution_surface"),
                (paper_reality, "_verify_observation_authority"),
                (paper_reality, "_derive_run_economics"),
                (paper_reality, "_attempt_locked_capital"),
                (paper_reality, "locked_capital_for_exchange_side"),
                (paper_reality, "_validate_decimal_text_resource_bound"),
                (paper_reality, "Decimal"),
                (paper_reality._impl, "_run_id"),
                (paper_reality._impl, "_observed_attempt"),
                (paper_reality._impl, "_require_canonical_action_surface"),
                (paper_reality._impl, "_require_canonical_observation_surface"),
            )
            patches = [
                patch.object(owner, name, forbidden)
                for owner, name in targets
            ]
            for current_patch in patches:
                current_patch.start()
            try:
                run = execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="sealed-helper-graph",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: record.as_observation()},
                    evidence_registry=registry,
                )
            finally:
                for current_patch in reversed(patches):
                    current_patch.stop()

            self.assertEqual(hostile_calls, 0)
            self.assertTrue(run.completed)
            self.assertEqual(run.attempts[0].outcome, PaperAttemptOutcome.ACCEPTED)
            self.assertEqual(run.worst_case_exposure, Decimal("40.0000"))

    def test_registry_revalidates_mutated_decimal_before_durable_write(self):
        class HostileDecimal(Decimal):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            registry = PaperExecutionEvidenceRegistry(ledger)
            source_action = _action()
            record = PaperExecutionEvidenceRecord(
                action_id=source_action.action_id,
                bookmaker_id=source_action.bookmaker_id,
                account_id=source_action.account_id,
                event_id=source_action.event_id,
                market_id=source_action.market_id,
                selection_id=source_action.selection_id,
                side=source_action.side,
                quote_id=source_action.quote_id,
                outcome=PaperAttemptOutcome.ACCEPTED,
                observed_at=STARTED_AT,
                evidence_grade=EvidenceGrade.EMPIRICAL,
                evidence_source="captured-paper-observation-v1",
                accepted_odds="5.00",
                accepted_stake="10.00",
            )
            object.__setattr__(record, "accepted_odds", HostileDecimal("5.00"))

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "accepted_odds must retain exact Decimal authority",
            ):
                registry.register(record)

            self.assertEqual(ledger.events(), [])

    def test_attempt_write_revalidates_mutated_exponent_before_serialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            attempt = DurableSideIntegrityTests._attempt(
                outcome=PaperAttemptOutcome.REJECTED,
                side="BACK",
            )
            object.__setattr__(attempt, "requested_stake", Decimal("1E+9000"))

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "canonical decimal resource bounds",
            ):
                ledger.record_attempt(attempt)

            self.assertEqual(ledger.events(), [])

    def test_empirical_decimal_ingress_rejects_arbitrary_str_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            _HostileStake.calls = 0

            with self.assertRaisesRegex(ValueError, "exact Decimal"):
                _registered_observation(
                    ledger,
                    source_action,
                    PaperAttemptOutcome.ACCEPTED,
                    odds="5.00",
                    stake=_HostileStake(),  # type: ignore[arg-type]
                )

            self.assertEqual(_HostileStake.calls, 0)
            self.assertEqual(ledger.events(), [])

    def test_empirical_decimal_ingress_rejects_decimal_subclass(self):
        class HostileDecimal(Decimal):
            calls = 0

            def __str__(self) -> str:
                type(self).calls += 1
                return super().__str__()

        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            value = HostileDecimal("5.00")

            with self.assertRaisesRegex(ValueError, "exact Decimal"):
                _registered_observation(
                    ledger,
                    source_action,
                    PaperAttemptOutcome.ACCEPTED,
                    odds=value,  # type: ignore[arg-type]
                    stake="10.00",
                )

            self.assertEqual(HostileDecimal.calls, 0)
            self.assertEqual(ledger.events(), [])

    def test_observation_digest_subclass_fails_before_equality_hooks_or_reservation(self):
        class HostileDigest(str):
            calls = 0

            def __eq__(self, other: object) -> bool:
                type(self).calls += 1
                return super().__eq__(other)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            before = len(ledger.events())
            object.__setattr__(
                observation,
                "evidence_sha256",
                HostileDigest(observation.evidence_sha256),
            )

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "evidence_sha256 must retain exact canonical text authority",
            ):
                execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="hostile-observation-digest",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: observation},
                    evidence_registry=registry,
                )

            self.assertEqual(HostileDigest.calls, 0)
            self.assertEqual(len(ledger.events()), before)

    def test_action_identity_subclass_fails_before_equality_hooks_or_reservation(self):
        class HostileIdentity(str):
            calls = 0

            def __eq__(self, other: object) -> bool:
                type(self).calls += 1
                return super().__eq__(other)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            before = len(ledger.events())
            object.__setattr__(
                source_action,
                "bookmaker_id",
                HostileIdentity(source_action.bookmaker_id),
            )

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "bookmaker_id must retain exact canonical text authority",
            ):
                execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="hostile-action-identity",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: observation},
                    evidence_registry=registry,
                )

            self.assertEqual(HostileIdentity.calls, 0)
            self.assertEqual(len(ledger.events()), before)

    def test_empirical_decimal_ingress_rejects_oversized_exponent(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()

            with self.assertRaisesRegex(ValueError, "resource limit"):
                _registered_observation(
                    ledger,
                    source_action,
                    PaperAttemptOutcome.ACCEPTED,
                    odds=Decimal("1E+9000"),  # type: ignore[arg-type]
                    stake="10.00",
                )

            self.assertEqual(ledger.events(), [])

    def test_empirical_accepted_lay_uses_liability_and_survives_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action(odds="5.00", stake="10.00")
            current = _plan(source_action)
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            first = execute_paper_plan(
                plan=current,
                trigger_id="lay-accepted",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={source_action.action_id: observation},
                evidence_registry=registry,
            )
            event_count = len(ledger.events())
            resumed = execute_paper_plan(
                plan=current,
                trigger_id="lay-accepted",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={source_action.action_id: observation},
                evidence_registry=registry,
            )

            self.assertEqual(first.attempts[0].side, "LAY")
            self.assertEqual(first.attempts[0].execution_stake, Decimal("10.00"))
            self.assertEqual(first.attempts[0].execution_odds, Decimal("5.00"))
            self.assertEqual(first.worst_case_exposure, Decimal("40.0000"))
            self.assertEqual(first.recovery_decision, RecoveryDecision.NONE)
            self.assertEqual(resumed, first)
            self.assertEqual(len(ledger.events()), event_count)

    def test_empirical_partial_lay_uses_accepted_partial_liability(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action(odds="5.00", stake="10.00")
            current = _plan(source_action)
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.PARTIAL,
                odds="4.50",
                stake="4.00",
            )
            result = execute_paper_plan(
                plan=current,
                trigger_id="lay-partial",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={source_action.action_id: observation},
                evidence_registry=registry,
            )

            self.assertEqual(result.attempts[0].execution_stake, Decimal("4.00"))
            self.assertEqual(result.worst_case_exposure, Decimal("14.0000"))
            self.assertEqual(
                result.recovery_decision,
                RecoveryDecision.HEDGE_REVIEW_REQUIRED,
            )

    def test_synthetic_lay_fails_before_run_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "explicit empirical execution evidence",
            ):
                execute_paper_plan(
                    plan=_plan(_action()),
                    trigger_id="lay-synthetic",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                )
            self.assertEqual(ledger.events(), ())

    def test_configured_lay_and_unknown_lay_fail_before_run_reservation(self):
        for grade, outcome, odds, stake in (
            (EvidenceGrade.CONFIGURED, PaperAttemptOutcome.ACCEPTED, "5.00", "10.00"),
            (EvidenceGrade.EMPIRICAL, PaperAttemptOutcome.UNKNOWN, None, None),
        ):
            with self.subTest(grade=grade, outcome=outcome):
                with tempfile.TemporaryDirectory() as tmp:
                    ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
                    source_action = _action()
                    observation, registry = _registered_observation(
                        ledger,
                        source_action,
                        outcome,
                        odds=odds,
                        stake=stake,
                        grade=grade,
                    )
                    before = len(ledger.events())
                    with self.assertRaises(PaperExecutionStateError):
                        execute_paper_plan(
                            plan=_plan(source_action),
                            trigger_id=f"lay-{grade.value}-{outcome.value}",
                            config=_config(),
                            ledger=ledger,
                            started_at=STARTED_AT,
                            observations={source_action.action_id: observation},
                            evidence_registry=registry,
                        )
                    self.assertEqual(len(ledger.events()), before)

    def test_suspended_empirical_fill_fails_before_run_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action()
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
                suspended=True,
            )
            before = len(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "suspended observation cannot claim a fill",
            ):
                execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="lay-suspended-fill",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: observation},
                    evidence_registry=registry,
                )

            self.assertEqual(len(ledger.events()), before)

    def test_empirical_overfill_fails_before_run_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            source_action = _action(stake="10.00")
            observation, registry = _registered_observation(
                ledger,
                source_action,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="11.00",
            )
            before = len(ledger.events())

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "accepted stake exceeds requested stake",
            ):
                execute_paper_plan(
                    plan=_plan(source_action),
                    trigger_id="lay-overfill",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={source_action.action_id: observation},
                    evidence_registry=registry,
                )

            self.assertEqual(len(ledger.events()), before)

    def test_mixed_or_multileg_lay_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            lay = _action("lay")
            back = _action("back", side="BACK", odds="2.00", stake="5.00")
            observation, registry = _registered_observation(
                ledger,
                lay,
                PaperAttemptOutcome.ACCEPTED,
                odds="5.00",
                stake="10.00",
            )
            before = len(ledger.events())
            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "single-leg",
            ):
                execute_paper_plan(
                    plan=_plan(lay, back),
                    trigger_id="mixed-lay",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={lay.action_id: observation},
                    evidence_registry=registry,
                )
            self.assertEqual(len(ledger.events()), before)

    def test_back_regression_keeps_stake_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            result = execute_paper_plan(
                plan=_plan(_action("back", side="BACK", odds="5.00", stake="10.00")),
                trigger_id="back-regression",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )
            self.assertEqual(result.worst_case_exposure, Decimal("10.00"))


class _MutatingEmptyObservationMapping(Mapping):
    def __init__(self, plan, action, config) -> None:
        self._plan = plan
        self._action = action
        self._config = config
        self.mutations = 0

    def __iter__(self):
        if self.mutations == 0:
            self.mutations += 1
            object.__setattr__(self._plan, "plan_id", "mutated-caller-plan")
            object.__setattr__(self._action, "action_id", "mutated-caller-action")
            object.__setattr__(self._config, "model_id", "mutated-caller-config")
        return iter(())

    def __len__(self) -> int:
        return 0

    def __getitem__(self, key):
        raise KeyError(key)


class ExecutionInputSnapshotTests(unittest.TestCase):
    def test_observation_mapping_cannot_mutate_execution_authorities_after_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            action = _action("snapshot-action", side="BACK")
            plan = _plan(action)
            config = _config()
            expected_plan_id = plan.plan_id
            expected_action_id = action.action_id
            expected_model_fingerprint = config.fingerprint
            observations = _MutatingEmptyObservationMapping(plan, action, config)

            result = execute_paper_plan(
                plan=plan,
                trigger_id="snapshot-execution-trigger",
                config=config,
                ledger=ledger,
                started_at=STARTED_AT,
                observations=observations,
            )

            self.assertEqual(observations.mutations, 1)
            self.assertTrue(result.completed)
            self.assertEqual(result.plan_id, expected_plan_id)
            self.assertEqual(result.model_fingerprint, expected_model_fingerprint)
            self.assertEqual(result.attempts[0].action_id, expected_action_id)
            reservations = [
                event
                for event in ledger.events(result.run_id)
                if event["event_type"] == "RUN_RESERVED"
            ]
            self.assertEqual(len(reservations), 1)
            self.assertEqual(reservations[0]["payload"]["plan_id"], expected_plan_id)
            self.assertEqual(
                reservations[0]["payload"]["model_fingerprint"],
                expected_model_fingerprint,
            )


class ReservationIdentitySnapshotTests(unittest.TestCase):
    def test_reserve_run_does_not_reread_plan_or_config_after_identity_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            plan = _plan(_action("snapshot-reserve"))
            config = _config()
            trigger_id = "snapshot-reserve-trigger"
            run_id = paper_reality._impl._run_id(plan, trigger_id, config)
            expected_plan_id = plan.plan_id
            expected_plan_fingerprint = plan.fingerprint
            expected_model_fingerprint = config.fingerprint
            original = paper_reality._canonical_observation_evidence_ids

            def interleaving(mapping, *, action_ids):
                result = original(mapping, action_ids=action_ids)
                object.__setattr__(plan, "plan_id", "mutated-after-snapshot")
                object.__setattr__(config, "model_id", "mutated-after-snapshot")
                return result

            with patch.object(
                paper_reality,
                "_canonical_observation_evidence_ids",
                interleaving,
            ):
                ledger.reserve_run(
                    run_id=run_id,
                    trigger_id=trigger_id,
                    plan=plan,
                    config=config,
                    started_at=STARTED_AT,
                    observation_evidence_ids={},
                )

            reservations = [
                event
                for event in ledger.events(run_id)
                if event["event_type"] == "RUN_RESERVED"
            ]
            self.assertEqual(len(reservations), 1)
            payload = reservations[0]["payload"]
            self.assertEqual(payload["plan_id"], expected_plan_id)
            self.assertEqual(payload["plan_fingerprint"], expected_plan_fingerprint)
            self.assertEqual(payload["model_fingerprint"], expected_model_fingerprint)

    def test_load_run_does_not_reread_plan_or_config_after_identity_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            action = _action("snapshot-load")
            plan = _plan(action)
            config = _config()
            trigger_id = "snapshot-load-trigger"
            run_id = paper_reality._impl._run_id(plan, trigger_id, config)
            expected_plan_id = plan.plan_id
            expected_plan_fingerprint = plan.fingerprint
            expected_model_fingerprint = config.fingerprint
            ledger.reserve_run(
                run_id=run_id,
                trigger_id=trigger_id,
                plan=plan,
                config=config,
                started_at=STARTED_AT,
                observation_evidence_ids={},
            )

            load_plan = _plan(_action("snapshot-load"))
            load_config = _config()
            original = paper_reality._canonical_observation_evidence_ids

            def interleaving(mapping, *, action_ids):
                result = original(mapping, action_ids=action_ids)
                object.__setattr__(load_plan, "plan_id", "mutated-after-snapshot")
                object.__setattr__(load_config, "model_id", "mutated-after-snapshot")
                return result

            with patch.object(
                paper_reality,
                "_canonical_observation_evidence_ids",
                interleaving,
            ):
                loaded = ledger.load_run(
                    run_id=run_id,
                    trigger_id=trigger_id,
                    plan=load_plan,
                    config=load_config,
                    started_at=STARTED_AT,
                    observation_evidence_ids={},
                )

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.plan_id, expected_plan_id)
            self.assertEqual(loaded.plan_fingerprint, expected_plan_fingerprint)
            self.assertEqual(loaded.model_fingerprint, expected_model_fingerprint)


class _HostileStake:
    calls = 0

    def __str__(self) -> str:
        type(self).calls += 1
        raise AssertionError("hostile stake __str__ must not execute")


class _HostileLegIterable:
    calls = 0

    def __iter__(self):
        type(self).calls += 1
        raise AssertionError("hostile leg iterable must not execute")


class _HostileSettlementKey:
    calls = 0

    def __hash__(self) -> int:
        type(self).calls += 1
        return object.__hash__(self)


class _HostileComparableText(str):
    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)


class _HostileExchangeSide(str):
    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)


class PaperExecutionLedgerDispatchAuthorityTests(unittest.TestCase):
    def test_execute_bypasses_instance_state_machine_method_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = _action(
                "ledger-instance-dispatch",
                side="BACK",
                odds="2.00",
                stake="10.00",
            )
            hostile_calls = 0

            def forbidden(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("instance ledger state-machine override executed")

            ledger.reserve_run = forbidden
            ledger.load_run = forbidden
            ledger.record_attempt = forbidden
            ledger.complete_run = forbidden
            try:
                result = execute_paper_plan(
                    plan=_plan(current),
                    trigger_id="ledger-instance-dispatch",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                )
            finally:
                ledger.__dict__.pop("reserve_run", None)
                ledger.__dict__.pop("load_run", None)
                ledger.__dict__.pop("record_attempt", None)
                ledger.__dict__.pop("complete_run", None)

            self.assertEqual(hostile_calls, 0)
            self.assertTrue(result.completed)
            self.assertEqual(result.worst_case_exposure, Decimal("10.00"))
            self.assertEqual(result.attempts[0].outcome, PaperAttemptOutcome.ACCEPTED)


class PaperExecutionLedgerPrimitiveAuthorityTests(unittest.TestCase):
    def test_execute_bypasses_instance_events_and_append_event_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = _action(
                "ledger-primitive-dispatch",
                side="BACK",
                odds="2.00",
                stake="10.00",
            )
            hostile_calls = 0

            def forbidden(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("instance durable primitive override executed")

            ledger.events = forbidden
            ledger._append_event = forbidden
            try:
                result = execute_paper_plan(
                    plan=_plan(current),
                    trigger_id="ledger-primitive-dispatch",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                )
            finally:
                ledger.__dict__.pop("events", None)
                ledger.__dict__.pop("_append_event", None)

            self.assertEqual(hostile_calls, 0)
            self.assertTrue(result.completed)
            self.assertEqual(result.worst_case_exposure, Decimal("10.00"))

    def test_execute_bypasses_internal_durable_primitive_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = _action(
                "ledger-internal-primitive-dispatch",
                side="BACK",
                odds="2.00",
                stake="10.00",
            )
            hostile_calls = 0

            def forbidden(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("internal durable primitive override executed")

            ledger._event = forbidden
            ledger._sync_parent_directory = forbidden
            ledger._write_anchor_unlocked = forbidden
            try:
                result = execute_paper_plan(
                    plan=_plan(current),
                    trigger_id="ledger-internal-primitive-dispatch",
                    config=_config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                )
            finally:
                ledger.__dict__.pop("_event", None)
                ledger.__dict__.pop("_sync_parent_directory", None)
                ledger.__dict__.pop("_write_anchor_unlocked", None)

            self.assertEqual(hostile_calls, 0)
            self.assertTrue(result.completed)
            self.assertEqual(result.worst_case_exposure, Decimal("10.00"))
            self.assertEqual(result.attempts[0].outcome, PaperAttemptOutcome.ACCEPTED)


class PaperBookLayEconomicsTests(unittest.TestCase):
    @staticmethod
    def _lay_leg() -> TicketLeg:
        return TicketLeg(
            event_id="event-lay",
            market_id="market-lay",
            selection_id="selection-lay",
            locked_odds=Decimal("5.00"),
            sport="football",
            exchange_side="lay",
        )

    def test_mutated_exchange_side_subclass_is_rejected_before_equality_dispatch(self):
        book = PaperBook(Decimal("100"))
        leg = self._lay_leg()
        object.__setattr__(leg, "exchange_side", _HostileExchangeSide("lay"))
        _HostileExchangeSide.comparisons = 0

        with self.assertRaisesRegex(ValueError, "exchange_side"):
            book.open_ticket(
                [leg],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )

        self.assertEqual(_HostileExchangeSide.comparisons, 0)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_lay_public_read_validates_visible_state_before_authority_comparison(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            reason="safe",
            placed_at=QUOTE_AT,
        )
        ticket.strategy_reason = _HostileComparableText("safe")
        _HostileComparableText.comparisons = 0

        def exact_text(value, label):
            if type(value) is not str:
                raise ValueError(f"PaperBook {label} must be a string")
            return value

        with patch.object(
            PaperBook,
            "_require_utf8_string",
            staticmethod(exact_text),
        ):
            with self.assertRaisesRegex(ValueError, "strategy_reason must be a string"):
                _ = book.committed_capital

        self.assertEqual(_HostileComparableText.comparisons, 0)

    def test_lay_lifecycle_rejects_noncanonical_settlement_key_before_rehash(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )
        hostile = _HostileSettlementKey()
        book._settlement_times[hostile] = None
        _HostileSettlementKey.calls = 0

        with self.assertRaisesRegex(
            ValueError,
            "settlement-time witness keys must be canonical strings",
        ):
            _ = book.committed_capital

        self.assertEqual(_HostileSettlementKey.calls, 0)

    def test_lay_open_rejects_arbitrary_stake_before_str_execution_without_upstream_parser(self):
        book = PaperBook(Decimal("100"))
        _HostileStake.calls = 0

        with self.assertRaisesRegex(
            TypeError,
            "exact Decimal, str, int, or float",
        ):
            book.open_ticket(
                [self._lay_leg()],
                _HostileStake(),
                placed_at=QUOTE_AT,
            )

        self.assertEqual(_HostileStake.calls, 0)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_lay_open_rejects_arbitrary_leg_iterable_before_iteration_without_upstream_parser(self):
        book = PaperBook(Decimal("100"))
        _HostileLegIterable.calls = 0

        with self.assertRaisesRegex(ValueError, "exact list or tuple"):
            book.open_ticket(
                _HostileLegIterable(),
                Decimal("10"),
                placed_at=QUOTE_AT,
            )

        self.assertEqual(_HostileLegIterable.calls, 0)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_lay_open_reuses_canonical_stake_ingress_when_available(self):
        book = PaperBook(Decimal("100"))
        _HostileStake.calls = 0

        def canonical_decimal(value, label):
            self.assertEqual(label, "stake")
            if type(value) is not Decimal:
                raise ValueError("canonical stake rejected")
            return value

        with patch.object(
            PaperBook,
            "_canonical_decimal_input",
            canonical_decimal,
            create=True,
        ):
            with self.assertRaisesRegex(ValueError, "canonical stake rejected"):
                book.open_ticket(
                    [self._lay_leg()],
                    _HostileStake(),
                    placed_at=QUOTE_AT,
                )

        self.assertEqual(_HostileStake.calls, 0)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_lay_open_preserves_exact_leg_container_ingress_when_available(self):
        book = PaperBook(Decimal("100"))
        _HostileLegIterable.calls = 0

        def canonical_decimal(value, _label):
            return Decimal(str(value))

        with patch.object(
            PaperBook,
            "_canonical_decimal_input",
            canonical_decimal,
            create=True,
        ):
            with self.assertRaisesRegex(ValueError, "exact list or tuple"):
                book.open_ticket(
                    _HostileLegIterable(),
                    Decimal("10"),
                    placed_at=QUOTE_AT,
                )

        self.assertEqual(_HostileLegIterable.calls, 0)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_lay_open_reuses_canonical_paperbook_operation_lock_when_available(self):
        book = PaperBook(Decimal("100"))
        entered = 0
        exited = 0

        @contextmanager
        def operation_lock(current):
            nonlocal entered, exited
            self.assertIs(current, book)
            entered += 1
            try:
                yield
            finally:
                exited += 1

        with patch.object(
            paper_module,
            "_require_paperbook_operation_lock",
            operation_lock,
            create=True,
        ):
            ticket = book.open_ticket(
                [self._lay_leg()],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )

        self.assertEqual(ticket.legs[0].exchange_side, "lay")
        self.assertEqual(entered, 1)
        self.assertEqual(exited, 1)
        self.assertEqual(book.balance, Decimal("60.00"))

    def test_lay_committed_capital_reuses_canonical_operation_lock_when_available(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )
        entered = 0
        exited = 0

        @contextmanager
        def operation_lock(current):
            nonlocal entered, exited
            self.assertIs(current, book)
            entered += 1
            try:
                yield
            finally:
                exited += 1

        with patch.object(
            paper_module,
            "_require_paperbook_operation_lock",
            operation_lock,
            create=True,
        ):
            committed = book.committed_capital

        self.assertEqual(committed, Decimal("40.00"))
        self.assertEqual(entered, 1)
        self.assertEqual(exited, 1)

    def test_invalid_canonical_operation_lock_authority_fails_closed(self):
        book = PaperBook(Decimal("100"))

        with patch.object(
            paper_module,
            "_require_paperbook_operation_lock",
            object(),
            create=True,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "operation lock authority is invalid",
            ):
                book.open_ticket(
                    [self._lay_leg()],
                    Decimal("10"),
                    placed_at=QUOTE_AT,
                )

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_lay_open_without_explicit_time_bypasses_mutated_clock_dispatch(self):
        book = PaperBook(Decimal("100"))
        hostile_calls = 0

        def forbidden():
            nonlocal hostile_calls
            hostile_calls += 1
            return "2099-01-01T00:00:00+00:00"

        with patch.object(paper_module, "utc_now_iso", forbidden):
            ticket = book.open_ticket([self._lay_leg()], Decimal("10"))

        self.assertEqual(hostile_calls, 0)
        self.assertNotEqual(ticket.placed_at, "2099-01-01T00:00:00+00:00")
        self.assertEqual(book.balance, Decimal("60.00"))

    def test_lay_open_class_debit_override_cannot_replace_liability_reserve_authority(self):
        book = PaperBook(Decimal("100"))
        hostile_calls = 0

        def hostile_debit(_cls, _balance, _amount):
            nonlocal hostile_calls
            hostile_calls += 1
            return Decimal("100")

        with patch.object(
            PaperBook,
            "_debit_balance",
            classmethod(hostile_debit),
        ):
            ticket = book.open_ticket(
                [self._lay_leg()],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(ticket.stake, Decimal("10"))
        self.assertEqual(book.balance, Decimal("60.00"))
        self.assertEqual(book.committed_capital, Decimal("40.00"))

    def test_lay_open_class_placed_at_override_cannot_replace_time_validation_authority(self):
        book = PaperBook(Decimal("100"))
        hostile_calls = 0

        def hostile_placed_at(_cls, _value, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1
            return "2099-01-01T00:00:00+00:00"

        with patch.object(
            PaperBook,
            "_validate_placed_at",
            classmethod(hostile_placed_at),
        ):
            ticket = book.open_ticket(
                [self._lay_leg()],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(ticket.placed_at, QUOTE_AT)
        self.assertEqual(book.balance, Decimal("60.00"))

    def test_lay_open_class_provenance_override_cannot_replace_binding_authority(self):
        book = PaperBook(Decimal("100"))
        hostile_calls = 0

        def hostile_provenance(_cls, *_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1
            return (("forged-source",), (), "forged-bankroll", "XXX")

        with patch.object(
            PaperBook,
            "_validate_ticket_provenance",
            classmethod(hostile_provenance),
        ):
            ticket = book.open_ticket(
                [self._lay_leg()],
                Decimal("10"),
                placed_at=QUOTE_AT,
                provider_source_ids=("paper-source",),
                provider_accounts=(("paper-source", "paper-account"),),
                bankroll_id="paper-bankroll",
                currency="EUR",
            )

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(ticket.provider_source_ids, ("paper-source",))
        self.assertEqual(
            ticket.provider_accounts,
            (("paper-source", "paper-account"),),
        )
        self.assertEqual(ticket.bankroll_id, "paper-bankroll")
        self.assertEqual(ticket.currency, "EUR")
        self.assertEqual(book.balance, Decimal("60.00"))

    def test_lay_economics_bypass_rebound_locked_capital_helper(self):
        import autosport._paperbook_lay_economics_guard as lay_economics_guard

        book = PaperBook(Decimal("100"))
        hostile_calls = 0

        def forged_zero_liability(*_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1
            return Decimal("0")

        with patch.object(
            lay_economics_guard,
            "locked_capital_for_exchange_side",
            forged_zero_liability,
        ):
            ticket = book.open_ticket(
                [self._lay_leg()],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )
            committed = book.committed_capital
            settled = book.settle(ticket.ticket_id, set())

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("40.00"))
        self.assertEqual(settled.status, TicketStatus.WON)
        self.assertEqual(settled.payout, Decimal("50.00"))
        self.assertEqual(book.balance, Decimal("110.00"))

    def test_open_lay_reserves_liability_and_reports_committed_capital(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )

        self.assertEqual(ticket.stake, Decimal("10"))
        self.assertEqual(book.balance, Decimal("60.00"))
        self.assertEqual(book.committed_capital, Decimal("40.00"))
        self.assertEqual(ticket.status, TicketStatus.OPEN)

    def test_lay_open_accepts_exact_liability_bankroll(self):
        book = PaperBook(Decimal("40"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )

        self.assertEqual(ticket.stake, Decimal("10"))
        self.assertEqual(book.balance, Decimal("0"))
        self.assertEqual(book.committed_capital, Decimal("40.00"))

    def test_lay_open_rejects_bankroll_below_liability_without_mutation(self):
        book = PaperBook(Decimal("39.99"))

        with self.assertRaisesRegex(ValueError, "insufficient virtual bankroll"):
            book.open_ticket(
                [self._lay_leg()],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )

        self.assertEqual(book.balance, Decimal("39.99"))
        self.assertEqual(book.tickets, {})
        self.assertEqual(book.committed_capital, Decimal("0"))

    def test_lay_selection_loses_returns_liability_plus_lay_stake(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )

        settled = book.settle(ticket.ticket_id, set())

        self.assertEqual(settled.status, TicketStatus.WON)
        self.assertEqual(settled.payout, Decimal("50.00"))
        self.assertEqual(book.balance, Decimal("110.00"))
        self.assertEqual(book.committed_capital, Decimal("0"))

    def test_lay_selection_wins_consumes_locked_liability(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )

        settled = book.settle(ticket.ticket_id, {ticket.legs[0].quote_key})

        self.assertEqual(settled.status, TicketStatus.LOST)
        self.assertEqual(settled.payout, Decimal("0"))
        self.assertEqual(book.balance, Decimal("60.00"))
        self.assertEqual(book.committed_capital, Decimal("0"))

    def test_live_lay_settlement_bypasses_class_settlement_result_override(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        def hostile(_cls, _ticket, balance, _winners, _voids):
            nonlocal hostile_calls
            hostile_calls += 1
            return TicketStatus.LOST, Decimal("0"), balance

        with patch.object(
            PaperBook,
            "_settlement_result",
            classmethod(hostile),
        ):
            settled = book.settle(ticket.ticket_id, set())

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(settled.status, TicketStatus.WON)
        self.assertEqual(settled.payout, Decimal("50.00"))
        self.assertEqual(book.balance, Decimal("110.00"))

    def test_live_lay_settlement_bypasses_module_causal_advance_override(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        def forbidden(*_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1
            raise AssertionError("module causal settlement override executed")

        with patch.object(
            paper_module,
            "_advance_paperbook_causal_history_settle",
            forbidden,
        ):
            settled = book.settle(ticket.ticket_id, set())

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(settled.status, TicketStatus.WON)
        self.assertEqual(book.balance, Decimal("110.00"))
        self.assertEqual(book.committed_capital, Decimal("0"))

    def test_live_lay_settlement_rejects_hostile_resolution_iterable_before_iteration(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        class HostileResolution:
            def __iter__(self):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("hostile resolution iterable executed")

        with self.assertRaisesRegex(
            ValueError,
            "exact built-in collection",
        ):
            book.settle(ticket.ticket_id, HostileResolution())

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(ticket.status, TicketStatus.OPEN)
        self.assertEqual(book.balance, Decimal("60.00"))

    def test_live_lay_settlement_rejects_ticket_id_subclass_before_hash_dispatch(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        class HostileTicketId(str):
            def __hash__(self):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("ticket id hash hook executed")

        with self.assertRaisesRegex(ValueError, "ticket_id must be an exact string"):
            book.settle(HostileTicketId(ticket.ticket_id), set())

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(ticket.status, TicketStatus.OPEN)
        self.assertEqual(book.balance, Decimal("60.00"))

    def test_live_lay_settlement_rejects_resolution_key_subclass_before_set_rehash(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        class HostileKey(str):
            def __hash__(self):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("resolution key hash hook executed")

        hostile_key = HostileKey(ticket.legs[0].quote_key)
        with self.assertRaisesRegex(ValueError, "must be an exact string"):
            book.settle(ticket.ticket_id, [hostile_key])

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(ticket.status, TicketStatus.OPEN)
        self.assertEqual(book.balance, Decimal("60.00"))

    def test_lay_void_releases_exact_locked_liability(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )

        settled = book.settle(
            ticket.ticket_id,
            set(),
            {ticket.legs[0].quote_key},
        )

        self.assertEqual(settled.status, TicketStatus.VOID)
        self.assertEqual(settled.payout, Decimal("40.00"))
        self.assertEqual(book.balance, Decimal("100.00"))
        self.assertEqual(book.committed_capital, Decimal("0"))

    def test_lay_replay_class_debit_override_cannot_replace_locked_capital_economics(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )
        hostile_calls = 0

        def hostile_debit(_cls, _balance, _amount):
            nonlocal hostile_calls
            hostile_calls += 1
            return Decimal("100")

        with patch.object(
            PaperBook,
            "_debit_balance",
            classmethod(hostile_debit),
        ):
            committed = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("40.00"))
        self.assertEqual(book.balance, Decimal("60.00"))

    def test_lay_replay_class_settlement_override_cannot_forge_snapshot_economics(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )
        book.settle(ticket.ticket_id, set())
        hostile_calls = 0

        def hostile_settlement(_cls, _ticket, balance, _winners, _voids):
            nonlocal hostile_calls
            hostile_calls += 1
            return TicketStatus.LOST, Decimal("0"), balance

        with patch.object(
            PaperBook,
            "_settlement_result",
            classmethod(hostile_settlement),
        ):
            committed = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("0"))
        self.assertEqual(book.balance, Decimal("110.00"))
        self.assertEqual(book.tickets[ticket.ticket_id].status, TicketStatus.WON)

    def test_lay_replay_class_lifecycle_entry_override_cannot_forge_history(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )
        hostile_calls = 0

        def hostile_entry(_cls, _entry):
            nonlocal hostile_calls
            hostile_calls += 1
            return ("open", "forged-ticket", (), ())

        with patch.object(
            PaperBook,
            "_validate_lifecycle_entry",
            classmethod(hostile_entry),
        ):
            committed = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("40.00"))
        self.assertEqual(book.balance, Decimal("60.00"))

    def test_lay_loaded_state_class_reachability_override_cannot_skip_replay(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
        )
        hostile_calls = 0

        def hostile_reachability(_cls, _book):
            nonlocal hostile_calls
            hostile_calls += 1

        with patch.object(
            PaperBook,
            "_validate_lifecycle_reachability",
            classmethod(hostile_reachability),
        ):
            committed = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("40.00"))
        self.assertEqual(book.balance, Decimal("60.00"))

    def test_lay_loaded_state_class_finite_override_cannot_replace_validation_authority(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        def hostile(*_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1

        with patch.object(PaperBook, "_require_finite", staticmethod(hostile)):
            committed = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("40.00"))

    def test_lay_loaded_state_class_text_override_cannot_replace_validation_authority(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        def hostile(*_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1

        with patch.object(PaperBook, "_require_canonical_text", staticmethod(hostile)):
            committed = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("40.00"))

    def test_lay_loaded_state_class_placed_at_override_cannot_replace_validation_authority(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        def hostile(*_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1
            return QUOTE_AT

        with patch.object(PaperBook, "_validate_placed_at", classmethod(hostile)):
            committed = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("40.00"))

    def test_lay_loaded_state_class_utf8_override_cannot_replace_validation_authority(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        def hostile(*_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1

        with patch.object(PaperBook, "_require_utf8_string", staticmethod(hostile)):
            committed = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("40.00"))

    def test_lay_loaded_state_class_provenance_override_cannot_replace_validation_authority(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        def hostile(*_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1
            return (), (), None, None

        with patch.object(PaperBook, "_validate_ticket_provenance", classmethod(hostile)):
            committed = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("40.00"))

    def test_lay_loaded_state_class_settled_at_override_cannot_replace_validation_authority(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        book.settle(ticket.ticket_id, set())
        hostile_calls = 0

        def hostile(*_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1
            return QUOTE_AT

        with patch.object(PaperBook, "_validate_settled_at", classmethod(hostile)):
            committed = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("0"))
        self.assertEqual(book.balance, Decimal("110.00"))

    def test_first_lay_open_rejects_hostile_lifecycle_container_before_economic_mutation(self):
        book = PaperBook(Decimal("100"))
        hostile_appends = 0

        class HostileLifecycle(list):
            def append(self, value):
                nonlocal hostile_appends
                hostile_appends += 1
                return super().append(value)

        book._lifecycle = HostileLifecycle(book._lifecycle)

        def skip_reachability(_cls, _book):
            return None

        with patch.object(
            PaperBook,
            "_validate_lifecycle_reachability",
            classmethod(skip_reachability),
        ):
            with self.assertRaisesRegex(ValueError, "lifecycle must be a canonical list"):
                book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)

        self.assertEqual(hostile_appends, 0)
        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_first_lay_open_rejects_hostile_settlement_witness_before_economic_mutation(self):
        book = PaperBook(Decimal("100"))

        class HostileSettlementWitness(dict):
            pass

        book._settlement_times = HostileSettlementWitness(book._settlement_times)

        def skip_reachability(_cls, _book):
            return None

        with patch.object(
            PaperBook,
            "_validate_lifecycle_reachability",
            classmethod(skip_reachability),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "settlement-time witness must be a canonical mapping",
            ):
                book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_lay_snapshot_rejects_balance_decimal_subclass_before_arithmetic_hooks(self):
        book = PaperBook(Decimal("100"))
        book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        class HostileDecimal(Decimal):
            def __lt__(self, _other):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("balance comparison hook executed")

            def __add__(self, _other):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("balance arithmetic hook executed")

        book.balance = HostileDecimal("60")

        with self.assertRaisesRegex(ValueError, "balance must be an exact Decimal"):
            _ = book.committed_capital

        self.assertEqual(hostile_calls, 0)

    def test_lay_snapshot_rejects_ticket_stake_decimal_subclass_before_economic_hooks(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        class HostileDecimal(Decimal):
            def __le__(self, _other):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("stake comparison hook executed")

            def __mul__(self, _other):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("stake multiplication hook executed")

        object.__setattr__(ticket, "stake", HostileDecimal("10"))

        with self.assertRaisesRegex(ValueError, "stake for ticket .* exact Decimal"):
            _ = book.committed_capital

        self.assertEqual(hostile_calls, 0)

    def test_lay_snapshot_rejects_ticket_payout_decimal_subclass_before_comparison_hooks(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        class HostileDecimal(Decimal):
            def __lt__(self, _other):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("payout comparison hook executed")

        object.__setattr__(ticket, "payout", HostileDecimal("0"))

        with self.assertRaisesRegex(ValueError, "payout for ticket .* exact Decimal"):
            _ = book.committed_capital

        self.assertEqual(hostile_calls, 0)

    def test_lay_snapshot_rejects_strategy_reason_str_subclass_before_contains_hook(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        class HostileText(str):
            def __contains__(self, _item):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("strategy reason contains hook executed")

        object.__setattr__(ticket, "strategy_reason", HostileText("reason"))

        with self.assertRaisesRegex(ValueError, "exact string"):
            _ = book.committed_capital

        self.assertEqual(hostile_calls, 0)

    def test_lay_snapshot_rejects_provenance_str_subclass_before_sort_hash_hooks(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket(
            [self._lay_leg()],
            Decimal("10"),
            placed_at=QUOTE_AT,
            provider_source_ids=("paper-exchange",),
            provider_accounts=(("paper-exchange", "paper-account"),),
        )
        hostile_calls = 0

        class HostileText(str):
            def __lt__(self, _other):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("provenance ordering hook executed")

            def __hash__(self):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("provenance hash hook executed")

        object.__setattr__(
            ticket,
            "provider_source_ids",
            (HostileText("paper-exchange"),),
        )

        with self.assertRaisesRegex(ValueError, "exact string"):
            _ = book.committed_capital

        self.assertEqual(hostile_calls, 0)

    def test_lay_snapshot_rejects_timestamp_str_subclass_before_text_hooks(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        class HostileText(str):
            def strip(self, *_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("timestamp strip hook executed")

            def encode(self, *_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("timestamp encode hook executed")

        object.__setattr__(ticket, "placed_at", HostileText(QUOTE_AT))

        with self.assertRaisesRegex(ValueError, "exact string"):
            _ = book.committed_capital

        self.assertEqual(hostile_calls, 0)

    def test_lay_replay_rejects_lifecycle_str_subclass_before_membership_hooks(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        action, ticket_id, winners, voids = book._lifecycle[0]
        hostile_calls = 0

        class HostileText(str):
            def __eq__(self, _other):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("lifecycle equality hook executed")

            def __hash__(self):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("lifecycle hash hook executed")

        book._lifecycle[0] = (
            HostileText(action),
            ticket_id,
            winners,
            voids,
        )

        with self.assertRaisesRegex(ValueError, "lifecycle action"):
            _ = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(book.balance, Decimal("60.00"))
        self.assertIn(ticket.ticket_id, book.tickets)

    def test_lay_leg_rejects_decimal_subclass_before_decimal_hooks(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)

        class HostileDecimal(Decimal):
            pass

        hostile = HostileDecimal("5.00")
        object.__setattr__(ticket.legs[0], "locked_odds", hostile)

        with self.assertRaisesRegex(ValueError, "exact Decimal"):
            _ = book.committed_capital

    def test_lay_settlement_bypasses_mutated_decimal_context_authority(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        def forbidden():
            nonlocal hostile_calls
            hostile_calls += 1
            raise AssertionError("mutated decimal context authority executed")

        with patch.object(paper_module, "_paper_decimal_context", forbidden):
            settled = book.settle(ticket.ticket_id, set())

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(settled.status, TicketStatus.WON)
        self.assertEqual(settled.payout, Decimal("50.00"))
        self.assertEqual(book.balance, Decimal("110.00"))

    def test_lay_open_bypasses_mutated_ticket_authority_module_dispatch(self):
        book = PaperBook(Decimal("100"))
        hostile_calls = 0

        def forbidden(*_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1
            raise AssertionError("mutated ticket authority module dispatch executed")

        with patch.object(
            paper_module,
            "_require_ticket_opening_authority",
            forbidden,
        ), patch.object(
            paper_module,
            "_require_paperbook_causal_history_authority",
            forbidden,
        ), patch.object(
            paper_module,
            "_record_ticket_opening_authority",
            forbidden,
        ), patch.object(
            paper_module,
            "_advance_paperbook_causal_history_open",
            forbidden,
        ):
            ticket = book.open_ticket(
                [self._lay_leg()],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(book.balance, Decimal("60.00"))
        self.assertIn(ticket.ticket_id, book.tickets)

    def test_lay_replay_bypasses_mutated_timestamp_parser_dispatch(self):
        book = PaperBook(Decimal("100"))
        ticket = book.open_ticket([self._lay_leg()], Decimal("10"), placed_at=QUOTE_AT)
        hostile_calls = 0

        def forbidden(*_args, **_kwargs):
            nonlocal hostile_calls
            hostile_calls += 1
            raise AssertionError("mutated timestamp parser dispatch executed")

        with patch.object(paper_module, "parse_iso_timestamp", forbidden):
            committed = book.committed_capital

        self.assertEqual(hostile_calls, 0)
        self.assertEqual(committed, Decimal("40.00"))
        self.assertEqual(book.balance, Decimal("60.00"))
        self.assertIn(ticket.ticket_id, book.tickets)

    def test_open_lay_snapshot_round_trip_preserves_liability_and_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-book.json"
            book = PaperBook(Decimal("100"))
            ticket = book.open_ticket(
                [self._lay_leg()],
                Decimal("10"),
                placed_at=QUOTE_AT,
            )
            book.save(path)

            loaded = PaperBook.load(path)
            loaded_ticket = loaded.tickets[ticket.ticket_id]

            self.assertEqual(loaded_ticket.legs[0].exchange_side, "lay")
            self.assertEqual(loaded_ticket.stake, Decimal("10"))
            self.assertEqual(loaded.balance, Decimal("60.00"))
            self.assertEqual(loaded.committed_capital, Decimal("40.00"))


if __name__ == "__main__":
    unittest.main()
