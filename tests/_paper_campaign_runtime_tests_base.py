from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime, AttributionComponent
from autosport.continuous_session import SettlementResolution
from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentCheckpoint,
    EnvironmentIdentity,
    Observation,
)
from autosport.paper import PaperBook
from autosport.paper_campaign_runtime import (
    PaperCampaignLearningHandoff,
    PaperCampaignRuntime,
    PaperCampaignRuntimeError,
    PaperReflectionPlan,
)
from autosport.paper_settlement_learning import PaperSettlementLearningBridge
from autosport.research_supervisor import ResearchSupervisor
from autosport.risk import PaperRiskPolicy
from autosport.scientific_registry import ScientificRegistry
from autosport.settlement import SettlementEngine


T0 = "2026-09-20T03:00:00+00:00"
T1 = "2026-09-20T03:00:01+00:00"
T2 = "2026-09-20T03:00:05+00:00"
T3 = "2026-09-20T03:00:10+00:00"
T4 = "2026-09-20T03:00:30+00:00"
T5 = "2026-09-20T03:00:40+00:00"
T6 = "2026-09-20T03:00:50+00:00"


def _fixture(
    root: Path,
    *,
    reflection_plan: PaperReflectionPlan | None = None,
    research_supervisor: ResearchSupervisor | None = None,
) -> tuple[
    TicketLeg,
    PaperBook,
    str,
    DecisionRecord,
    CausalLearningEnvironment,
    EnvironmentCheckpoint,
    Observation,
    PaperSettlementLearningBridge,
    PaperCampaignRuntime,
]:
    goal = EconomicGoalContract(
        goal_id="campaign-goal",
        revision=1,
        bankroll_id="campaign-bankroll",
        currency="USD",
    )
    risk = PaperRiskPolicy(economic_goal=goal)
    leg = TicketLeg(
        event_id="campaign-event-1",
        market_id="winner",
        selection_id="home",
        locked_odds=Decimal("2.00"),
        sport="table_tennis",
    )
    book = PaperBook("100")
    ticket = book.open_ticket(
        (leg,),
        Decimal("10"),
        placed_at=T3,
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
    )
    book.save(root / "paper_book.json")

    identity = EnvironmentIdentity(
        source_id="campaign-source",
        config_id="campaign-config",
        data_id="campaign-data",
        protocol_id="campaign-protocol",
        cutoff_ts="2026-09-20T03:01:00+00:00",
        seed=17,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="campaign-episode",
        policy_id="campaign-policy",
        admissible_actions=frozenset({"PAPER_PROPOSAL"}),
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at=T0,
        available_at=T1,
        evidence=(("market_state", "campaign-snapshot"),),
    )

    ledger = JsonlDecisionLedger(root / "decisions.jsonl")
    decision = DecisionRecord(
        replay_run_id="campaign-run",
        agent="campaign-fixture",
        observed_ts=observation.observed_at,
        action="OPEN_PAPER_TICKET",
        payload={
            "ticket_id": ticket.ticket_id,
            "quote_key": leg.quote_key,
            "stake": str(ticket.stake),
        },
        context_hash=observation.observation_id,
        decision_id="campaign-decision",
        decision_kind=ECONOMIC_DECISION_KIND,
    )
    ledger.append_economic(decision, EconomicDecisionAuthority(goal, risk))

    baseline = environment.checkpoint()
    loop = AgentLoopRuntime.initialize_pristine(
        root / "agent-loop.json",
        loop_id="campaign-loop",
        environment_checkpoint=baseline,
        policy_id=environment.episode.policy_id,
        economic_goal_fingerprint=provenance_for(goal).contract_sha256,
        risk_fingerprint=risk.provenance_sha256,
        source_sha256="a" * 64,
        config_sha256="b" * 64,
        at=T0,
    )
    bridge = PaperSettlementLearningBridge(
        root / "paper-learning-bridge.json",
        paper_book_path=root / "paper_book.json",
        decision_ledger=ledger,
        agent_loop=loop,
        economic_goal=goal,
        risk_policy=risk,
    )
    runtime = PaperCampaignRuntime(
        environment=environment,
        settlement_bridge=bridge,
        reflection_plan=reflection_plan,
        research_supervisor=research_supervisor,
    )
    runtime.begin_and_bind_paper_ticket(
        ticket_id=ticket.ticket_id,
        decision_id=decision.decision_id,
        observation=observation,
        action_type="PAPER_PROPOSAL",
        decision_at=T2,
        parameters=(
            ("economic_decision_id", decision.decision_id),
            ("paper_ticket_id", ticket.ticket_id),
        ),
        at=T2,
    )
    return (
        leg,
        book,
        ticket.ticket_id,
        decision,
        environment,
        baseline,
        observation,
        bridge,
        runtime,
    )


