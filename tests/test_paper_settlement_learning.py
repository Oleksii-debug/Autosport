from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime, ExternalEffectState
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
)
from autosport.paper import PaperBook
from autosport.paper_settlement_learning import (
    PaperSettlementLearningBridge,
    PaperSettlementLearningBridgeError,
)
from autosport.risk import PaperRiskPolicy
from autosport.settlement import SettlementEngine
from autosport.continuous_session import SettlementResolution


def _fixture(
    root: Path,
    *,
    legs: tuple[TicketLeg, ...],
    action_type: str = "PAPER_PROPOSAL",
    placed_at: str = "2026-09-19T21:19:10+00:00",
    effect_state: ExternalEffectState = ExternalEffectState.PAPER_ONLY,
    bind_action_to_decision: bool = True,
):
    goal = EconomicGoalContract(
        goal_id="bridge-goal",
        revision=1,
        bankroll_id="bridge-bankroll",
        currency="USD",
    )
    risk = PaperRiskPolicy(economic_goal=goal)
    book = PaperBook("100")
    ticket = book.open_ticket(
        legs,
        Decimal("10"),
        placed_at=placed_at,
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
        admissible_actions=frozenset({action_type}),
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-19T21:19:00+00:00",
        available_at="2026-09-19T21:19:01+00:00",
        evidence=(("market_state", "bridge-snapshot"),),
    )

    ledger = JsonlDecisionLedger(root / "decisions.jsonl")
    quote_keys = tuple(sorted(leg.quote_key for leg in legs))
    decision_action = "OPEN_PAPER_TICKET"
    payload = {
        "ticket_id": ticket.ticket_id,
        "stake": str(ticket.stake),
    }
    if len(quote_keys) == 1:
        payload["quote_key"] = quote_keys[0]
    else:
        payload["quote_keys"] = list(quote_keys)
    decision = DecisionRecord(
        replay_run_id="bridge-run",
        agent="bridge-fixture",
        observed_ts=observation.observed_at,
        action=decision_action,
        payload=payload,
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
    action_binding = (
        (
            ("economic_decision_id", decision.decision_id),
            ("paper_ticket_id", ticket.ticket_id),
        )
        if bind_action_to_decision
        else (
            ("economic_decision_id", "different-economic-decision"),
            ("paper_ticket_id", "different-paper-ticket"),
        )
    )
    action = environment.act(
        observation,
        action_type=action_type,
        decision_at="2026-09-19T21:19:05+00:00",
        parameters=action_binding,
    )
    runtime.commit_action(
        action,
        episode=environment.episode,
        observation=observation,
        effect_state=effect_state,
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
    return (
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
    )


def _settle(
    root: Path,
    *,
    outcomes: dict[str, str],
) -> tuple[PaperBook, tuple[SettlementResolution, ...]]:
    book = PaperBook.load(root / "paper_book.json")
    engine = SettlementEngine()
    engine.record(outcomes)
    settled = engine.settle_ready(book)
    if not settled:
        raise AssertionError("fixture settlement did not settle a ticket")
    book.save(root / "paper_book.json")
    leg_by_key = {
        leg.quote_key: leg
        for ticket in book.tickets.values()
        for leg in ticket.legs
    }
    resolutions = tuple(
        SettlementResolution(
            event_identity=f"provider-a:{leg_by_key[quote_key].event_id}",
            settlement_ref=f"result:{index}",
            quote_outcomes={quote_key: outcome},
            evidence_id=f"evidence:{index}",
            evidence_sha256=f"{index + 1:x}" * 64,
            available_at="2026-09-19T21:19:30+00:00",
        )
        for index, (quote_key, outcome) in enumerate(sorted(outcomes.items()))
    )
    return book, resolutions


class PaperSettlementLearningBridgeTests(unittest.TestCase):
    def test_outbox_survives_failure_before_agent_loop_ack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            goal, risk, ticket, decision, environment, baseline, runtime, observation, action, bridge = _fixture(
                root,
                legs=(leg,),
            )
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )
            _book, resolutions = _settle(root, outcomes={leg.quote_key: "win"})

            with patch.object(
                AgentLoopRuntime,
                "record_resolution",
                side_effect=RuntimeError("simulated crash before learner ack"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    bridge.reconcile_after_settlement(
                        paper_book_path=root / "paper_book.json",
                        resolutions=resolutions,
                        settled_ticket_ids=(ticket.ticket_id,),
                        at="2026-09-19T21:20:00+00:00",
                    )

            durable = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            self.assertEqual(durable["bindings"][ticket.ticket_id]["status"], "OUTBOX")
            self.assertEqual(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)

            reopened_runtime = AgentLoopRuntime(root / "agent-loop.json")
            reopened_bridge = PaperSettlementLearningBridge(
                root / "paper_learning_bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=reopened_runtime,
                economic_goal=goal,
                risk_policy=risk,
            )
            acked = reopened_bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=(),
                settled_ticket_ids=(),
                at="2026-09-19T21:20:01+00:00",
            )
            self.assertEqual(len(acked), 1)
            self.assertEqual(reopened_runtime.snapshot().phase, AgentLoopPhase.EVALUATE)
            loop_state = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(len(loop_state["resolutions"]), 1)
            self.assertEqual(loop_state["resolutions"][0]["reward_value"], "10.00")

    def test_retry_after_agent_loop_resolution_before_bridge_ack_is_exactly_once(self) -> None:
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
            ) = _fixture(root, legs=(leg,))
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )

            # Prove the pre-settlement BOUND state itself survives process restart.
            bridge = PaperSettlementLearningBridge(
                root / "paper_learning_bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=AgentLoopRuntime(root / "agent-loop.json"),
                economic_goal=goal,
                risk_policy=risk,
            )
            self.assertEqual(
                bridge.reconcile_after_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=(),
                    settled_ticket_ids=(),
                    at="2026-09-19T21:19:20+00:00",
                ),
                (),
            )
            _book, resolutions = _settle(root, outcomes={leg.quote_key: "win"})

            real_write = bridge._write

            def fail_ack_write(payload):
                if any(
                    binding["status"] == "ACKED"
                    for binding in payload["bindings"].values()
                ):
                    raise RuntimeError("simulated crash after AgentLoop resolution")
                real_write(payload)

            with patch.object(bridge, "_write", side_effect=fail_ack_write):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "after AgentLoop resolution",
                ):
                    bridge.reconcile_after_settlement(
                        paper_book_path=root / "paper_book.json",
                        resolutions=resolutions,
                        settled_ticket_ids=(ticket.ticket_id,),
                        at="2026-09-19T21:20:00+00:00",
                    )

            loop_after_crash = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(loop_after_crash["resolutions"]), 1)
            bridge_after_crash = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                bridge_after_crash["bindings"][ticket.ticket_id]["status"],
                "OUTBOX",
            )

            reopened_runtime = AgentLoopRuntime(root / "agent-loop.json")
            reopened_bridge = PaperSettlementLearningBridge(
                root / "paper_learning_bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=reopened_runtime,
                economic_goal=goal,
                risk_policy=risk,
            )
            acked = reopened_bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=(),
                settled_ticket_ids=(),
                at="2026-09-19T21:20:01+00:00",
            )
            self.assertEqual(len(acked), 1)
            final_loop = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(final_loop["resolutions"]), 1)
            final_bridge = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                final_bridge["bindings"][ticket.ticket_id]["status"],
                "ACKED",
            )

    def test_all_void_ticket_produces_exact_zero_reward(self) -> None:
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
                _goal,
                _risk,
                ticket,
                decision,
                environment,
                baseline,
                _runtime,
                observation,
                action,
                bridge,
            ) = _fixture(root, legs=(leg,))
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )
            _book, resolutions = _settle(root, outcomes={leg.quote_key: "void"})
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket.ticket_id,),
                at="2026-09-19T21:20:00+00:00",
            )
            loop_state = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                Decimal(loop_state["resolutions"][0]["reward_value"]),
                Decimal("0"),
            )


    def test_backdated_settlement_evidence_cannot_mint_observed_transition(self) -> None:
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
                _goal,
                _risk,
                ticket,
                decision,
                environment,
                baseline,
                runtime,
                observation,
                action,
                bridge,
            ) = _fixture(root, legs=(leg,))
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )
            book = PaperBook.load(root / "paper_book.json")
            engine = SettlementEngine()
            engine.record({leg.quote_key: "win"})
            self.assertEqual(engine.settle_ready(book), [ticket.ticket_id])
            book.save(root / "paper_book.json")
            backdated = SettlementResolution(
                event_identity=f"provider-a:{leg.event_id}",
                settlement_ref="backdated-result",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="backdated-evidence",
                evidence_sha256="f" * 64,
                available_at="2026-09-19T21:19:09+00:00",
            )
            with self.assertRaisesRegex(
                PaperSettlementLearningBridgeError,
                "predates bound action or ticket placement",
            ):
                bridge.reconcile_after_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=(backdated,),
                    settled_ticket_ids=(ticket.ticket_id,),
                    at="2026-09-19T21:20:00+00:00",
                )
            self.assertIs(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)
            durable = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            self.assertEqual(durable["bindings"][ticket.ticket_id]["status"], "BOUND")
            self.assertIsNone(durable["bindings"][ticket.ticket_id]["outbox"])

    def test_multileg_later_evidence_cannot_mask_backdated_leg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legs = (
                TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                ),
                TicketLeg(
                    event_id="event-2",
                    market_id="winner",
                    selection_id="away",
                    locked_odds=Decimal("3.00"),
                    sport="table_tennis",
                ),
            )
            (
                _goal,
                _risk,
                ticket,
                decision,
                environment,
                baseline,
                runtime,
                observation,
                action,
                bridge,
            ) = _fixture(root, legs=legs)
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )
            book = PaperBook.load(root / "paper_book.json")
            engine = SettlementEngine()
            engine.record(
                {
                    legs[0].quote_key: "win",
                    legs[1].quote_key: "win",
                }
            )
            self.assertEqual(engine.settle_ready(book), [ticket.ticket_id])
            book.save(root / "paper_book.json")
            resolutions = (
                SettlementResolution(
                    event_identity=f"provider-a:{legs[0].event_id}",
                    settlement_ref="backdated-result:event-1",
                    quote_outcomes={legs[0].quote_key: "win"},
                    evidence_id="backdated-evidence:event-1",
                    evidence_sha256="e" * 64,
                    available_at="2026-09-19T21:19:09+00:00",
                ),
                SettlementResolution(
                    event_identity=f"provider-a:{legs[1].event_id}",
                    settlement_ref="causal-result:event-2",
                    quote_outcomes={legs[1].quote_key: "win"},
                    evidence_id="causal-evidence:event-2",
                    evidence_sha256="d" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                ),
            )
            with self.assertRaisesRegex(
                PaperSettlementLearningBridgeError,
                "predates bound action or ticket placement",
            ):
                bridge.reconcile_after_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=resolutions,
                    settled_ticket_ids=(ticket.ticket_id,),
                    at="2026-09-19T21:20:00+00:00",
                )
            self.assertIs(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)
            durable = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            self.assertEqual(durable["bindings"][ticket.ticket_id]["status"], "BOUND")
            self.assertIsNone(durable["bindings"][ticket.ticket_id]["outbox"])


    def test_future_settlement_evidence_cannot_mint_observed_transition(self) -> None:
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
                _goal,
                _risk,
                ticket,
                decision,
                environment,
                baseline,
                runtime,
                observation,
                action,
                bridge,
            ) = _fixture(root, legs=(leg,))
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )
            book = PaperBook.load(root / "paper_book.json")
            engine = SettlementEngine()
            engine.record({leg.quote_key: "win"})
            self.assertEqual(engine.settle_ready(book), [ticket.ticket_id])
            book.save(root / "paper_book.json")
            future = SettlementResolution(
                event_identity=f"provider-a:{leg.event_id}",
                settlement_ref="future-result",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="future-evidence",
                evidence_sha256="d" * 64,
                available_at="2026-09-19T21:21:00+00:00",
            )
            with self.assertRaisesRegex(
                PaperSettlementLearningBridgeError,
                "causal validation",
            ):
                bridge.reconcile_after_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=(future,),
                    settled_ticket_ids=(ticket.ticket_id,),
                    at="2026-09-19T21:20:00+00:00",
                )
            self.assertEqual(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)
            loop_state = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(loop_state["resolutions"], [])

    def test_wrong_event_scope_cannot_mint_observed_transition(self) -> None:
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
                _goal,
                _risk,
                ticket,
                decision,
                environment,
                baseline,
                runtime,
                observation,
                action,
                bridge,
            ) = _fixture(root, legs=(leg,))
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )
            book = PaperBook.load(root / "paper_book.json")
            engine = SettlementEngine()
            engine.record({leg.quote_key: "win"})
            self.assertEqual(engine.settle_ready(book), [ticket.ticket_id])
            book.save(root / "paper_book.json")
            wrong_scope = SettlementResolution(
                event_identity="provider-a:another-event",
                settlement_ref="wrong-scope-result",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="wrong-scope-evidence",
                evidence_sha256="e" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            with self.assertRaisesRegex(
                PaperSettlementLearningBridgeError,
                "event identity",
            ):
                bridge.reconcile_after_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=(wrong_scope,),
                    settled_ticket_ids=(ticket.ticket_id,),
                    at="2026-09-19T21:20:00+00:00",
                )
            self.assertEqual(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)

    def test_unbound_settled_ticket_remains_economic_only(self) -> None:
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
                _goal,
                _risk,
                ticket,
                _decision,
                _environment,
                _baseline,
                runtime,
                _observation,
                _action,
                bridge,
            ) = _fixture(root, legs=(leg,))
            book, resolutions = _settle(root, outcomes={leg.quote_key: "win"})
            self.assertEqual(book.balance, Decimal("110.00"))
            self.assertEqual(
                bridge.reconcile_after_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=resolutions,
                    settled_ticket_ids=(ticket.ticket_id,),
                    at="2026-09-19T21:20:00+00:00",
                ),
                (),
            )
            self.assertEqual(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)
            loop_state = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(loop_state["resolutions"], [])

    def test_loss_uses_negative_exact_reward_from_final_paper_economics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legs = (
                TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                ),
                TicketLeg(
                    event_id="event-2",
                    market_id="winner",
                    selection_id="away",
                    locked_odds=Decimal("3.00"),
                    sport="table_tennis",
                ),
            )
            _goal, _risk, ticket, decision, environment, baseline, runtime, observation, action, bridge = _fixture(
                root,
                legs=legs,
            )
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )
            _book, resolutions = _settle(
                root,
                outcomes={legs[0].quote_key: "loss"},
            )
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket.ticket_id,),
                at="2026-09-19T21:20:00+00:00",
            )
            loop_state = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(loop_state["resolutions"][0]["reward_value"], "-10")
            self.assertEqual(runtime.snapshot().phase, AgentLoopPhase.EVALUATE)

    def test_multileg_win_void_preserves_void_and_exact_net_reward(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legs = (
                TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                ),
                TicketLeg(
                    event_id="event-2",
                    market_id="winner",
                    selection_id="away",
                    locked_odds=Decimal("3.00"),
                    sport="table_tennis",
                ),
            )
            _goal, _risk, ticket, decision, environment, baseline, _runtime, observation, action, bridge = _fixture(
                root,
                legs=legs,
            )
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )
            _book, resolutions = _settle(
                root,
                outcomes={
                    legs[0].quote_key: "win",
                    legs[1].quote_key: "void",
                },
            )
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket.ticket_id,),
                at="2026-09-19T21:20:00+00:00",
            )
            loop_state = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(loop_state["resolutions"][0]["reward_value"], "10.00")
            bridge_state = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            binding = bridge_state["bindings"][ticket.ticket_id]
            self.assertEqual(
                binding["outbox"]["known_quote_outcomes"],
                {
                    legs[0].quote_key: "win",
                    legs[1].quote_key: "void",
                },
            )

    def test_ticket_created_before_action_cannot_bind_reward(self) -> None:
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
                _goal,
                _risk,
                ticket,
                decision,
                environment,
                baseline,
                runtime,
                observation,
                action,
                bridge,
            ) = _fixture(
                root,
                legs=(leg,),
                placed_at="2026-09-19T21:19:04+00:00",
            )
            with self.assertRaisesRegex(
                PaperSettlementLearningBridgeError,
                "predates bound AgentLoop action",
            ):
                bridge.bind_ticket(
                    ticket_id=ticket.ticket_id,
                    decision_id=decision.decision_id,
                    environment=environment,
                    observation=observation,
                    action=action,
                    baseline_checkpoint=baseline,
                )
            durable = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            self.assertEqual(durable["bindings"], {})
            self.assertIs(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)

    def test_non_paper_action_effect_cannot_bind_ticket_reward(self) -> None:
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
                _goal,
                _risk,
                ticket,
                decision,
                environment,
                baseline,
                runtime,
                observation,
                action,
                bridge,
            ) = _fixture(
                root,
                legs=(leg,),
                effect_state=ExternalEffectState.NONE,
            )
            with self.assertRaisesRegex(
                PaperSettlementLearningBridgeError,
                "requires durable PAPER_ONLY action effect",
            ):
                bridge.bind_ticket(
                    ticket_id=ticket.ticket_id,
                    decision_id=decision.decision_id,
                    environment=environment,
                    observation=observation,
                    action=action,
                    baseline_checkpoint=baseline,
                )
            durable = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            self.assertEqual(durable["bindings"], {})
            self.assertIs(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)

    def test_abstain_action_cannot_bind_ticket_reward(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            _goal, _risk, ticket, decision, environment, baseline, _runtime, observation, action, bridge = _fixture(
                root,
                legs=(leg,),
                action_type="WAIT",
            )
            with self.assertRaisesRegex(
                PaperSettlementLearningBridgeError,
                "WAIT/NO_BET/ABSTAIN",
            ):
                bridge.bind_ticket(
                    ticket_id=ticket.ticket_id,
                    decision_id=decision.decision_id,
                    environment=environment,
                    observation=observation,
                    action=action,
                    baseline_checkpoint=baseline,
                )


    def test_wrong_non_abstain_agent_action_cannot_bind_ticket_reward(self) -> None:
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
                _goal,
                _risk,
                ticket,
                decision,
                environment,
                baseline,
                runtime,
                observation,
                action,
                bridge,
            ) = _fixture(
                root,
                legs=(leg,),
                bind_action_to_decision=False,
            )
            self.assertEqual(action.action_type, "PAPER_PROPOSAL")
            with self.assertRaisesRegex(
                PaperSettlementLearningBridgeError,
                "AgentLoop action is not bound to supplied economic decision and PaperTicket",
            ):
                bridge.bind_ticket(
                    ticket_id=ticket.ticket_id,
                    decision_id=decision.decision_id,
                    environment=environment,
                    observation=observation,
                    action=action,
                    baseline_checkpoint=baseline,
                )
            self.assertIs(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)
            self.assertIsNone(runtime.snapshot().transition_id)
            durable = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            self.assertEqual(durable["bindings"], {})


    def test_different_non_abstain_action_type_with_same_ids_cannot_bind_reward(self) -> None:
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
                _goal,
                _risk,
                ticket,
                decision,
                environment,
                baseline,
                runtime,
                observation,
                action,
                bridge,
            ) = _fixture(
                root,
                legs=(leg,),
                action_type="HEDGE_PROPOSAL",
            )
            self.assertEqual(dict(action.parameters)["economic_decision_id"], decision.decision_id)
            self.assertEqual(dict(action.parameters)["paper_ticket_id"], ticket.ticket_id)
            with self.assertRaisesRegex(
                PaperSettlementLearningBridgeError,
                "economic decision action is not semantically bound to AgentLoop action_type",
            ):
                bridge.bind_ticket(
                    ticket_id=ticket.ticket_id,
                    decision_id=decision.decision_id,
                    environment=environment,
                    observation=observation,
                    action=action,
                    baseline_checkpoint=baseline,
                )
            self.assertIs(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)
            self.assertIsNone(runtime.snapshot().transition_id)
            durable = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            self.assertEqual(durable["bindings"], {})


    def test_prepared_settlement_recovers_after_book_save_before_handoff(self) -> None:
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
                _runtime,
                observation,
                action,
                bridge,
            ) = _fixture(root, legs=(leg,))
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )
            resolution = SettlementResolution(
                event_identity=f"provider-a:{leg.event_id}",
                settlement_ref="prepared-result",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="prepared-evidence",
                evidence_sha256="c" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            self.assertEqual(
                bridge.prepare_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=(resolution,),
                    at="2026-09-19T21:20:00+00:00",
                ),
                (ticket.ticket_id,),
            )

            book = PaperBook.load(root / "paper_book.json")
            engine = SettlementEngine()
            engine.record({leg.quote_key: "win"})
            self.assertEqual(engine.settle_ready(book), [ticket.ticket_id])
            book.save(root / "paper_book.json")

            restarted_runtime = AgentLoopRuntime(root / "agent-loop.json")
            restarted_bridge = PaperSettlementLearningBridge(
                root / "paper_learning_bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=restarted_runtime,
                economic_goal=goal,
                risk_policy=risk,
            )
            acknowledged = restarted_bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=(),
                settled_ticket_ids=(),
                at="2026-09-19T21:20:01+00:00",
            )
            self.assertEqual(len(acknowledged), 1)
            self.assertIs(restarted_runtime.snapshot().phase, AgentLoopPhase.EVALUATE)
            self.assertEqual(
                PaperBook.load(root / "paper_book.json").balance,
                Decimal("110.00"),
            )
            durable = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            binding = durable["bindings"][ticket.ticket_id]
            self.assertEqual(binding["status"], "ACKED")
            self.assertIsNotNone(binding["settlement_intent"])
            self.assertEqual(
                restarted_bridge.reconcile_after_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=(),
                    settled_ticket_ids=(),
                    at="2026-09-19T21:20:02+00:00",
                ),
                (),
            )


    def test_bound_ticket_outside_current_settlement_handoff_stays_bound(self) -> None:
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
                _goal,
                _risk,
                ticket,
                decision,
                environment,
                baseline,
                runtime,
                observation,
                action,
                bridge,
            ) = _fixture(root, legs=(leg,))
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )
            _book, resolutions = _settle(root, outcomes={leg.quote_key: "win"})

            self.assertEqual(
                bridge.reconcile_after_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=resolutions,
                    settled_ticket_ids=("different-ticket-id",),
                    at="2026-09-19T21:20:00+00:00",
                ),
                (),
            )
            durable = json.loads(
                (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
            )
            binding = durable["bindings"][ticket.ticket_id]
            self.assertEqual(binding["status"], "BOUND")
            self.assertIsNone(binding["outbox"])
            self.assertIs(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)

            acknowledged = bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(ticket.ticket_id,),
                at="2026-09-19T21:20:01+00:00",
            )
            self.assertEqual(len(acknowledged), 1)
            self.assertIs(runtime.snapshot().phase, AgentLoopPhase.EVALUATE)

    def test_duplicate_settled_ticket_identity_fails_closed(self) -> None:
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
                _goal,
                _risk,
                ticket,
                _decision,
                _environment,
                _baseline,
                runtime,
                _observation,
                _action,
                bridge,
            ) = _fixture(root, legs=(leg,))
            _book, resolutions = _settle(root, outcomes={leg.quote_key: "win"})
            with self.assertRaisesRegex(
                PaperSettlementLearningBridgeError,
                "duplicate ticket identity",
            ):
                bridge.reconcile_after_settlement(
                    paper_book_path=root / "paper_book.json",
                    resolutions=resolutions,
                    settled_ticket_ids=(ticket.ticket_id, ticket.ticket_id),
                    at="2026-09-19T21:20:00+00:00",
                )
            self.assertIs(runtime.snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)


if __name__ == "__main__":
    unittest.main()
