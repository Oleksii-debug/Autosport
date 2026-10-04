from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.paper_execution_reality import (
    EvidenceGrade,
    ObservedPaperExecution,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
    RecoveryDecision,
    execute_paper_plan,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T03:00:00+00:00"
STARTED_AT = "2026-09-20T03:00:00.100000+00:00"
EXPIRES_AT = "2026-09-20T03:01:00+00:00"


def rewrite_rehashed_events(
    ledger: PaperExecutionLedger,
    events: list[dict[str, object]],
) -> list[dict[str, object]]:
    rewritten: list[dict[str, object]] = []
    previous_sha256 = None
    for sequence, item in enumerate(events):
        event = ledger._event(
            event_type=item["event_type"],
            run_id=item["run_id"],
            key=item["event_key"],
            payload=item["payload"],
            sequence=sequence,
            previous_sha256=previous_sha256,
        )
        rewritten.append(event)
        previous_sha256 = event["event_sha256"]
    ledger.path.write_text(
        "".join(
            json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for item in rewritten
        ),
        encoding="utf-8",
    )
    ledger._write_anchor_unlocked(rewritten)
    return rewritten


def action(
    action_id: str,
    *,
    odds: str = "2.50",
    stake: str = "10.00",
    side: str = "BACK",
    market_id: str | None = None,
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-venue",
        account_id="paper-account",
        event_id="event-1",
        market_id=market_id or f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side=side,
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )


def plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="paper-plan-1",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="decision-1",
        approval_id="paper-only",
        created_at=QUOTE_AT,
        actions=tuple(actions),
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
        "max_slippage_bps": 100,
    }
    values.update(overrides)
    return PaperExecutionModelConfig(**values)


def registered_observation(
    ledger: PaperExecutionLedger,
    source_action: ExecutionAction,
    outcome: PaperAttemptOutcome,
    *,
    at: str = "2026-09-20T03:00:00.250000+00:00",
    odds: str | None = None,
    stake: str | None = None,
    suspended: bool = False,
    grade: EvidenceGrade = EvidenceGrade.EMPIRICAL,
    source: str = "captured-paper-observation-v1",
) -> tuple[ObservedPaperExecution, PaperExecutionEvidenceRegistry]:
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
        observed_at=at,
        evidence_grade=grade,
        evidence_source=source,
        accepted_odds=odds,
        accepted_stake=stake,
        suspended=suspended,
        reason=f"observed {outcome.value.lower()}",
    )
    registry = PaperExecutionEvidenceRegistry(ledger)
    registry.register(record)
    return record.as_observation(), registry