def _settle(root: Path, leg: TicketLeg, outcome: str) -> tuple[SettlementResolution, ...]:
    book = PaperBook.load(root / "paper_book.json")
    settlement = SettlementEngine()
    settlement.record({leg.quote_key: outcome})
    if not settlement.settle_ready(book):
        raise AssertionError("fixture did not settle the ticket")
    book.save(root / "paper_book.json")
    return (
        SettlementResolution(
            event_identity=f"campaign-provider:{leg.event_id}",
            settlement_ref="campaign-result-1",
            quote_outcomes={leg.quote_key: outcome},
            evidence_id="campaign-evidence-1",
            evidence_sha256="c" * 64,
            available_at=T4,
        ),
    )


class PaperCampaignRuntimeTests(unittest.TestCase):
    def test_terminalizes_exact_bridge_witness_without_recomputing_reward(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leg, _book, ticket_id, _decision, _environment, _baseline, _observation, bridge, runtime = _fixture(root)
            resolutions = _settle(root, leg, "win")

            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=T4,
            )
            witness = bridge.resolution_witness(ticket_id)
            receipt = runtime.finalize_ticket(ticket_id=ticket_id, at=T4)

            self.assertEqual(receipt.transition_id, witness.transition.transition_id)
            self.assertEqual(receipt.checkpoint_id, witness.next_checkpoint.checkpoint_id)
            self.assertIs(runtime.agent_loop.snapshot().phase, AgentLoopPhase.CHECKPOINT)
            raw = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(len(raw["resolutions"]), 1)
            self.assertEqual(raw["resolutions"][0]["reward_value"], "10.00")
            self.assertEqual(len(raw["attributions"]), 1)
            self.assertEqual(len(raw["postmortems"]), 1)
            finding = raw["attributions"][0]["findings"][0]
            self.assertEqual(finding["component"], "RANDOMNESS")
            self.assertEqual(finding["status"], "UNKNOWN")
            self.assertIsNone(finding["contribution"])
            self.assertEqual(finding["evidence_sha256"], witness.settlement_bundle_sha256)

    def test_handoff_closes_existing_continuous_settlement_seam(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leg, _book, ticket_id, _decision, _environment, _baseline, _observation, _bridge, runtime = _fixture(root)
            handoff = PaperCampaignLearningHandoff(runtime, ticket_id=ticket_id)
            resolutions = _settle(root, leg, "loss")

            handoff.prepare_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                at=T4,
            )
            transitions = handoff.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=T4,
            )

            self.assertEqual(len(transitions), 1)
            snapshot = runtime.agent_loop.snapshot()
            self.assertIs(snapshot.phase, AgentLoopPhase.CHECKPOINT)
            self.assertEqual(snapshot.checkpointed_transition_id, transitions[0])
            raw = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["resolutions"][0]["reward_value"], "-10")
            self.assertEqual(len(raw["attributions"]), 1)

    def test_restart_after_resolution_converges_without_duplicate_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leg, _book, ticket_id, _decision, environment, baseline, _observation, bridge, _runtime = _fixture(root)
            resolutions = _settle(root, leg, "void")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=T4,
            )
            resumed_environment = CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(environment.episode.admissible_actions),
                checkpoint=baseline,
            )
            reopened_bridge = PaperSettlementLearningBridge(
                root / "paper-learning-bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=AgentLoopRuntime(root / "agent-loop.json"),
                economic_goal=bridge.economic_goal,
                risk_policy=bridge.risk_policy,
            )
            recovered = PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=reopened_bridge,
            )
            first = recovered.finalize_ticket(ticket_id=ticket_id, at=T4)
            second = recovered.finalize_ticket(ticket_id=ticket_id, at=T4)

            self.assertEqual(first, second)
            raw = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(len(raw["resolutions"]), 1)
            self.assertEqual(len(raw["attributions"]), 1)
            self.assertEqual(len(raw["postmortems"]), 1)
            self.assertEqual(raw["resolutions"][0]["reward_value"], "0")

    def test_restart_after_attribution_or_postmortem_converges_exactly_once(self) -> None:
        for boundary in ("attribution", "postmortem"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                leg, _book, ticket_id, _decision, environment, baseline, _observation, bridge, runtime = _fixture(root)
                resolutions = _settle(root, leg, "loss")
                bridge.reconcile_after_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=resolutions,
                    settled_ticket_ids=(ticket_id,),
                    at=T4,
                )
                witness = bridge.resolution_witness(ticket_id)
                runtime.agent_loop.advance(expected=AgentLoopPhase.EVALUATE, at=T4)
                frozen_at = runtime._bind_finalization_plan(
                    witness,
                    available_at=T4,
                )
                attribution = runtime._attribution(
                    witness,
                    available_at=frozen_at,
                )
                runtime.agent_loop.record_attribution(attribution, at=T4)
                if boundary == "postmortem":
                    runtime.agent_loop.record_postmortem(
                        runtime._postmortem(
                            attribution,
                            witness,
                            available_at=frozen_at,
                        ),
                        at=T4,
                    )

                resumed_environment = CausalLearningEnvironment.resume(
                    environment.identity,
                    episode_key=environment.episode.episode_key,
                    policy_id=environment.episode.policy_id,
                    admissible_actions=frozenset(environment.episode.admissible_actions),
                    checkpoint=baseline,
                )
                recovered = PaperCampaignRuntime(
                    environment=resumed_environment,
                    settlement_bridge=PaperSettlementLearningBridge(
                        root / "paper-learning-bridge.json",
                        paper_book_path=root / "paper_book.json",
                        decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                        agent_loop=AgentLoopRuntime(root / "agent-loop.json"),
                        economic_goal=bridge.economic_goal,
                        risk_policy=bridge.risk_policy,
                    ),
                )
                recovered.finalize_ticket(ticket_id=ticket_id, at=T4)

                raw = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
                self.assertEqual(len(raw["resolutions"]), 1)
                self.assertEqual(len(raw["attributions"]), 1)
                self.assertEqual(len(raw["postmortems"]), 1)
                self.assertEqual(raw["phase"], AgentLoopPhase.CHECKPOINT.value)

    def test_research_handoff_is_bounded_and_not_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ScientificRegistry.initialize_pristine(root / "scientific_registry.json")
            supervisor = ResearchSupervisor.initialize_pristine(
                root / "research_supervisor.json", registry
            )
            plan = PaperReflectionPlan(
                research_question_statement="Why is this PAPER reward unresolved?",
                research_budget_units=3,
                research_deadline_at="2026-09-20T04:00:00+00:00",
            )
            leg, _book, ticket_id, _decision, _environment, _baseline, _observation, bridge, runtime = _fixture(
                root,
                reflection_plan=plan,
                research_supervisor=supervisor,
            )
            resolutions = _settle(root, leg, "win")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=T4,
            )
            receipt = runtime.finalize_ticket(ticket_id=ticket_id, at=T4)

            self.assertIsNotNone(receipt.research_run_id)
            state = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["research_handoffs"]), 1)
            research = supervisor.status(receipt.research_run_id or "")
            self.assertEqual(research.budget_units, 3)
            self.assertEqual(research.consumed_budget_units, 0)
            registry_state = json.loads(
                (root / "scientific_registry.json").read_text(encoding="utf-8")
            )
            self.assertFalse(
                any(
                    item["record_type"] == "PromotionDecision"
                    for item in registry_state["records"]
                )
            )

    def test_restart_rejects_substituted_decision_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _leg,
                _book,
                ticket_id,
                decision,
                environment,
                baseline,
                observation,
                bridge,
                _runtime,
            ) = _fixture(root)
            substituted = Observation(
                environment_id=observation.environment_id,
                observed_at=observation.observed_at,
                available_at=observation.available_at,
                evidence=(("market_state", "substituted-snapshot"),),
            )
            resumed_environment = CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(environment.episode.admissible_actions),
                checkpoint=baseline,
            )
            recovered = PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=PaperSettlementLearningBridge(
                    root / "paper-learning-bridge.json",
                    paper_book_path=root / "paper_book.json",
                    decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                    agent_loop=AgentLoopRuntime(root / "agent-loop.json"),
                    economic_goal=bridge.economic_goal,
                    risk_policy=bridge.risk_policy,
                ),
            )
            with self.assertRaisesRegex(
                PaperCampaignRuntimeError,
                "exact causal Observation",
            ):
                recovered.begin_and_bind_paper_ticket(
                    ticket_id=ticket_id,
                    decision_id=decision.decision_id,
                    observation=substituted,
                    action_type="PAPER_PROPOSAL",
                    decision_at=T2,
                    parameters=(
                        ("economic_decision_id", decision.decision_id),
                        ("paper_ticket_id", ticket_id),
                    ),
                    at=T2,
                    baseline_checkpoint=baseline,
                )

    def test_research_bounds_are_frozen_before_handoff_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ScientificRegistry.initialize_pristine(
                root / "scientific_registry.json"
            )
            supervisor = ResearchSupervisor.initialize_pristine(
                root / "research_supervisor.json", registry
            )
            plan = PaperReflectionPlan(
                research_question_statement="Why is this PAPER reward unresolved?",
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
            ) = _fixture(
                root,
                reflection_plan=plan,
                research_supervisor=supervisor,
            )
            resolutions = _settle(root, leg, "loss")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=T4,
            )
            with patch.object(
                AgentLoopRuntime,
                "handoff_research",
                side_effect=RuntimeError("simulated crash before research handoff"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    runtime.finalize_ticket(ticket_id=ticket_id, at=T4)

            state = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["phase"], AgentLoopPhase.RESEARCH_HANDOFF.value)
            self.assertEqual(len(state["postmortems"]), 1)
            durable_plan = json.loads(
                (root / "paper-learning-bridge.json.campaign.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                durable_plan["plans"][ticket_id]["research_budget_units"],
                3,
            )

            resumed_environment = CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(environment.episode.admissible_actions),
                checkpoint=baseline,
            )
            conflicting = PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=PaperSettlementLearningBridge(
                    root / "paper-learning-bridge.json",
                    paper_book_path=root / "paper_book.json",
                    decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                    agent_loop=AgentLoopRuntime(root / "agent-loop.json"),
                    economic_goal=bridge.economic_goal,
                    risk_policy=bridge.risk_policy,
                ),
                reflection_plan=PaperReflectionPlan(
                    research_question_statement="Why is this PAPER reward unresolved?",
                    research_budget_units=4,
                    research_deadline_at="2026-09-20T04:00:00+00:00",
                ),
                research_supervisor=supervisor,
            )
            with self.assertRaisesRegex(
                PaperCampaignRuntimeError,
                "finalization plan conflicts",
            ):
                conflicting.finalize_ticket(ticket_id=ticket_id, at=T4)

    def test_wrong_same_identity_environment_checkpoint_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _leg,
                _book,
                _ticket_id,
                _decision,
                environment,
                baseline,
                _observation,
                bridge,
                _runtime,
            ) = _fixture(root)
            wrong_checkpoint = EnvironmentCheckpoint(
                environment_id=baseline.environment_id,
                episode_id=baseline.episode_id,
                policy_id=baseline.policy_id,
                step_index=1,
                chain_sha256="1" * 64,
                last_transition_id="2" * 64,
                committed_action_ids=("3" * 64,),
                committed_decision_intents=(("4" * 64, "5" * 64),),
            )
            divergent = CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(environment.episode.admissible_actions),
                checkpoint=wrong_checkpoint,
            )
            with self.assertRaisesRegex(PaperCampaignRuntimeError, "checkpoint differs"):
                PaperCampaignRuntime(
                    environment=divergent,
                    settlement_bridge=bridge,
                )

    def test_late_reflection_semantics_keep_actual_first_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ScientificRegistry.initialize_pristine(
                root / "scientific_registry.json"
            )
            supervisor = ResearchSupervisor.initialize_pristine(
                root / "research_supervisor.json", registry
            )
            plan = PaperReflectionPlan(
                summary_code="LATE_REFLECTION",
                reason_code="LATE_REASON",
                research_question_statement="What explains this late PAPER review?",
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
            ) = _fixture(
                root,
                reflection_plan=plan,
                research_supervisor=supervisor,
            )
            resolutions = _settle(root, leg, "win")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=T4,
            )

            first = runtime.finalize_ticket(ticket_id=ticket_id, at=T5)
            second = runtime.finalize_ticket(ticket_id=ticket_id, at=T6)
            self.assertEqual(first, second)

            state = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["attributions"][0]["attributed_at"], T5)
            self.assertEqual(
                state["attributions"][0]["findings"][0]["evidence_available_at"],
                T5,
            )
            self.assertEqual(state["postmortems"][0]["created_at"], T5)
            self.assertEqual(state["research_handoffs"][0]["requested_at"], T5)
            durable = json.loads(
                (root / "paper-learning-bridge.json.campaign.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                durable["plans"][ticket_id]["reflection_available_at"],
                T5,
            )

    def test_deleted_campaign_sidecar_cannot_replace_anchored_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = PaperReflectionPlan(summary_code="ANCHORED_PLAN")
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
            ) = _fixture(root, reflection_plan=plan)
            resolutions = _settle(root, leg, "win")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=T4,
            )
            witness = bridge.resolution_witness(ticket_id)
            frozen_at = runtime._bind_finalization_plan(
                witness,
                available_at=T5,
            )
            self.assertEqual(frozen_at, T5)
            runtime.state_path.unlink()

            resumed_environment = CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(environment.episode.admissible_actions),
                checkpoint=baseline,
            )
            recovered_bridge = PaperSettlementLearningBridge(
                root / "paper-learning-bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=AgentLoopRuntime(root / "agent-loop.json"),
                economic_goal=bridge.economic_goal,
                risk_policy=bridge.risk_policy,
            )
            conflicting = PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=recovered_bridge,
                reflection_plan=PaperReflectionPlan(summary_code="REPLACEMENT_PLAN"),
            )
            with self.assertRaisesRegex(
                PaperCampaignRuntimeError,
                "conflicts with bridge anchor",
            ):
                conflicting.finalize_ticket(ticket_id=ticket_id, at=T6)

            raw = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(raw["phase"], AgentLoopPhase.EVALUATE.value)
            self.assertEqual(raw["attributions"], [])

            same_plan = PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=recovered_bridge,
                reflection_plan=plan,
            )
            same_plan.finalize_ticket(ticket_id=ticket_id, at=T6)
            raw = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(raw["attributions"][0]["attributed_at"], T5)
            durable = json.loads(
                same_plan.state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                durable["plans"][ticket_id]["reflection_available_at"],
                T5,
            )

    def test_late_research_deadline_before_frozen_plan_time_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ScientificRegistry.initialize_pristine(
                root / "scientific_registry.json"
            )
            supervisor = ResearchSupervisor.initialize_pristine(
                root / "research_supervisor.json", registry
            )
            plan = PaperReflectionPlan(
                research_question_statement="Can this late question still run?",
                research_budget_units=1,
                research_deadline_at="2026-09-20T03:00:35+00:00",
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
            ) = _fixture(
                root,
                reflection_plan=plan,
                research_supervisor=supervisor,
            )
            resolutions = _settle(root, leg, "loss")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=T4,
            )
            with self.assertRaisesRegex(
                PaperCampaignRuntimeError,
                "deadline predates frozen reflection availability",
            ):
                runtime.finalize_ticket(ticket_id=ticket_id, at=T5)

            state = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["phase"], AgentLoopPhase.EVALUATE.value)
            self.assertEqual(state["attributions"], [])
            durable = json.loads(
                (root / "paper-learning-bridge.json.campaign.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(durable["plans"], {})

    def test_future_finalization_and_conflicting_reflection_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leg, _book, ticket_id, _decision, environment, baseline, _observation, bridge, _runtime = _fixture(root)
            resolutions = _settle(root, leg, "win")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket_id,),
                at=T4,
            )
            recovered_environment = CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(environment.episode.admissible_actions),
                checkpoint=baseline,
            )
            recovered = PaperCampaignRuntime(
                environment=recovered_environment,
                settlement_bridge=PaperSettlementLearningBridge(
                    root / "paper-learning-bridge.json",
                    paper_book_path=root / "paper_book.json",
                    decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                    agent_loop=AgentLoopRuntime(root / "agent-loop.json"),
                    economic_goal=bridge.economic_goal,
                    risk_policy=bridge.risk_policy,
                ),
            )
            with self.assertRaisesRegex(PaperCampaignRuntimeError, "predates"):
                recovered.finalize_ticket(
                    ticket_id=ticket_id,
                    at="2026-09-20T03:00:29+00:00",
                )
            recovered.agent_loop.advance(expected=AgentLoopPhase.EVALUATE, at=T4)
            witness = recovered.settlement_bridge.resolution_witness(ticket_id)
            frozen_at = recovered._bind_finalization_plan(
                witness,
                available_at=T4,
            )
            recovered.agent_loop.record_attribution(
                recovered._attribution(
                    witness,
                    available_at=frozen_at,
                ),
                at=T4,
            )
            conflicting = PaperCampaignRuntime(
                environment=recovered_environment,
                settlement_bridge=recovered.settlement_bridge,
                reflection_plan=PaperReflectionPlan(
                    unresolved_components=(AttributionComponent.DATA,),
                ),
            )
            with self.assertRaisesRegex(PaperCampaignRuntimeError, "finalization plan conflicts"):
                conflicting.finalize_ticket(ticket_id=ticket_id, at=T4)

            recovered.agent_loop.record_postmortem(
                recovered._postmortem(
                    recovered._attribution(
                        witness,
                        available_at=frozen_at,
                    ),
                    witness,
                    available_at=frozen_at,
                ),
                at=T4,
            )

            same_attribution_different_postmortem = PaperCampaignRuntime(
                environment=recovered_environment,
                settlement_bridge=recovered.settlement_bridge,
                reflection_plan=PaperReflectionPlan(
                    summary_code="DIFFERENT_POSTMORTEM_SUMMARY",
                ),
            )
            with self.assertRaisesRegex(PaperCampaignRuntimeError, "finalization plan conflicts"):
                same_attribution_different_postmortem.finalize_ticket(
                    ticket_id=ticket_id,
                    at=T4,
                )


if __name__ == "__main__":
    unittest.main()
