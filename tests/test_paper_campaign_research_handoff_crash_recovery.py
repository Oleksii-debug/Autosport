from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.agent_loop import AgentLoopError, AgentLoopPhase, AgentLoopRuntime
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.learning_environment import CausalLearningEnvironment
from autosport.paper_campaign_runtime import PaperCampaignRuntime, PaperReflectionPlan
from autosport.paper_settlement_learning import PaperSettlementLearningBridge
from autosport.research_supervisor import ResearchSupervisor
from autosport.research_trigger_adapter import ResearchTriggerAdapter
from autosport.scientific_registry import ScientificRegistry


_BASE_PATH = Path(__file__).with_name("_paper_campaign_runtime_tests_base.py")
_SPEC = importlib.util.spec_from_file_location(
    "_paper_campaign_runtime_tests_base_r01",
    _BASE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("cannot load campaign runtime regression base")
_legacy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_legacy)


class PaperCampaignResearchHandoffCrashRecoveryTests(unittest.TestCase):
    def test_restart_after_supervisor_accept_before_agentloop_handoff_is_exactly_once(
        self,
    ) -> None:
        """A durable research run must not fork if AgentLoop crashes before its ack."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ScientificRegistry.initialize_pristine(
                root / "scientific_registry.json"
            )
            supervisor = ResearchSupervisor.initialize_pristine(
                root / "research_supervisor.json",
                registry,
            )
            plan = PaperReflectionPlan(
                summary_code="R01_XSTORE_CRASH",
                reason_code="R01_RESEARCH_HANDOFF",
                research_question_statement=(
                    "Why did this settled PAPER decision remain unresolved?"
                ),
                research_budget_units=3,
                research_deadline_at="2026-09-20T04:00:00+00:00",
            )
            (
                leg,
                _book,
                ticket_id,
                _decision,
                environment,
                baseline,
                _observation,
                bridge,
                runtime,
            ) = _legacy._fixture(
                root,
                reflection_plan=plan,
                research_supervisor=supervisor,
            )
            resolutions = _legacy._settle(root, leg, "loss")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=_legacy.T4,
            )

            durable_receipts = []
            real_accept = ResearchTriggerAdapter.accept

            def accept_then_crash(adapter, event):
                receipt = real_accept(adapter, event)
                durable_receipts.append(receipt)
                raise RuntimeError(
                    "simulated crash after ResearchSupervisor acceptance"
                )

            with patch.object(
                ResearchTriggerAdapter,
                "accept",
                new=accept_then_crash,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "after ResearchSupervisor acceptance",
                ):
                    runtime.finalize_ticket(ticket_id=ticket_id, at=_legacy.T4)

            self.assertEqual(len(durable_receipts), 1)
            accepted_run_id = durable_receipts[0].run_id
            self.assertEqual(len(supervisor.list_runs()), 1)
            self.assertEqual(supervisor.list_runs()[0].run_id, accepted_run_id)

            loop_after_crash = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                loop_after_crash["phase"],
                AgentLoopPhase.RESEARCH_HANDOFF.value,
            )
            self.assertEqual(loop_after_crash["research_handoffs"], [])
            self.assertIsNone(
                loop_after_crash["current"]["research_question_id"]
            )
            self.assertIsNone(
                loop_after_crash["current"]["research_trigger_id"]
            )
            self.assertIsNone(loop_after_crash["current"]["research_run_id"])

            registry_after_crash = json.loads(
                (root / "scientific_registry.json").read_text(encoding="utf-8")
            )
            questions_after_crash = [
                record
                for record in registry_after_crash["records"]
                if record["record_type"] == "ResearchQuestion"
            ]
            self.assertEqual(len(questions_after_crash), 1)

            reopened_registry = ScientificRegistry(
                root / "scientific_registry.json"
            )
            reopened_supervisor = ResearchSupervisor(
                root / "research_supervisor.json",
                reopened_registry,
            )
            reopened_loop = AgentLoopRuntime(root / "agent-loop.json")
            reopened_bridge = PaperSettlementLearningBridge(
                root / "paper-learning-bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=reopened_loop,
                economic_goal=bridge.economic_goal,
                risk_policy=bridge.risk_policy,
            )
            resumed_environment = CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(
                    environment.episode.admissible_actions
                ),
                checkpoint=baseline,
            )
            recovered = PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=reopened_bridge,
                reflection_plan=plan,
                research_supervisor=reopened_supervisor,
            )

            # The accepted supervisor run predates the configured deadline, but
            # the process itself recovers only after expiry.  This is the exact
            # cross-store crash window: AgentLoop has no durable research_run_id
            # yet, so recovery must prove/reuse the already-published run rather
            # than either rejecting it or creating a new late run.
            first = recovered.finalize_ticket(
                ticket_id=ticket_id,
                at="2026-09-20T04:00:01+00:00",
            )
            second = recovered.finalize_ticket(
                ticket_id=ticket_id,
                at="2026-09-20T04:00:02+00:00",
            )

            self.assertEqual(first, second)
            self.assertEqual(first.research_run_id, accepted_run_id)
            runs = reopened_supervisor.list_runs()
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].run_id, accepted_run_id)

            final_loop = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                final_loop["phase"],
                AgentLoopPhase.CHECKPOINT.value,
            )
            self.assertEqual(len(final_loop["research_handoffs"]), 1)
            self.assertEqual(
                final_loop["research_handoffs"][0]["run_id"],
                accepted_run_id,
            )
            self.assertEqual(
                final_loop["current"]["research_run_id"],
                accepted_run_id,
            )

            final_registry = json.loads(
                (root / "scientific_registry.json").read_text(encoding="utf-8")
            )
            final_questions = [
                record
                for record in final_registry["records"]
                if record["record_type"] == "ResearchQuestion"
            ]
            self.assertEqual(len(final_questions), 1)
            self.assertEqual(
                final_questions[0]["record_id"],
                questions_after_crash[0]["record_id"],
            )


    def test_postdeadline_retry_cannot_create_run_missing_at_crash(self) -> None:
        """Late recovery must not turn an unaccepted request into a new run."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ScientificRegistry.initialize_pristine(
                root / "scientific_registry.json"
            )
            supervisor = ResearchSupervisor.initialize_pristine(
                root / "research_supervisor.json",
                registry,
            )
            plan = PaperReflectionPlan(
                summary_code="R01_XSTORE_NEGATIVE",
                reason_code="R01_RESEARCH_DEADLINE",
                research_question_statement=(
                    "Why did this settled PAPER decision remain unresolved?"
                ),
                research_budget_units=3,
                research_deadline_at="2026-09-20T04:00:00+00:00",
            )
            (
                leg,
                _book,
                ticket_id,
                _decision,
                environment,
                baseline,
                _observation,
                bridge,
                runtime,
            ) = _legacy._fixture(
                root,
                reflection_plan=plan,
                research_supervisor=supervisor,
            )
            resolutions = _legacy._settle(root, leg, "loss")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=_legacy.T4,
            )

            # AgentLoop publishes the deterministic ResearchQuestion before the
            # adapter boundary.  Crash there leaves a real question but no
            # supervisor acceptance; a restart after the deadline must not
            # manufacture the missing external run.
            with patch.object(
                ResearchTriggerAdapter,
                "accept",
                side_effect=RuntimeError(
                    "simulated crash before ResearchSupervisor acceptance"
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "before ResearchSupervisor acceptance",
                ):
                    runtime.finalize_ticket(ticket_id=ticket_id, at=_legacy.T4)

            self.assertEqual(supervisor.list_runs(), ())
            registry_after_crash = json.loads(
                (root / "scientific_registry.json").read_text(encoding="utf-8")
            )
            questions_after_crash = [
                record
                for record in registry_after_crash["records"]
                if record["record_type"] == "ResearchQuestion"
            ]
            self.assertEqual(len(questions_after_crash), 1)

            reopened_registry = ScientificRegistry(
                root / "scientific_registry.json"
            )
            reopened_supervisor = ResearchSupervisor(
                root / "research_supervisor.json",
                reopened_registry,
            )
            reopened_loop = AgentLoopRuntime(root / "agent-loop.json")
            reopened_bridge = PaperSettlementLearningBridge(
                root / "paper-learning-bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=reopened_loop,
                economic_goal=bridge.economic_goal,
                risk_policy=bridge.risk_policy,
            )
            resumed_environment = CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(
                    environment.episode.admissible_actions
                ),
                checkpoint=baseline,
            )
            recovered = PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=reopened_bridge,
                reflection_plan=plan,
                research_supervisor=reopened_supervisor,
            )

            with self.assertRaisesRegex(
                AgentLoopError,
                "deadline expired before durable supervisor acceptance",
            ):
                recovered.finalize_ticket(
                    ticket_id=ticket_id,
                    at="2026-09-20T04:00:01+00:00",
                )

            self.assertEqual(reopened_supervisor.list_runs(), ())
            final_loop = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                final_loop["phase"],
                AgentLoopPhase.RESEARCH_HANDOFF.value,
            )
            self.assertEqual(final_loop["research_handoffs"], [])
            final_registry = json.loads(
                (root / "scientific_registry.json").read_text(encoding="utf-8")
            )
            final_questions = [
                record
                for record in final_registry["records"]
                if record["record_type"] == "ResearchQuestion"
            ]
            self.assertEqual(len(final_questions), 1)
            self.assertEqual(
                final_questions[0]["record_id"],
                questions_after_crash[0]["record_id"],
            )


if __name__ == "__main__":
    unittest.main()
