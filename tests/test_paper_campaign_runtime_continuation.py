from __future__ import annotations

import importlib.util
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


_BASE_PATH = Path(__file__).with_name("_paper_campaign_runtime_tests_base.py")
_SPEC = importlib.util.spec_from_file_location(
    "_paper_campaign_runtime_tests_base_continuation",
    _BASE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("cannot load campaign runtime regression base")
_legacy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_legacy)


class PaperCampaignRuntimeContinuationTests(unittest.TestCase):
    def test_finalized_checkpoint_allows_next_ticket_after_process_restart(self) -> None:
        """A sealed campaign checkpoint is a real restart head, not a terminal stop."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                first_leg,
                _book,
                first_ticket_id,
                _first_decision,
                environment,
                _baseline,
                _first_observation,
                bridge,
                runtime,
            ) = _legacy._fixture(root)

            resolutions = _legacy._settle(root, first_leg, "win")
            bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=resolutions,
                settled_ticket_ids=(first_ticket_id,),
                at=_legacy.T4,
            )
            first_receipt = runtime.finalize_ticket(
                ticket_id=first_ticket_id,
                at=_legacy.T4,
            )
            checkpoint = runtime.environment.checkpoint()
            self.assertEqual(first_receipt.checkpoint_id, checkpoint.checkpoint_id)
            self.assertEqual(checkpoint.step_index, 1)

            # Simulate a clean process restart from only the durable AgentLoop,
            # bridge state and exact EnvironmentCheckpoint produced above.
            resumed_environment = _legacy.CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(environment.episode.admissible_actions),
                checkpoint=checkpoint,
            )
            ledger = _legacy.JsonlDecisionLedger(root / "decisions.jsonl")
            resumed_bridge = _legacy.PaperSettlementLearningBridge(
                root / "paper-learning-bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=ledger,
                agent_loop=_legacy.AgentLoopRuntime(root / "agent-loop.json"),
                economic_goal=bridge.economic_goal,
                risk_policy=bridge.risk_policy,
            )
            resumed_runtime = _legacy.PaperCampaignRuntime(
                environment=resumed_environment,
                settlement_bridge=resumed_bridge,
            )

            second_leg = _legacy.TicketLeg(
                event_id="campaign-event-2",
                market_id="winner",
                selection_id="away",
                locked_odds=Decimal("1.80"),
                sport="table_tennis",
            )
            book = _legacy.PaperBook.load(root / "paper_book.json")
            second_ticket = book.open_ticket(
                (second_leg,),
                Decimal("5"),
                placed_at=_legacy.T6,
                bankroll_id=bridge.economic_goal.bankroll_id,
                currency=bridge.economic_goal.currency,
            )
            book.save(root / "paper_book.json")

            second_observation = _legacy.Observation(
                environment_id=resumed_environment.environment_id,
                observed_at=_legacy.T5,
                available_at=_legacy.T5,
                evidence=(("market_state", "campaign-snapshot-2"),),
            )
            second_decision = _legacy.DecisionRecord(
                replay_run_id="campaign-run-2",
                agent="campaign-continuation-test",
                observed_ts=second_observation.observed_at,
                action="OPEN_PAPER_TICKET",
                payload={
                    "ticket_id": second_ticket.ticket_id,
                    "quote_key": second_leg.quote_key,
                    "stake": str(second_ticket.stake),
                },
                context_hash=second_observation.observation_id,
                decision_id="campaign-decision-2",
                decision_kind=_legacy.ECONOMIC_DECISION_KIND,
            )
            ledger.append_economic(
                second_decision,
                _legacy.EconomicDecisionAuthority(
                    bridge.economic_goal,
                    bridge.risk_policy,
                ),
            )

            second_action = resumed_runtime.begin_and_bind_paper_ticket(
                ticket_id=second_ticket.ticket_id,
                decision_id=second_decision.decision_id,
                observation=second_observation,
                action_type="PAPER_PROPOSAL",
                decision_at=_legacy.T6,
                parameters=(
                    ("economic_decision_id", second_decision.decision_id),
                    ("paper_ticket_id", second_ticket.ticket_id),
                ),
                at=_legacy.T6,
            )

            snapshot = resumed_runtime.agent_loop.snapshot()
            self.assertIs(snapshot.phase, _legacy.AgentLoopPhase.WAIT_OUTCOME)
            self.assertEqual(snapshot.environment_checkpoint_id, checkpoint.checkpoint_id)
            self.assertEqual(snapshot.observation_id, second_observation.observation_id)
            self.assertEqual(snapshot.action_id, second_action.action_id)
            self.assertNotEqual(second_action.action_id, first_receipt.transition_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
