from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


_BASE_PATH = Path(__file__).with_name("_paper_campaign_runtime_tests_base.py")
_SPEC = importlib.util.spec_from_file_location(
    "_paper_campaign_runtime_tests_base",
    _BASE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("cannot load campaign runtime regression base")
_legacy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_legacy)


class PaperCampaignRuntimeTests(_legacy.PaperCampaignRuntimeTests):
    def test_late_reflection_semantics_keep_actual_first_availability(self) -> None:
        """Precommitted semantics become usable when the reward becomes available."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = _legacy.ScientificRegistry.initialize_pristine(
                root / "scientific_registry.json"
            )
            supervisor = _legacy.ResearchSupervisor.initialize_pristine(
                root / "research_supervisor.json", registry
            )
            plan = _legacy.PaperReflectionPlan(
                summary_code="PRECOMMITTED_REFLECTION",
                reason_code="PRECOMMITTED_REASON",
                research_question_statement="What explains this PAPER review?",
                research_budget_units=2,
                research_deadline_at="2026-09-20T04:00:00+00:00",
            )
            (
                leg,
                _book,
                ticket_id,
                _decision,
                _environment,
                _baseline,
                _observation,
                bridge,
                runtime,
            ) = _legacy._fixture(
                root,
                reflection_plan=plan,
                research_supervisor=supervisor,
            )
            resolutions = _legacy._settle(root, leg, "win")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=_legacy.T4,
            )

            first = runtime.finalize_ticket(ticket_id=ticket_id, at=_legacy.T5)
            second = runtime.finalize_ticket(ticket_id=ticket_id, at=_legacy.T6)
            self.assertEqual(first, second)

            state = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            # The reflection policy was already bound into the Action at T2.
            # Reward truth first exists at T4, so T4 is the first causal time at
            # which attribution/postmortem may use both pieces of evidence.
            self.assertEqual(state["attributions"][0]["attributed_at"], _legacy.T4)
            self.assertEqual(
                state["attributions"][0]["findings"][0]["evidence_available_at"],
                _legacy.T4,
            )
            self.assertEqual(state["postmortems"][0]["created_at"], _legacy.T4)
            # The external research request is still made at the real T5 call.
            self.assertEqual(state["research_handoffs"][0]["requested_at"], _legacy.T5)
            durable = json.loads(
                (root / "paper-learning-bridge.json.campaign.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                durable["plans"][ticket_id]["reflection_available_at"],
                _legacy.T4,
            )

    def test_deleted_campaign_sidecar_cannot_replace_anchored_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _legacy.PaperReflectionPlan(summary_code="ANCHORED_PLAN")
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
            ) = _legacy._fixture(root, reflection_plan=plan)
            resolutions = _legacy._settle(root, leg, "win")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=_legacy.T4,
            )
            witness = bridge.resolution_witness(ticket_id)
            frozen_at = runtime._bind_finalization_plan(
                witness,
                available_at=_legacy.T5,
            )
            self.assertEqual(frozen_at, _legacy.T4)
            runtime.state_path.unlink()

            resumed_environment = _legacy.CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(environment.episode.admissible_actions),
                checkpoint=baseline,
            )
            recovered_bridge = _legacy.PaperSettlementLearningBridge(
                root / "paper-learning-bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=_legacy.JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=_legacy.AgentLoopRuntime(root / "agent-loop.json"),
                economic_goal=bridge.economic_goal,
                risk_policy=bridge.risk_policy,
            )
            conflicting = _legacy.PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=recovered_bridge,
                reflection_plan=_legacy.PaperReflectionPlan(
                    summary_code="REPLACEMENT_PLAN"
                ),
            )
            with self.assertRaisesRegex(
                _legacy.PaperCampaignRuntimeError,
                "pre-settlement reflection commitment",
            ):
                conflicting.finalize_ticket(ticket_id=ticket_id, at=_legacy.T6)

            raw = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(raw["phase"], _legacy.AgentLoopPhase.EVALUATE.value)
            self.assertEqual(raw["attributions"], [])

            same_plan = _legacy.PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=recovered_bridge,
                reflection_plan=plan,
            )
            same_plan.finalize_ticket(ticket_id=ticket_id, at=_legacy.T6)
            raw = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(raw["attributions"][0]["attributed_at"], _legacy.T4)
            durable = json.loads(
                same_plan.state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                durable["plans"][ticket_id]["reflection_available_at"],
                _legacy.T4,
            )

    def test_paired_bridge_and_campaign_rollback_cannot_substitute_plan(self) -> None:
        """Reproduce the exact valid-state rollback attack from the independent audit."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _legacy.PaperReflectionPlan(summary_code="PLAN_A")
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
            ) = _legacy._fixture(root, reflection_plan=plan)
            resolutions = _legacy._settle(root, leg, "win")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=_legacy.T4,
            )

            bridge_path = root / "paper-learning-bridge.json"
            campaign_path = root / "paper-learning-bridge.json.campaign.json"
            preplan_bridge = bridge_path.read_bytes()
            preplan_campaign = campaign_path.read_bytes()

            witness = bridge.resolution_witness(ticket_id)
            self.assertEqual(
                runtime._bind_finalization_plan(witness, available_at=_legacy.T5),
                _legacy.T4,
            )

            # Crash before AgentLoop attribution, then restore both individually
            # valid post-settlement/pre-plan snapshots.  The old implementation
            # lost every trace of PLAN_A here and accepted a later PLAN_B.
            bridge_path.write_bytes(preplan_bridge)
            campaign_path.write_bytes(preplan_campaign)

            resumed_environment = _legacy.CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(environment.episode.admissible_actions),
                checkpoint=baseline,
            )
            restored_bridge = _legacy.PaperSettlementLearningBridge(
                bridge_path,
                paper_book_path=root / "paper_book.json",
                decision_ledger=_legacy.JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=_legacy.AgentLoopRuntime(root / "agent-loop.json"),
                economic_goal=bridge.economic_goal,
                risk_policy=bridge.risk_policy,
            )
            changed = _legacy.PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=restored_bridge,
                reflection_plan=_legacy.PaperReflectionPlan(summary_code="PLAN_B"),
            )
            with self.assertRaisesRegex(
                _legacy.PaperCampaignRuntimeError,
                "pre-settlement reflection commitment",
            ):
                changed.finalize_ticket(ticket_id=ticket_id, at=_legacy.T6)

            untouched = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(untouched["phase"], _legacy.AgentLoopPhase.EVALUATE.value)
            self.assertEqual(untouched["attributions"], [])

            exact = _legacy.PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=restored_bridge,
                reflection_plan=plan,
            )
            exact.finalize_ticket(ticket_id=ticket_id, at=_legacy.T6)
            final_state = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(final_state["attributions"][0]["attributed_at"], _legacy.T4)
            durable = json.loads(campaign_path.read_text(encoding="utf-8"))
            self.assertEqual(
                durable["plans"][ticket_id]["reflection_available_at"],
                _legacy.T4,
            )


if __name__ == "__main__":
    _legacy.unittest.main()
