from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime, ExternalEffectState
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
    EnvironmentIdentity,
    Observation,
    Transition,
)
from autosport.paper import PaperBook
from autosport.paper_settlement_learning import (
    PaperSettlementLearningBridge,
    PaperSettlementLearningBridgeError,
)
from autosport.risk import PaperRiskPolicy
from autosport.settlement import SettlementEngine


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _rewrite_bridge_state(root: Path, state: dict[str, object]) -> None:
    bare = {
        "schema": state["schema"],
        "schema_version": state["schema_version"],
        "bindings": state["bindings"],
    }
    state["state_sha256"] = _canonical_digest(bare)
    (root / "paper_learning_bridge.json").write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _fixture(root: Path, *, leg: TicketLeg):
    goal = EconomicGoalContract(
        goal_id="bridge-goal",
        revision=1,
        bankroll_id="bridge-bankroll",
        currency="USD",
    )
    risk = PaperRiskPolicy(economic_goal=goal)
    book = PaperBook("100")
    ticket = book.open_ticket(
        (leg,),
        Decimal("10"),
        placed_at="2026-09-19T21:19:10+00:00",
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
    )
    book.save(root / "paper_book.json")

    identity = EnvironmentIdentity(
        source_id="bridge-source",
        config_id="bridge-config",
        data_id="bridge-data",
        protocol_id="bridge-protocol",
        cutoff_ts="2026-09-19T21:20:00+00:00",
        seed=7,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="bridge-episode",
        policy_id="bridge-policy",
        admissible_actions=frozenset({"PAPER_PROPOSAL"}),
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-19T21:19:00+00:00",
        available_at="2026-09-19T21:19:01+00:00",
        evidence=(("market_state", "bridge-snapshot"),),
    )

    ledger = JsonlDecisionLedger(root / "decisions.jsonl")
    decision = DecisionRecord(
        replay_run_id="bridge-run",
        agent="bridge-fixture",
        observed_ts=observation.observed_at,
        action="OPEN_PAPER_TICKET",
        payload={
            "ticket_id": ticket.ticket_id,
            "stake": str(ticket.stake),
            "quote_key": leg.quote_key,
        },
        context_hash=observation.observation_id,
        decision_id="bridge-decision",
        decision_kind=ECONOMIC_DECISION_KIND,
    )
    ledger.append_economic(decision, EconomicDecisionAuthority(goal, risk))

    baseline = environment.checkpoint()
    runtime = AgentLoopRuntime.initialize_pristine(
        root / "agent-loop.json",
        loop_id="bridge-loop",
        environment_checkpoint=baseline,
        policy_id=environment.episode.policy_id,
        economic_goal_fingerprint=provenance_for(goal).contract_sha256,
        risk_fingerprint=risk.provenance_sha256,
        source_sha256="a" * 64,
        config_sha256="b" * 64,
        at="2026-09-19T21:18:59+00:00",
    )
    runtime.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T21:19:01+00:00",
    )
    for phase in (
        AgentLoopPhase.OBSERVE,
        AgentLoopPhase.ASSESS,
        AgentLoopPhase.PLAN,
        AgentLoopPhase.DECIDE,
    ):
        runtime.advance(expected=phase, at="2026-09-19T21:19:02+00:00")

    action = environment.act(
        observation,
        action_type="PAPER_PROPOSAL",
        decision_at="2026-09-19T21:19:05+00:00",
        parameters=(
            ("economic_decision_id", decision.decision_id),
            ("paper_ticket_id", ticket.ticket_id),
        ),
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.PAPER_ONLY,
        at="2026-09-19T21:19:05+00:00",
    )
    bridge = PaperSettlementLearningBridge(
        root / "paper_learning_bridge.json",
        paper_book_path=root / "paper_book.json",
        decision_ledger=ledger,
        agent_loop=runtime,
        economic_goal=goal,
        risk_policy=risk,
    )
    return goal, risk, ticket, decision, environment, baseline, runtime, observation, action, bridge


def _settle(root: Path, *, leg: TicketLeg) -> tuple[SettlementResolution, ...]:
    book = PaperBook.load(root / "paper_book.json")
    engine = SettlementEngine()
    engine.record({leg.quote_key: "win"})
    settled = engine.settle_ready(book)
    if not settled:
        raise AssertionError("fixture settlement did not settle a ticket")
    book.save(root / "paper_book.json")
    return (
        SettlementResolution(
            event_identity=f"provider-a:{leg.event_id}",
            settlement_ref="result:0",
            quote_outcomes={leg.quote_key: "win"},
            evidence_id="evidence:0",
            evidence_sha256="1" * 64,
            available_at="2026-09-19T21:19:30+00:00",
        ),
    )


