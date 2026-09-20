from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from unittest import mock


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
            after_deadline = runtime.finalize_ticket(
                ticket_id=ticket_id,
                at="2026-09-20T04:00:01+00:00",
            )
            self.assertEqual(first, second)
            self.assertEqual(first, after_deadline)

            state = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            # The reflection policy was already bound into the Action at T2.
            # Reward truth first exists at T4, so T4 is the first causal time at
            # which attribution/postmortem may use both pieces of evidence.
            self.assertEqual(
                state["attributions"][0]["attributed_at"],
                _legacy.T4.replace("+00:00", "Z"),
            )
            self.assertEqual(
                state["attributions"][0]["findings"][0]["evidence_available_at"],
                _legacy.T4.replace("+00:00", "Z"),
            )
            self.assertEqual(
                state["postmortems"][0]["created_at"],
                _legacy.T4.replace("+00:00", "Z"),
            )
            # Research identity is causally anchored to the postmortem at T4;
            # the later T5 API call is only the mutation/retry clock.
            self.assertEqual(
                state["research_handoffs"][0]["requested_at"],
                _legacy.T4.replace("+00:00", "Z"),
            )
            self.assertEqual(len(state["research_handoffs"]), 1)
            durable = json.loads(
                (root / "paper-learning-bridge.json.campaign.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                durable["plans"][ticket_id]["reflection_available_at"],
                _legacy.T4.replace("+00:00", "Z"),
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
            self.assertEqual(
                frozen_at,
                _legacy.T4.replace("+00:00", "Z"),
            )
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
            self.assertEqual(
                raw["attributions"][0]["attributed_at"],
                _legacy.T4.replace("+00:00", "Z"),
            )
            durable = json.loads(
                same_plan.state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                durable["plans"][ticket_id]["reflection_available_at"],
                _legacy.T4.replace("+00:00", "Z"),
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
                _legacy.T4.replace("+00:00", "Z"),
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
            self.assertEqual(
                final_state["attributions"][0]["attributed_at"],
                _legacy.T4.replace("+00:00", "Z"),
            )
            durable = json.loads(campaign_path.read_text(encoding="utf-8"))
            self.assertEqual(
                durable["plans"][ticket_id]["reflection_available_at"],
                _legacy.T4.replace("+00:00", "Z"),
            )

    def test_post_deadline_restart_after_durable_handoff_commits_checkpoint(self) -> None:
        """A durable research handoff survives a crash before checkpoint commit."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = _legacy.ScientificRegistry.initialize_pristine(
                root / "scientific_registry.json"
            )
            supervisor = _legacy.ResearchSupervisor.initialize_pristine(
                root / "research_supervisor.json", registry
            )
            plan = _legacy.PaperReflectionPlan(
                summary_code="RESTART_AFTER_HANDOFF",
                reason_code="RESTART_AFTER_HANDOFF_REASON",
                research_question_statement="What explains this PAPER review?",
                research_budget_units=2,
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
            resolutions = _legacy._settle(root, leg, "win")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=_legacy.T4,
            )

            with mock.patch.object(
                _legacy.AgentLoopRuntime,
                "commit_checkpoint",
                autospec=True,
                side_effect=RuntimeError("simulated crash before checkpoint commit"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash before checkpoint commit",
                ):
                    runtime.finalize_ticket(ticket_id=ticket_id, at=_legacy.T5)

            crashed = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(crashed["phase"], _legacy.AgentLoopPhase.CHECKPOINT.value)
            self.assertEqual(len(crashed["research_handoffs"]), 1)
            self.assertIsNotNone(crashed["research_handoffs"][0]["run_id"])

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
            recovered = _legacy.PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=recovered_bridge,
                reflection_plan=plan,
                research_supervisor=supervisor,
            )
            receipt = recovered.finalize_ticket(
                ticket_id=ticket_id,
                at="2026-09-20T04:00:01+00:00",
            )

            final_state = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(final_state["research_handoffs"]), 1)
            self.assertEqual(
                final_state["environment_checkpoint_id"],
                receipt.checkpoint_id,
            )
            self.assertEqual(
                final_state["checkpointed_transition_id"],
                receipt.transition_id,
            )


    def _abstention_runtime(self, root: Path):
        goal = _legacy.EconomicGoalContract(
            goal_id="abstention-goal",
            revision=1,
            bankroll_id="abstention-bankroll",
            currency="USD",
        )
        risk = _legacy.PaperRiskPolicy(economic_goal=goal)
        book = _legacy.PaperBook("100")
        book.save(root / "paper_book.json")
        identity = _legacy.EnvironmentIdentity(
            source_id="abstention-source",
            config_id="abstention-config",
            data_id="abstention-data",
            protocol_id="abstention-protocol",
            cutoff_ts="2026-09-20T03:01:00+00:00",
            seed=23,
        )
        environment = _legacy.CausalLearningEnvironment(
            identity,
            episode_key="abstention-episode",
            policy_id="abstention-policy",
            admissible_actions=frozenset({"ABSTAIN", "PAPER_PROPOSAL"}),
        )
        baseline = environment.checkpoint()
        loop = _legacy.AgentLoopRuntime.initialize_pristine(
            root / "agent-loop.json",
            loop_id="abstention-loop",
            environment_checkpoint=baseline,
            policy_id=environment.episode.policy_id,
            economic_goal_fingerprint=_legacy.provenance_for(goal).contract_sha256,
            risk_fingerprint=risk.provenance_sha256,
            source_sha256="d" * 64,
            config_sha256="e" * 64,
            at=_legacy.T0,
        )
        ledger = _legacy.JsonlDecisionLedger(root / "decisions.jsonl")
        bridge = _legacy.PaperSettlementLearningBridge(
            root / "paper-learning-bridge.json",
            paper_book_path=root / "paper_book.json",
            decision_ledger=ledger,
            agent_loop=loop,
            economic_goal=goal,
            risk_policy=risk,
        )
        runtime = _legacy.PaperCampaignRuntime(
            environment=environment,
            settlement_bridge=bridge,
        )
        observation = _legacy.Observation(
            environment_id=environment.environment_id,
            observed_at=_legacy.T0,
            available_at=_legacy.T1,
            evidence=(("market_state", "no-edge-snapshot"),),
        )
        return goal, risk, environment, baseline, observation, ledger, runtime

    def test_abstention_closes_without_ticket_settlement_or_fake_reward(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                goal,
                risk,
                environment,
                baseline,
                observation,
                ledger,
                runtime,
            ) = self._abstention_runtime(root)

            first = runtime.commit_abstention(
                observation=observation,
                decision_at=_legacy.T2,
                reason_code="NO_ADMISSIBLE_EDGE",
                at=_legacy.T2,
                parameters=(("policy_signal", "below-threshold"),),
            )
            self.assertTrue(first.newly_committed)
            self.assertEqual(first.observation_id, observation.observation_id)
            self.assertEqual(first.checkpoint_id, baseline.checkpoint_id)
            self.assertEqual(environment.checkpoint().checkpoint_id, baseline.checkpoint_id)

            raw = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["phase"], _legacy.AgentLoopPhase.CHECKPOINT.value)
            self.assertEqual(len(raw["decisions"]), 1)
            self.assertEqual(raw["decisions"][0]["action_type"], "ABSTAIN")
            self.assertFalse(raw["decisions"][0]["may_execute"])
            self.assertEqual(raw["decisions"][0]["external_effect_state"], "NONE")
            self.assertEqual(raw["resolutions"], [])
            self.assertEqual(raw["attributions"], [])
            self.assertEqual(raw["postmortems"], [])
            self.assertEqual(raw["checkpointed_transition_id"], None)
            campaign_state = json.loads(
                (root / "paper-learning-bridge.json.campaign.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(campaign_state["plans"], {})

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
                decision_ledger=ledger,
                agent_loop=_legacy.AgentLoopRuntime(root / "agent-loop.json"),
                economic_goal=goal,
                risk_policy=risk,
            )
            recovered = _legacy.PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=recovered_bridge,
            )
            exact = recovered.commit_abstention(
                observation=observation,
                decision_at=_legacy.T2,
                reason_code="NO_ADMISSIBLE_EDGE",
                at=_legacy.T4,
                parameters=(("policy_signal", "below-threshold"),),
            )
            self.assertFalse(exact.newly_committed)
            self.assertEqual(exact.action_id, first.action_id)

            before_conflict = (root / "agent-loop.json").read_bytes()
            with self.assertRaisesRegex(
                _legacy.PaperCampaignRuntimeError,
                "rejected campaign abstention",
            ):
                recovered.commit_abstention(
                    observation=observation,
                    decision_at=_legacy.T2,
                    reason_code="CHANGED_REASON",
                    at=_legacy.T5,
                    parameters=(("policy_signal", "below-threshold"),),
                )
            self.assertEqual(
                (root / "agent-loop.json").read_bytes(),
                before_conflict,
            )

            next_observation = _legacy.Observation(
                environment_id=resumed_environment.environment_id,
                observed_at=_legacy.T4,
                available_at=_legacy.T4,
                evidence=(("market_state", "second-no-edge-snapshot"),),
            )
            second = recovered.commit_abstention(
                observation=next_observation,
                decision_at=_legacy.T5,
                reason_code="NO_ADMISSIBLE_EDGE",
                at=_legacy.T5,
            )
            self.assertTrue(second.newly_committed)
            self.assertEqual(second.checkpoint_id, baseline.checkpoint_id)
            final = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(len(final["decisions"]), 2)
            self.assertEqual(final["resolutions"], [])
            self.assertEqual(final["attributions"], [])
            self.assertEqual(final["postmortems"], [])


if __name__ == "__main__":
    _legacy.unittest.main()
