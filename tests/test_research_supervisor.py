import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.research_supervisor import (
    InvalidResearchTransitionError,
    ResearchPhase,
    ResearchRunStoppedError,
    ResearchSupervisorStore,
    ResearchTrigger,
    StaleSupervisorCheckpointError,
    SupervisorControls,
    TriggerIdentityConflictError,
)


class ResearchSupervisorStoreTests(unittest.TestCase):
    @staticmethod
    def _trigger(
        trigger_id: str = "timer:2026-09-17T17:00Z",
        question_id: str = "q-1",
    ) -> ResearchTrigger:
        return ResearchTrigger(
            trigger_id=trigger_id,
            trigger_kind="scheduled",
            question_id=question_id,
            requested_at="2026-09-17T17:00:00Z",
            payload_sha256="a" * 64,
        )

    @staticmethod
    def _controls(
        *,
        budget: str = "10",
        deadline: str = "2026-09-18T17:00:00Z",
    ) -> SupervisorControls:
        return SupervisorControls(
            priority=50,
            budget_limit=Decimal(budget),
            deadline=deadline,
            max_retries=2,
            max_experiments=1,
            dependencies=("factory-v1", "learning-env-v1"),
        )

    @staticmethod
    def _advance_to_source_search(
        store: ResearchSupervisorStore,
        run_id: str,
        revision: int,
    ):
        checkpoint = store.advance(
            run_id,
            expected_revision=revision,
            phase=ResearchPhase.HYPOTHESIS,
            checkpointed_at="2026-09-17T17:01:00Z",
            bindings={"hypothesis_id": "h-1"},
            next_action=ResearchPhase.SOURCE_SEARCH.value,
        )
        return store.advance(
            run_id,
            expected_revision=checkpoint.revision,
            phase=ResearchPhase.SOURCE_SEARCH,
            checkpointed_at="2026-09-17T17:02:00Z",
            bindings={
                "source_search_sha256": "b" * 64,
                "negative_memory_check_sha256": "c" * 64,
            },
            next_action=ResearchPhase.PROTOCOL_FREEZE.value,
        )

    def test_duplicate_trigger_collapses_to_same_durable_run_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research_supervisor.json"
            store = ResearchSupervisorStore.initialize_pristine(path)
            trigger = self._trigger()
            controls = self._controls()
            first = store.start_or_resume(
                trigger,
                controls,
                checkpointed_at="2026-09-17T17:00:01Z",
            )

            reopened = ResearchSupervisorStore(path)
            duplicate = reopened.start_or_resume(
                trigger,
                controls,
                checkpointed_at="2026-09-17T17:10:00Z",
            )

            self.assertEqual(duplicate, first)
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(state["runs"]), 1)
            self.assertEqual(len(state["triggers"]), 1)

    def test_duplicate_trigger_cannot_rewrite_question_or_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research_supervisor.json"
            store = ResearchSupervisorStore.initialize_pristine(path)
            trigger = self._trigger()
            store.start_or_resume(
                trigger,
                self._controls(),
                checkpointed_at="2026-09-17T17:00:01Z",
            )

            with self.assertRaises(TriggerIdentityConflictError):
                store.start_or_resume(
                    self._trigger(question_id="q-rewritten"),
                    self._controls(),
                    checkpointed_at="2026-09-17T17:00:02Z",
                )
            with self.assertRaises(TriggerIdentityConflictError):
                store.start_or_resume(
                    trigger,
                    self._controls(budget="11"),
                    checkpointed_at="2026-09-17T17:00:02Z",
                )

    def test_phase_machine_rejects_skip_and_post_commit_protocol_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research_supervisor.json"
            store = ResearchSupervisorStore.initialize_pristine(path)
            checkpoint = store.start_or_resume(
                self._trigger(),
                self._controls(),
                checkpointed_at="2026-09-17T17:00:01Z",
            )

            with self.assertRaises(InvalidResearchTransitionError):
                store.advance(
                    checkpoint.run_id,
                    expected_revision=checkpoint.revision,
                    phase=ResearchPhase.PROTOCOL_FREEZE,
                    checkpointed_at="2026-09-17T17:01:00Z",
                    bindings={
                        "hypothesis_id": "h-1",
                        "source_search_sha256": "b" * 64,
                        "negative_memory_check_sha256": "c" * 64,
                        "protocol_id": "protocol-1",
                        "protocol_sha256": "d" * 64,
                    },
                )

            checkpoint = self._advance_to_source_search(
                store,
                checkpoint.run_id,
                checkpoint.revision,
            )
            checkpoint = store.advance(
                checkpoint.run_id,
                expected_revision=checkpoint.revision,
                phase=ResearchPhase.PROTOCOL_FREEZE,
                checkpointed_at="2026-09-17T17:03:00Z",
                bindings={
                    "protocol_id": "protocol-1",
                    "protocol_sha256": "d" * 64,
                },
                next_action=ResearchPhase.DATASET_SNAPSHOT.value,
            )

            with self.assertRaisesRegex(
                InvalidResearchTransitionError,
                "protocol_sha256 cannot be rewritten",
            ):
                store.advance(
                    checkpoint.run_id,
                    expected_revision=checkpoint.revision,
                    phase=ResearchPhase.DATASET_SNAPSHOT,
                    checkpointed_at="2026-09-17T17:04:00Z",
                    bindings={
                        "protocol_sha256": "e" * 64,
                        "dataset_snapshot_id": "dataset-1",
                    },
                )

    def test_experiment_chain_requires_negative_memory_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research_supervisor.json"
            store = ResearchSupervisorStore.initialize_pristine(path)
            checkpoint = store.start_or_resume(
                self._trigger(),
                self._controls(),
                checkpointed_at="2026-09-17T17:00:01Z",
            )
            checkpoint = store.advance(
                checkpoint.run_id,
                expected_revision=checkpoint.revision,
                phase=ResearchPhase.HYPOTHESIS,
                checkpointed_at="2026-09-17T17:01:00Z",
                bindings={"hypothesis_id": "h-1"},
            )

            with self.assertRaisesRegex(ValueError, "negative_memory_check_sha256"):
                store.advance(
                    checkpoint.run_id,
                    expected_revision=checkpoint.revision,
                    phase=ResearchPhase.SOURCE_SEARCH,
                    checkpointed_at="2026-09-17T17:02:00Z",
                    bindings={"source_search_sha256": "b" * 64},
                )

    def test_stale_revision_cannot_double_commit_a_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research_supervisor.json"
            store = ResearchSupervisorStore.initialize_pristine(path)
            checkpoint = store.start_or_resume(
                self._trigger(),
                self._controls(),
                checkpointed_at="2026-09-17T17:00:01Z",
            )
            stale_revision = checkpoint.revision
            committed = store.advance(
                checkpoint.run_id,
                expected_revision=stale_revision,
                phase=ResearchPhase.HYPOTHESIS,
                checkpointed_at="2026-09-17T17:01:00Z",
                bindings={"hypothesis_id": "h-1"},
            )
            self.assertEqual(committed.revision, stale_revision + 1)

            with self.assertRaises(StaleSupervisorCheckpointError):
                store.advance(
                    checkpoint.run_id,
                    expected_revision=stale_revision,
                    phase=ResearchPhase.HYPOTHESIS,
                    checkpointed_at="2026-09-17T17:01:01Z",
                    bindings={"hypothesis_id": "h-1"},
                )

    def test_pause_resume_cancel_and_budget_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research_supervisor.json"
            store = ResearchSupervisorStore.initialize_pristine(path)
            checkpoint = store.start_or_resume(
                self._trigger(),
                self._controls(budget="1"),
                checkpointed_at="2026-09-17T17:00:01Z",
            )
            checkpoint = store.set_paused(
                checkpoint.run_id,
                expected_revision=checkpoint.revision,
                paused=True,
                checkpointed_at="2026-09-17T17:00:30Z",
                blocker="operator STOP",
            )
            with self.assertRaisesRegex(ResearchRunStoppedError, "paused"):
                store.advance(
                    checkpoint.run_id,
                    expected_revision=checkpoint.revision,
                    phase=ResearchPhase.HYPOTHESIS,
                    checkpointed_at="2026-09-17T17:01:00Z",
                    bindings={"hypothesis_id": "h-1"},
                )

            checkpoint = store.set_paused(
                checkpoint.run_id,
                expected_revision=checkpoint.revision,
                paused=False,
                checkpointed_at="2026-09-17T17:01:01Z",
            )
            with self.assertRaisesRegex(ResearchRunStoppedError, "budget"):
                store.advance(
                    checkpoint.run_id,
                    expected_revision=checkpoint.revision,
                    phase=ResearchPhase.HYPOTHESIS,
                    checkpointed_at="2026-09-17T17:01:02Z",
                    bindings={"hypothesis_id": "h-1"},
                    budget_delta=Decimal("1.01"),
                )

            checkpoint = store.cancel(
                checkpoint.run_id,
                expected_revision=checkpoint.revision,
                checkpointed_at="2026-09-17T17:01:03Z",
                reason="owner cancelled",
            )
            with self.assertRaisesRegex(ResearchRunStoppedError, "cancelled"):
                store.advance(
                    checkpoint.run_id,
                    expected_revision=checkpoint.revision,
                    phase=ResearchPhase.HYPOTHESIS,
                    checkpointed_at="2026-09-17T17:01:04Z",
                    bindings={"hypothesis_id": "h-1"},
                )

    def test_deadline_blocks_progress_but_duplicate_trigger_stays_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research_supervisor.json"
            store = ResearchSupervisorStore.initialize_pristine(path)
            trigger = self._trigger()
            controls = self._controls(deadline="2026-09-17T17:05:00Z")
            checkpoint = store.start_or_resume(
                trigger,
                controls,
                checkpointed_at="2026-09-17T17:00:01Z",
            )

            duplicate = store.start_or_resume(
                trigger,
                controls,
                checkpointed_at="2026-09-17T17:10:00Z",
            )
            self.assertEqual(duplicate, checkpoint)
            with self.assertRaisesRegex(ResearchRunStoppedError, "deadline"):
                store.advance(
                    checkpoint.run_id,
                    expected_revision=checkpoint.revision,
                    phase=ResearchPhase.HYPOTHESIS,
                    checkpointed_at="2026-09-17T17:10:00Z",
                    bindings={"hypothesis_id": "h-1"},
                )
            with self.assertRaisesRegex(ResearchRunStoppedError, "deadline"):
                store.start_or_resume(
                    self._trigger(trigger_id="timer:new", question_id="q-new"),
                    controls,
                    checkpointed_at="2026-09-17T17:10:00Z",
                )

    def test_status_is_operator_readable_without_becoming_domain_truth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "research_supervisor.json"
            store = ResearchSupervisorStore.initialize_pristine(path)
            checkpoint = store.start_or_resume(
                self._trigger(),
                self._controls(budget="7.5"),
                checkpointed_at="2026-09-17T17:00:01Z",
            )
            checkpoint = store.advance(
                checkpoint.run_id,
                expected_revision=checkpoint.revision,
                phase=ResearchPhase.HYPOTHESIS,
                checkpointed_at="2026-09-17T17:01:00Z",
                bindings={"hypothesis_id": "h-1"},
                budget_delta="0.25",
                next_action=ResearchPhase.SOURCE_SEARCH.value,
            )

            status = store.status(checkpoint.run_id)
            self.assertEqual(status.run_id, checkpoint.run_id)
            self.assertEqual(status.phase, ResearchPhase.HYPOTHESIS)
            self.assertEqual(status.question_id, "q-1")
            self.assertEqual(status.hypothesis_id, "h-1")
            self.assertEqual(status.budget_spent, Decimal("0.25"))
            self.assertEqual(status.budget_limit, Decimal("7.5"))
            self.assertEqual(status.next_action, ResearchPhase.SOURCE_SEARCH.value)


if __name__ == "__main__":
    unittest.main()