class PaperObservedRewardAvailabilityBindingTests(unittest.TestCase):
    def test_resigned_outbox_cannot_predate_canonical_settlement_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            (
                goal,
                risk,
                ticket,
                decision,
                environment,
                baseline,
                runtime,
                observation,
                action,
                bridge,
            ) = _fixture(root, leg=leg)

            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )
            resolutions = _settle(root, leg=leg)

            with patch.object(
                AgentLoopRuntime,
                "record_resolution",
                side_effect=RuntimeError("crash before learner acknowledgement"),
            ):
                with self.assertRaisesRegex(RuntimeError, "crash before learner"):
                    bridge.reconcile_after_settlement(
                        paper_book_path=root / "paper_book.json",
                        resolutions=resolutions,
                        settled_ticket_ids=(ticket.ticket_id,),
                        at="2026-09-19T21:20:00+00:00",
                    )

            state = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            binding = state["bindings"][ticket.ticket_id]
            outbox = binding["outbox"]
            settlement_available_at = outbox["settlement_evidence"][0]["available_at"]
            forged_available_at = "2026-09-19T21:19:20+00:00"
            self.assertLess(forged_available_at, settlement_available_at)

            book = PaperBook.load(root / "paper_book.json")
            settled_ticket = book.tickets[ticket.ticket_id]
            forged_outcome, forged_reward = bridge._paper_observed_evidence(
                binding,
                settled_ticket,
                bundle_sha256=outbox["settlement_bundle_sha256"],
                revealed_at=forged_available_at,
            )
            forged_reference = bridge._observed_reward_reference(
                binding,
                settled_ticket,
                settlement_bundle_sha256=outbox["settlement_bundle_sha256"],
                outcome=forged_outcome,
                reward=forged_reward,
            )
            raw_transition = outbox["transition"]
            forged_transition = Transition(
                environment_id=raw_transition["environment_id"],
                episode_id=raw_transition["episode_id"],
                step_index=raw_transition["step_index"],
                observation_id=raw_transition["observation_id"],
                action_id=raw_transition["action_id"],
                outcome_id=forged_outcome.outcome_id,
                reward_id=forged_reward.reward_id,
                decision_at=raw_transition["decision_at"],
                resolved_at=forged_available_at,
            )

            outbox["observed_reward_reference"] = forged_reference
            outbox["outcome"] = {
                "environment_id": forged_outcome.environment_id,
                "action_id": forged_outcome.action_id,
                "revealed_at": forged_outcome.revealed_at,
                "truth": forged_outcome.truth.value,
                "evidence": [list(item) for item in forged_outcome.evidence],
                "simulation_model_id": forged_outcome.simulation_model_id,
            }
            outbox["reward"] = {
                "environment_id": forged_reward.environment_id,
                "action_id": forged_reward.action_id,
                "outcome_id": forged_reward.outcome_id,
                "reward": str(forged_reward.reward),
                "available_at": forged_reward.available_at,
                "truth": forged_reward.truth.value,
                "evidence": [list(item) for item in forged_reward.evidence],
                "simulation_model_id": forged_reward.simulation_model_id,
            }
            outbox["transition"] = {
                "environment_id": forged_transition.environment_id,
                "episode_id": forged_transition.episode_id,
                "step_index": forged_transition.step_index,
                "observation_id": forged_transition.observation_id,
                "action_id": forged_transition.action_id,
                "outcome_id": forged_transition.outcome_id,
                "reward_id": forged_transition.reward_id,
                "decision_at": forged_transition.decision_at,
                "resolved_at": forged_transition.resolved_at,
            }
            outbox["next_checkpoint"]["last_transition_id"] = forged_transition.transition_id
            outbox["outbox_id"] = _canonical_digest(
                {key: value for key, value in outbox.items() if key != "outbox_id"}
            )
            _rewrite_bridge_state(root, state)

            reopened_runtime = AgentLoopRuntime(root / "agent-loop.json")
            reopened_bridge = PaperSettlementLearningBridge(
                root / "paper_learning_bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=reopened_runtime,
                economic_goal=goal,
                risk_policy=risk,
            )

            with self.assertRaises(PaperSettlementLearningBridgeError):
                reopened_bridge.reconcile_after_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=(),
                    settled_ticket_ids=(),
                    at="2026-09-19T21:20:01+00:00",
                )

            self.assertIs(reopened_runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)


if __name__ == "__main__":
    unittest.main()
