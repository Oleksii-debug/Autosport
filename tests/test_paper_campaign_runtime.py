from __future__ import annotations

import importlib.util
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from autosport.learning_environment import EvidenceTruth, Outcome, RewardEvidence


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


    def test_canonical_runtime_abstention_reaches_checkpoint_without_ticket(self) -> None:
        """WAIT is reachable from PaperCampaignRuntime without settlement authority."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            goal = _legacy.EconomicGoalContract(
                goal_id="campaign-abstention-goal",
                revision=1,
                bankroll_id="campaign-abstention-bankroll",
                currency="USD",
            )
            risk = _legacy.PaperRiskPolicy(economic_goal=goal)
            paper_path = root / "paper_book.json"
            _legacy.PaperBook("100").save(paper_path)

            identity = _legacy.EnvironmentIdentity(
                source_id="campaign-abstention-source",
                config_id="campaign-abstention-config",
                data_id="campaign-abstention-data",
                protocol_id="campaign-abstention-protocol",
                cutoff_ts="2026-09-20T04:00:00+00:00",
                seed=41,
            )
            environment = _legacy.CausalLearningEnvironment(
                identity,
                episode_key="campaign-abstention-episode",
                policy_id="campaign-abstention-policy",
                admissible_actions=frozenset({"WAIT", "NO_BET"}),
            )
            observation = _legacy.Observation(
                environment_id=environment.environment_id,
                observed_at=_legacy.T0,
                available_at=_legacy.T1,
                evidence=(("opportunity", "no-authorized-paper-action"),),
            )
            baseline = environment.checkpoint()
            loop = _legacy.AgentLoopRuntime.initialize_pristine(
                root / "agent-loop.json",
                loop_id="campaign-abstention-loop",
                environment_checkpoint=baseline,
                policy_id=environment.episode.policy_id,
                economic_goal_fingerprint=_legacy.provenance_for(goal).contract_sha256,
                risk_fingerprint=risk.provenance_sha256,
                source_sha256="a" * 64,
                config_sha256="b" * 64,
                at=_legacy.T0,
            )
            bridge = _legacy.PaperSettlementLearningBridge(
                root / "paper-learning-bridge.json",
                paper_book_path=paper_path,
                decision_ledger=_legacy.JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=loop,
                economic_goal=goal,
                risk_policy=risk,
            )
            runtime = _legacy.PaperCampaignRuntime(
                environment=environment,
                settlement_bridge=bridge,
            )

            paper_before = paper_path.read_bytes()
            bridge_before = bridge.state_path.read_bytes()
            campaign_before = runtime.state_path.read_bytes()

            action = runtime.begin_abstention(
                observation=observation,
                action_type="WAIT",
                decision_at=_legacy.T2,
                parameters=(("reason", "no-edge"),),
                at=_legacy.T2,
            )
            outcome = Outcome(
                environment_id=environment.environment_id,
                action_id=action.action_id,
                revealed_at=_legacy.T3,
                truth=EvidenceTruth.OBSERVED,
                evidence=(("market_resolution", "no-position-outcome"),),
            )
            reward = RewardEvidence(
                environment_id=environment.environment_id,
                action_id=action.action_id,
                outcome_id=outcome.outcome_id,
                reward=Decimal("0"),
                available_at=_legacy.T4,
                truth=EvidenceTruth.OBSERVED,
                evidence=(("reward_basis", "explicit-no-position-evidence"),),
            )

            receipt = runtime.finalize_abstention(
                observation=observation,
                action=action,
                outcome=outcome,
                reward=reward,
                at=_legacy.T4,
            )

            snapshot = runtime.agent_loop.snapshot()
            self.assertIs(snapshot.phase, _legacy.AgentLoopPhase.CHECKPOINT)
            self.assertEqual(snapshot.environment_checkpoint_id, receipt.checkpoint_id)
            self.assertEqual(snapshot.checkpointed_transition_id, receipt.transition_id)
            self.assertEqual(runtime.environment.checkpoint().checkpoint_id, receipt.checkpoint_id)
            self.assertEqual(paper_path.read_bytes(), paper_before)
            self.assertEqual(bridge.state_path.read_bytes(), bridge_before)
            self.assertEqual(runtime.state_path.read_bytes(), campaign_before)

            durable = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(durable["decisions"]), 1)
            self.assertEqual(len(durable["resolutions"]), 1)
            self.assertEqual(len(durable["attributions"]), 1)
            self.assertEqual(len(durable["postmortems"]), 1)
            self.assertEqual(durable["research_handoffs"], [])



    def test_abstention_helper_rebinds_after_ticket_checkpoint_advances(self) -> None:
        """A ticket finalization cannot leave the next WAIT on a stale environment."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            goal = _legacy.EconomicGoalContract(
                goal_id="mixed-path-goal",
                revision=1,
                bankroll_id="mixed-path-bankroll",
                currency="USD",
            )
            risk = _legacy.PaperRiskPolicy(economic_goal=goal)
            paper_path = root / "paper_book.json"
            _legacy.PaperBook("100").save(paper_path)

            identity = _legacy.EnvironmentIdentity(
                source_id="mixed-path-source",
                config_id="mixed-path-config",
                data_id="mixed-path-data",
                protocol_id="mixed-path-protocol",
                cutoff_ts="2026-09-20T04:00:00+00:00",
                seed=43,
            )
            environment = _legacy.CausalLearningEnvironment(
                identity,
                episode_key="mixed-path-episode",
                policy_id="mixed-path-policy",
                admissible_actions=frozenset({"WAIT", "NO_BET", "PAPER_PROPOSAL"}),
            )
            baseline = environment.checkpoint()
            loop = _legacy.AgentLoopRuntime.initialize_pristine(
                root / "agent-loop.json",
                loop_id="mixed-path-loop",
                environment_checkpoint=baseline,
                policy_id=environment.episode.policy_id,
                economic_goal_fingerprint=_legacy.provenance_for(goal).contract_sha256,
                risk_fingerprint=risk.provenance_sha256,
                source_sha256="a" * 64,
                config_sha256="b" * 64,
                at=_legacy.T0,
            )
            ledger = _legacy.JsonlDecisionLedger(root / "decisions.jsonl")
            bridge = _legacy.PaperSettlementLearningBridge(
                root / "paper-learning-bridge.json",
                paper_book_path=paper_path,
                decision_ledger=ledger,
                agent_loop=loop,
                economic_goal=goal,
                risk_policy=risk,
            )
            runtime = _legacy.PaperCampaignRuntime(
                environment=environment,
                settlement_bridge=bridge,
            )

            first_observation = _legacy.Observation(
                environment_id=environment.environment_id,
                observed_at=_legacy.T0,
                available_at=_legacy.T1,
                evidence=(("opportunity", "first-wait"),),
            )
            first_action = runtime.begin_abstention(
                observation=first_observation,
                action_type="WAIT",
                decision_at=_legacy.T2,
                at=_legacy.T2,
            )
            first_outcome = Outcome(
                environment_id=environment.environment_id,
                action_id=first_action.action_id,
                revealed_at=_legacy.T3,
                truth=EvidenceTruth.OBSERVED,
                evidence=(("market_resolution", "first-wait-observed"),),
            )
            first_reward = RewardEvidence(
                environment_id=environment.environment_id,
                action_id=first_action.action_id,
                outcome_id=first_outcome.outcome_id,
                reward=Decimal("0"),
                available_at=_legacy.T4,
                truth=EvidenceTruth.OBSERVED,
                evidence=(("reward_basis", "first-wait-explicit"),),
            )
            first_receipt = runtime.finalize_abstention(
                observation=first_observation,
                action=first_action,
                outcome=first_outcome,
                reward=first_reward,
                at=_legacy.T4,
            )
            stale_helper = runtime._abstention_learning_runtime
            self.assertIs(stale_helper.environment, runtime.environment)
            self.assertEqual(
                runtime.environment.checkpoint().checkpoint_id,
                first_receipt.checkpoint_id,
            )

            leg = _legacy.TicketLeg(
                event_id="mixed-path-event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = _legacy.PaperBook.load(paper_path)
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-20T03:01:10+00:00",
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
            )
            book.save(paper_path)

            ticket_observation = _legacy.Observation(
                environment_id=runtime.environment.environment_id,
                observed_at="2026-09-20T03:01:00+00:00",
                available_at="2026-09-20T03:01:01+00:00",
                evidence=(("market_state", "mixed-ticket"),),
            )
            decision = _legacy.DecisionRecord(
                replay_run_id="mixed-path-run",
                agent="mixed-path-fixture",
                observed_ts=ticket_observation.observed_at,
                action="OPEN_PAPER_TICKET",
                payload={
                    "ticket_id": ticket.ticket_id,
                    "quote_key": leg.quote_key,
                    "stake": str(ticket.stake),
                },
                context_hash=ticket_observation.observation_id,
                decision_id="mixed-path-decision",
                decision_kind=_legacy.ECONOMIC_DECISION_KIND,
            )
            ledger.append_economic(
                decision,
                _legacy.EconomicDecisionAuthority(goal, risk),
            )
            runtime.begin_and_bind_paper_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                observation=ticket_observation,
                action_type="PAPER_PROPOSAL",
                decision_at="2026-09-20T03:01:05+00:00",
                parameters=(
                    ("economic_decision_id", decision.decision_id),
                    ("paper_ticket_id", ticket.ticket_id),
                ),
                at="2026-09-20T03:01:05+00:00",
            )

            settlement = _legacy.SettlementEngine()
            settlement.record({leg.quote_key: "win"})
            settled = tuple(settlement.settle_ready(book))
            self.assertEqual(settled, (ticket.ticket_id,))
            book.save(paper_path)
            resolutions = (
                _legacy.SettlementResolution(
                    event_identity=f"mixed-provider:{leg.event_id}",
                    settlement_ref="mixed-result-1",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="mixed-evidence-1",
                    evidence_sha256="e" * 64,
                    available_at="2026-09-20T03:01:20+00:00",
                ),
            )
            bridge.reconcile_after_settlement(
                paper_book_path=paper_path,
                resolutions=resolutions,
                settled_ticket_ids=settled,
                at="2026-09-20T03:01:20+00:00",
            )
            ticket_receipt = runtime.finalize_ticket(
                ticket_id=ticket.ticket_id,
                at="2026-09-20T03:01:30+00:00",
            )
            ticket_checkpoint = runtime.environment.checkpoint()
            self.assertEqual(ticket_checkpoint.checkpoint_id, ticket_receipt.checkpoint_id)
            self.assertIsNot(stale_helper.environment, runtime.environment)

            next_observation = _legacy.Observation(
                environment_id=runtime.environment.environment_id,
                observed_at="2026-09-20T03:01:40+00:00",
                available_at="2026-09-20T03:01:41+00:00",
                evidence=(("opportunity", "second-wait"),),
            )
            next_action = runtime.begin_abstention(
                observation=next_observation,
                action_type="WAIT",
                decision_at="2026-09-20T03:01:45+00:00",
                at="2026-09-20T03:01:45+00:00",
            )
            rebound_helper = runtime._abstention_learning_runtime
            self.assertIsNot(rebound_helper, stale_helper)
            self.assertIs(rebound_helper.environment, runtime.environment)
            self.assertEqual(
                runtime.agent_loop.snapshot().environment_checkpoint_id,
                ticket_checkpoint.checkpoint_id,
            )

            next_outcome = Outcome(
                environment_id=runtime.environment.environment_id,
                action_id=next_action.action_id,
                revealed_at="2026-09-20T03:01:50+00:00",
                truth=EvidenceTruth.OBSERVED,
                evidence=(("market_resolution", "second-wait-observed"),),
            )
            next_reward = RewardEvidence(
                environment_id=runtime.environment.environment_id,
                action_id=next_action.action_id,
                outcome_id=next_outcome.outcome_id,
                reward=Decimal("0"),
                available_at="2026-09-20T03:02:00+00:00",
                truth=EvidenceTruth.OBSERVED,
                evidence=(("reward_basis", "second-wait-explicit"),),
            )
            second_receipt = runtime.finalize_abstention(
                observation=next_observation,
                action=next_action,
                outcome=next_outcome,
                reward=next_reward,
                at="2026-09-20T03:02:00+00:00",
            )
            final_snapshot = runtime.agent_loop.snapshot()
            self.assertIs(final_snapshot.phase, _legacy.AgentLoopPhase.CHECKPOINT)
            self.assertEqual(
                final_snapshot.environment_checkpoint_id,
                second_receipt.checkpoint_id,
            )
            self.assertNotEqual(
                ticket_checkpoint.checkpoint_id,
                second_receipt.checkpoint_id,
            )


if __name__ == "__main__":
    _legacy.unittest.main()