class PaperExecutionRealityTests(unittest.TestCase):
    def test_all_accepted_keeps_decision_and_execution_quotes_distinct_and_dedupes_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1", odds="2.50"), action("a2", odds="3.00"))
            model = config()

            first = execute_paper_plan(
                plan=current,
                trigger_id="trigger-1",
                config=model,
                ledger=ledger,
                started_at=STARTED_AT,
            )
            self.assertTrue(first.completed)
            self.assertTrue(first.all_actions_accepted)
            self.assertEqual(first.pending_action_ids, ())
            self.assertEqual(first.recovery_decision, RecoveryDecision.NONE)
            self.assertEqual(len(first.attempts), 2)
            for attempt, source_action in zip(first.attempts, current.actions, strict=True):
                self.assertEqual(attempt.decision_quote_id, source_action.quote_id)
                self.assertEqual(attempt.decision_odds, source_action.requested_odds)
                self.assertEqual(attempt.execution_stake, source_action.requested_stake)
                self.assertEqual(attempt.evidence_grade, EvidenceGrade.SYNTHETIC)
                self.assertIsNone(attempt.evidence_id)

            count = len(ledger.events())
            second = execute_paper_plan(
                plan=current,
                trigger_id="trigger-1",
                config=model,
                ledger=ledger,
                started_at=STARTED_AT,
            )
            self.assertEqual(second, first)
            self.assertEqual(len(ledger.events()), count)

    def test_second_leg_rejection_stops_sequence_and_exposes_recovery_need(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"), action("a2"), action("a3"))
            obs1, registry = registered_observation(
                ledger, current.actions[0], PaperAttemptOutcome.ACCEPTED,
                odds="2.40", stake="10.00"
            )
            obs2, _ = registered_observation(
                ledger, current.actions[1], PaperAttemptOutcome.REJECTED
            )
            result = execute_paper_plan(
                plan=current,
                trigger_id="trigger-reject",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={"a1": obs1, "a2": obs2},
                evidence_registry=registry,
            )
            self.assertEqual(
                [item.outcome for item in result.attempts],
                [PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.REJECTED],
            )
            self.assertEqual(result.pending_action_ids, ("a3",))
            self.assertEqual(result.recovery_decision, RecoveryDecision.HEDGE_REVIEW_REQUIRED)
            self.assertEqual(result.worst_case_exposure, Decimal("10.00"))

    def test_partial_fill_is_recorded_and_never_blindly_continues_next_leg(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"), action("a2"))
            obs, registry = registered_observation(
                ledger, current.actions[0], PaperAttemptOutcome.PARTIAL,
                odds="2.30", stake="4.00"
            )
            result = execute_paper_plan(
                plan=current,
                trigger_id="trigger-partial",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={"a1": obs},
                evidence_registry=registry,
            )
            self.assertEqual(len(result.attempts), 1)
            self.assertEqual(result.attempts[0].outcome, PaperAttemptOutcome.PARTIAL)
            self.assertEqual(result.attempts[0].execution_stake, Decimal("4.00"))
            self.assertEqual(result.pending_action_ids, ("a2",))
            self.assertEqual(result.worst_case_exposure, Decimal("4.00"))
            self.assertEqual(result.recovery_decision, RecoveryDecision.HEDGE_REVIEW_REQUIRED)

    def test_unknown_attempt_uses_requested_stake_as_worst_case_and_does_not_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1", stake="12.50"))
            model = config(unknown_bps=10_000)
            first = execute_paper_plan(
                plan=current, trigger_id="trigger-unknown", config=model,
                ledger=ledger, started_at=STARTED_AT,
            )
            count = len(ledger.events())
            second = execute_paper_plan(
                plan=current, trigger_id="trigger-unknown", config=model,
                ledger=ledger, started_at=STARTED_AT,
            )
            self.assertEqual(first.attempts[0].outcome, PaperAttemptOutcome.UNKNOWN)
            self.assertEqual(first.worst_case_exposure, Decimal("12.50"))
            self.assertEqual(first.recovery_decision, RecoveryDecision.HEDGE_REVIEW_REQUIRED)
            self.assertEqual(second, first)
            self.assertEqual(len(ledger.events()), count)

    def test_suspension_rejects_current_leg_and_preserves_remaining_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"), action("a2"), action("a3"))
            result = execute_paper_plan(
                plan=current, trigger_id="trigger-suspend", config=config(),
                ledger=ledger, started_at=STARTED_AT,
                suspended_action_ids=frozenset({"a2"}),
            )
            self.assertEqual(
                [item.outcome for item in result.attempts],
                [PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.REJECTED],
            )
            self.assertTrue(result.attempts[1].suspended)
            self.assertEqual(result.pending_action_ids, ("a3",))

    def test_suspension_set_is_durable_run_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"), action("a2"), action("a3"))
            suspended = frozenset({"a2"})
            first = execute_paper_plan(
                plan=current,
                trigger_id="trigger-suspend-identity",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=suspended,
            )
            restarted = execute_paper_plan(
                plan=current,
                trigger_id="trigger-suspend-identity",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=suspended,
            )
            self.assertEqual(restarted, first)
            reserve = next(
                event
                for event in ledger.events()
                if event["event_type"] == "RUN_RESERVED"
            )
            self.assertEqual(
                reserve["payload"]["suspended_action_ids"],
                ["a2"],
            )

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "event_key already has different payload",
            ):
                execute_paper_plan(
                    plan=current,
                    trigger_id="trigger-suspend-identity",
                    config=config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                )

    def test_observation_and_suspension_cannot_claim_same_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"))
            obs, registry = registered_observation(
                ledger,
                current.actions[0],
                PaperAttemptOutcome.ACCEPTED,
            )
            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "both observed and synthetically suspended",
            ):
                execute_paper_plan(
                    plan=current,
                    trigger_id="trigger-observed-suspended",
                    config=config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={"a1": obs},
                    evidence_registry=registry,
                    suspended_action_ids=frozenset({"a1"}),
                )
            self.assertFalse(
                any(
                    event["event_type"] == "RUN_RESERVED"
                    for event in ledger.events()
                )
            )

    def test_stale_quote_fails_closed_without_claiming_a_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            result = execute_paper_plan(
                plan=plan(action("a1")),
                trigger_id="trigger-stale",
                config=config(max_quote_age_ms=500),
                ledger=ledger,
                started_at="2026-09-20T03:00:01+00:00",
            )
            attempt = result.attempts[0]
            self.assertEqual(attempt.outcome, PaperAttemptOutcome.REJECTED)
            self.assertIsNone(attempt.execution_odds)
            self.assertIsNone(attempt.execution_stake)
            self.assertEqual(result.recovery_decision, RecoveryDecision.NO_EXPOSURE)

    def test_empirical_odds_move_is_registry_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1", odds="2.50"))
            obs, registry = registered_observation(
                ledger, current.actions[0], PaperAttemptOutcome.ACCEPTED,
                odds="2.20", stake="10.00"
            )
            result = execute_paper_plan(
                plan=current, trigger_id="trigger-odds-move", config=config(),
                ledger=ledger, started_at=STARTED_AT,
                observations={"a1": obs}, evidence_registry=registry,
            )
            attempt = result.attempts[0]
            self.assertEqual(attempt.decision_odds, Decimal("2.50"))
            self.assertEqual(attempt.execution_odds, Decimal("2.20"))
            self.assertEqual(attempt.evidence_grade, EvidenceGrade.EMPIRICAL)
            self.assertEqual(attempt.evidence_id, obs.evidence_id)
            self.assertEqual(attempt.evidence_sha256, obs.evidence_sha256)

    def test_submillisecond_timestamp_precision_is_accepted_and_floored(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"))
            obs, registry = registered_observation(
                ledger, current.actions[0], PaperAttemptOutcome.ACCEPTED,
                at="2026-09-20T03:00:00.250987+00:00",
                odds="2.40", stake="10.00"
            )
            result = execute_paper_plan(
                plan=current, trigger_id="trigger-sub-ms", config=config(),
                ledger=ledger, started_at="2026-09-20T03:00:00.100123+00:00",
                observations={"a1": obs}, evidence_registry=registry,
            )
            self.assertEqual(result.attempts[0].delay_ms, 150)
            self.assertEqual(result.attempts[0].quote_age_ms, 250)

    def test_restart_reads_same_completed_state_from_durable_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            current = plan(action("a1"), action("a2"))
            model = config()
            first = execute_paper_plan(
                plan=current, trigger_id="trigger-restart", config=model,
                ledger=PaperExecutionLedger(path), started_at=STARTED_AT,
            )
            restarted = execute_paper_plan(
                plan=current, trigger_id="trigger-restart", config=model,
                ledger=PaperExecutionLedger(path), started_at=STARTED_AT,
            )
            self.assertEqual(restarted, first)

    def test_tampered_event_fails_closed_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            execute_paper_plan(
                plan=plan(action("a1")), trigger_id="trigger-tamper",
                config=config(), ledger=PaperExecutionLedger(path), started_at=STARTED_AT,
            )
            lines = path.read_text(encoding="utf-8").splitlines()
            event = json.loads(lines[1])
            event["payload"]["reason"] = "tampered"
            lines[1] = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(PaperExecutionIntegrityError, "digest mismatch"):
                PaperExecutionLedger(path).events()

    def test_rehashed_unknown_event_type_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            execute_paper_plan(
                plan=plan(action("a1")),
                trigger_id="trigger-unknown-event",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )
            events = list(ledger.events())
            forged = ledger._event(
                event_type="FORGED_EXECUTION_EVENT",
                run_id=events[-1]["run_id"],
                key=f'{events[-1]["run_id"]}:forged',
                payload={},
                sequence=len(events),
                previous_sha256=events[-1]["event_sha256"],
            )
            rewritten = [*events, forged]
            path.write_text(
                "".join(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                    for item in rewritten
                ),
                encoding="utf-8",
            )
            ledger._write_anchor_unlocked(rewritten)

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "unsupported event_type",
            ):
                PaperExecutionLedger(path).events()

    def test_rehashed_synthetic_attempt_semantics_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = plan(action("a1"))
            model = config()
            execute_paper_plan(
                plan=current,
                trigger_id="trigger-rehashed-attempt",
                config=model,
                ledger=ledger,
                started_at=STARTED_AT,
            )
            events = list(ledger.events())
            attempt = next(
                item
                for item in events
                if item["event_type"] == "ATTEMPT_RECORDED"
            )
            attempt["payload"]["reason"] = "forged but fully rehashed"
            rewrite_rehashed_events(ledger, events)

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "synthetic attempt is not reproducible",
            ):
                execute_paper_plan(
                    plan=current,
                    trigger_id="trigger-rehashed-attempt",
                    config=model,
                    ledger=ledger,
                    started_at=STARTED_AT,
                )

    def test_rehashed_attempt_event_reordering_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = plan(action("a1"), action("a2"))
            model = config()
            execute_paper_plan(
                plan=current,
                trigger_id="trigger-reordered-attempts",
                config=model,
                ledger=ledger,
                started_at=STARTED_AT,
            )
            events = list(ledger.events())
            indexes = [
                index
                for index, item in enumerate(events)
                if item["event_type"] == "ATTEMPT_RECORDED"
            ]
            self.assertEqual(len(indexes), 2)
            events[indexes[0]], events[indexes[1]] = (
                events[indexes[1]],
                events[indexes[0]],
            )
            rewrite_rehashed_events(ledger, events)

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "canonical sequence order",
            ):
                execute_paper_plan(
                    plan=current,
                    trigger_id="trigger-reordered-attempts",
                    config=model,
                    ledger=ledger,
                    started_at=STARTED_AT,
                )

    def test_rehashed_observed_attempt_must_reproduce_registered_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = plan(action("a1"))
            observation, registry = registered_observation(
                ledger,
                current.actions[0],
                PaperAttemptOutcome.ACCEPTED,
                odds="2.40",
                stake="10.00",
            )
            execute_paper_plan(
                plan=current,
                trigger_id="trigger-observed-rehash",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={"a1": observation},
                evidence_registry=registry,
            )
            events = list(ledger.events())
            attempt = next(
                item
                for item in events
                if item["event_type"] == "ATTEMPT_RECORDED"
            )
            attempt["payload"]["reason"] = "forged observed execution reason"
            rewrite_rehashed_events(ledger, events)

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "observed attempt is not reproducible",
            ):
                execute_paper_plan(
                    plan=current,
                    trigger_id="trigger-observed-rehash",
                    config=config(),
                    ledger=ledger,
                    started_at=STARTED_AT,
                    observations={"a1": observation},
                    evidence_registry=registry,
                )

    def test_rehashed_evidence_event_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = action("a1")
            observation, _registry = registered_observation(
                ledger,
                current,
                PaperAttemptOutcome.ACCEPTED,
                odds="2.40",
                stake="10.00",
            )
            events = list(ledger.events())
            evidence_event = next(
                item
                for item in events
                if item["event_type"]
                == "OBSERVATION_EVIDENCE_REGISTERED"
            )
            evidence_event["event_key"] = (
                "evidence:forged-" + observation.evidence_id
            )
            rewrite_rehashed_events(ledger, events)

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "evidence event identity is invalid",
            ):
                PaperExecutionLedger(path).events()

    def test_rehashed_reservation_event_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            execute_paper_plan(
                plan=plan(action("a1")),
                trigger_id="trigger-reservation-key",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )
            events = list(ledger.events())
            reservation = next(
                item
                for item in events
                if item["event_type"] == "RUN_RESERVED"
            )
            reservation["event_key"] = (
                reservation["run_id"] + ":forged-reserve"
            )
            rewrite_rehashed_events(ledger, events)

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "reservation event identity is invalid",
            ):
                PaperExecutionLedger(path).events()

    def test_rehashed_reservation_payload_extension_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            execute_paper_plan(
                plan=plan(action("a1")),
                trigger_id="trigger-reservation-schema",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )
            events = list(ledger.events())
            reservation = next(
                item
                for item in events
                if item["event_type"] == "RUN_RESERVED"
            )
            reservation["payload"]["alternate_authority"] = "forged"
            rewrite_rehashed_events(ledger, events)

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "reservation payload is invalid",
            ):
                PaperExecutionLedger(path).events()

    def test_rehashed_completion_payload_extension_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            execute_paper_plan(
                plan=plan(action("a1")),
                trigger_id="trigger-completion-schema",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )
            events = list(ledger.events())
            completion = next(
                item
                for item in events
                if item["event_type"] == "RUN_COMPLETED"
            )
            completion["payload"]["alternate_economics"] = "forged"
            rewrite_rehashed_events(ledger, events)

            with self.assertRaisesRegex(
                PaperExecutionIntegrityError,
                "completion payload is invalid",
            ):
                PaperExecutionLedger(path).events()

    def test_invalid_reservation_inputs_leave_no_durable_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            current = plan(action("a1"))
            model = config()
            run_id = "paper-exec-v2-" + "1" * 64

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "both observed and synthetically suspended",
            ):
                ledger.reserve_run(
                    run_id=run_id,
                    trigger_id="trigger-invalid-reservation",
                    plan=current,
                    config=model,
                    started_at=STARTED_AT,
                    observation_evidence_ids={"a1": "evidence-a1"},
                    suspended_action_ids=frozenset({"a1"}),
                )
            self.assertFalse(path.exists())

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "invalid execution evidence",
            ):
                ledger.reserve_run(
                    run_id=run_id,
                    trigger_id="trigger-invalid-reservation",
                    plan=current,
                    config=model,
                    started_at=STARTED_AT,
                    observation_evidence_ids={
                        "outside-plan": "evidence-outside"
                    },
                )
            self.assertFalse(path.exists())

    def test_attempt_transition_requires_reservation_before_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = PaperExecutionLedger(
                Path(tmp) / "source-execution.jsonl"
            )
            current = plan(action("a1"))
            result = execute_paper_plan(
                plan=current,
                trigger_id="trigger-attempt-source",
                config=config(),
                ledger=source,
                started_at=STARTED_AT,
            )
            attempt = result.attempts[0]

            target_path = Path(tmp) / "target-execution.jsonl"
            target = PaperExecutionLedger(target_path)
            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "requires exactly one durable reservation",
            ):
                target.record_attempt(attempt)
            self.assertFalse(target_path.exists())

    def test_exact_attempt_retry_is_idempotent_after_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(
                Path(tmp) / "paper-execution.jsonl"
            )
            result = execute_paper_plan(
                plan=plan(action("a1")),
                trigger_id="trigger-attempt-idempotency",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )
            event_count = len(ledger.events())
            ledger.record_attempt(result.attempts[0])
            self.assertEqual(len(ledger.events()), event_count)

    def test_attempt_write_cannot_switch_reserved_evidence_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = plan(action("a1"))
            model = config()
            source = PaperExecutionLedger(
                Path(tmp) / "source-observed.jsonl"
            )
            observation, registry = registered_observation(
                source,
                current.actions[0],
                PaperAttemptOutcome.ACCEPTED,
                odds="2.40",
                stake="10.00",
            )
            observed = execute_paper_plan(
                plan=current,
                trigger_id="trigger-evidence-class",
                config=model,
                ledger=source,
                started_at=STARTED_AT,
                observations={"a1": observation},
                evidence_registry=registry,
            )

            target = PaperExecutionLedger(
                Path(tmp) / "target-synthetic.jsonl"
            )
            target.reserve_run(
                run_id=observed.run_id,
                trigger_id="trigger-evidence-class",
                plan=current,
                config=model,
                started_at=STARTED_AT,
                observation_evidence_ids={},
            )
            event_count = len(target.events())
            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "reserved synthetic authority",
            ):
                target.record_attempt(observed.attempts[0])
            self.assertEqual(len(target.events()), event_count)

    def test_exact_attempt_retry_reverifies_rehashed_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            result = execute_paper_plan(
                plan=plan(action("a1")),
                trigger_id="trigger-retry-reservation-proof",
                config=config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )
            events = list(ledger.events())
            reservation = next(
                item
                for item in events
                if item["event_type"] == "RUN_RESERVED"
            )
            reservation["payload"]["plan_id"] = "forged-plan-id"
            rewrite_rehashed_events(ledger, events)

            with self.assertRaisesRegex(
                PaperExecutionStateError,
                "reserved plan/model identity",
            ):
                ledger.record_attempt(result.attempts[0])

    def test_writer_lock_fails_closed_instead_of_creating_parallel_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            ledger = PaperExecutionLedger(path)
            ledger._lock_path.write_text("occupied", encoding="utf-8")
            try:
                with self.assertRaisesRegex(PaperExecutionStateError, "writer lock exists"):
                    execute_paper_plan(
                        plan=plan(action("a1")), trigger_id="trigger-lock",
                        config=config(), ledger=ledger, started_at=STARTED_AT,
                    )
            finally:
                ledger._lock_path.unlink()

    def test_non_back_plan_fails_closed_until_liability_authority_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(PaperExecutionStateError, "supports BACK only"):
                execute_paper_plan(
                    plan=plan(action("a1", side="LAY")),
                    trigger_id="trigger-lay",
                    config=config(),
                    ledger=PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl"),
                    started_at=STARTED_AT,
                )

    def test_per_leg_market_identity_survives_for_external_settlement(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(
                action("a1", market_id="match-odds"),
                action("a2", market_id="total-goals"),
            )
            result = execute_paper_plan(
                plan=current, trigger_id="trigger-settlement-identity",
                config=config(max_slippage_bps=0), ledger=ledger, started_at=STARTED_AT,
            )
            self.assertEqual(
                [(item.event_id, item.market_id, item.selection_id) for item in result.attempts],
                [
                    ("event-1", "match-odds", "selection-a1"),
                    ("event-1", "total-goals", "selection-a2"),
                ],
            )

    def test_tail_deletion_of_completion_is_detected_by_latest_root_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            execute_paper_plan(
                plan=plan(action("a1")), trigger_id="trigger-tail",
                config=config(), ledger=PaperExecutionLedger(path), started_at=STARTED_AT,
            )
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(lines[-1])["event_type"], "RUN_COMPLETED")
            path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                PaperExecutionIntegrityError, "event count|root"
            ):
                PaperExecutionLedger(path).events()

    def test_deleting_last_attempt_while_completion_remains_breaks_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            execute_paper_plan(
                plan=plan(action("a1"), action("a2")), trigger_id="trigger-delete-attempt",
                config=config(), ledger=PaperExecutionLedger(path), started_at=STARTED_AT,
            )
            lines = path.read_text(encoding="utf-8").splitlines()
            attempt_indexes = [
                i for i, line in enumerate(lines)
                if json.loads(line)["event_type"] == "ATTEMPT_RECORDED"
            ]
            del lines[attempt_indexes[-1]]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(
                PaperExecutionIntegrityError, "sequence|predecessor"
            ):
                PaperExecutionLedger(path).events()

    def test_replaying_older_valid_prefix_with_current_anchor_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "paper-execution.jsonl"
            execute_paper_plan(
                plan=plan(action("a1"), action("a2")), trigger_id="trigger-prefix",
                config=config(), ledger=PaperExecutionLedger(path), started_at=STARTED_AT,
            )
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(PaperExecutionIntegrityError, "event count|root"):
                PaperExecutionLedger(path).events()

    def test_forged_empirical_observation_with_unknown_id_fails_before_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"))
            forged = ObservedPaperExecution(
                action_id="a1",
                outcome=PaperAttemptOutcome.ACCEPTED,
                observed_at="2026-09-20T03:00:00.250000+00:00",
                evidence_grade=EvidenceGrade.EMPIRICAL,
                evidence_source="caller-claimed-empirical",
                evidence_id="paper-exec-evidence-v1-" + "a" * 64,
                evidence_sha256="a" * 64,
                accepted_odds="99.00",
                accepted_stake="10.00",
            )
            with self.assertRaisesRegex(PaperExecutionStateError, "unknown to durable registry"):
                execute_paper_plan(
                    plan=current, trigger_id="trigger-forged", config=config(),
                    ledger=ledger, started_at=STARTED_AT,
                    observations={"a1": forged},
                    evidence_registry=PaperExecutionEvidenceRegistry(ledger),
                )
            self.assertEqual(ledger.events(), ())

    def test_registered_evidence_for_different_quote_cannot_authorize_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
            current = plan(action("a1"))
            wrong = action("a1")
            object.__setattr__(wrong, "quote_id", "different-quote")
            obs, registry = registered_observation(
                ledger, wrong, PaperAttemptOutcome.ACCEPTED,
                odds="2.40", stake="10.00"
            )
            with self.assertRaisesRegex(PaperExecutionStateError, "exact action/quote"):
                execute_paper_plan(
                    plan=current, trigger_id="trigger-wrong-quote", config=config(),
                    ledger=ledger, started_at=STARTED_AT,
                    observations={"a1": obs}, evidence_registry=registry,
                )


if __name__ == "__main__":
    unittest.main()
