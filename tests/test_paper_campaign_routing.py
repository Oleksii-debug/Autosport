from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from autosport.paper_campaign_routing import (
    PaperCampaignLearningRouter,
    PaperCampaignRouteError,
    PaperCampaignRouteStore,
)
from autosport.paper_campaign_runtime import PaperCampaignLearningHandoff
from autosport.paper_settlement_learning import PaperSettlementLearningBridgeError


_BASE_PATH = Path(__file__).with_name("_paper_campaign_runtime_tests_base.py")
_SPEC = importlib.util.spec_from_file_location(
    "_paper_campaign_runtime_tests_base_for_routing",
    _BASE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("cannot load campaign runtime regression base")
_campaign = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_campaign)


def _handoff(
    ticket_id: str,
    *,
    environment_id: str,
    episode_id: str,
    action_id: str,
    prepared: tuple[str, ...] = (),
    transitions: tuple[str, ...] = (),
    has_outbox: bool = False,
    terminal: bool | None = None,
) -> PaperCampaignLearningHandoff:
    if terminal is None:
        terminal = has_outbox
    transition_id = f"transition-{ticket_id}"
    checkpoint_id = f"checkpoint-{ticket_id}"
    handoff = Mock(spec=PaperCampaignLearningHandoff)
    handoff.ticket_id = ticket_id
    agent_loop = Mock()
    agent_loop.snapshot.return_value = SimpleNamespace(
        environment_id=environment_id,
        episode_id=episode_id,
        action_id=action_id,
        transition_id=transition_id if terminal else None,
        checkpointed_transition_id=transition_id if terminal else None,
        environment_checkpoint_id=checkpoint_id if terminal else "checkpoint-baseline",
        attribution_id="attribution-id" if terminal else None,
        postmortem_id="postmortem-id" if terminal else None,
    )
    bridge = Mock()
    bridge._read.return_value = {
        "bindings": {
            ticket_id: {
                "ticket_id": ticket_id,
                "environment_id": environment_id,
                "episode_id": episode_id,
                "action_id": action_id,
            }
        }
    }
    if has_outbox:
        bridge.resolution_witness.return_value = SimpleNamespace(
            transition=SimpleNamespace(
                transition_id=transition_id,
                environment_id=environment_id,
                episode_id=episode_id,
                action_id=action_id,
            ),
            next_checkpoint=SimpleNamespace(checkpoint_id=checkpoint_id),
        )
    else:
        bridge.resolution_witness.side_effect = PaperSettlementLearningBridgeError(
            "ticket has no durable learner outbox"
        )
    handoff.runtime = SimpleNamespace(
        agent_loop=agent_loop,
        settlement_bridge=bridge,
    )
    handoff.prepare_settlement.return_value = prepared
    handoff.reconcile_after_settlement.return_value = transitions
    return handoff


class PaperCampaignRouteStoreTests(unittest.TestCase):
    def test_register_restart_and_exact_retry_preserve_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign_routes.json"
            handoff = _handoff(
                "ticket-1",
                environment_id="env-1",
                episode_id="episode-1",
                action_id="action-1",
            )
            first = PaperCampaignRouteStore(path)
            route = first.register(handoff)
            self.assertEqual(route.status, "ACTIVE")

            restored = PaperCampaignRouteStore(path)
            self.assertEqual(restored.get("ticket-1"), route)
            self.assertEqual(restored.register(handoff), route)

    def test_same_ticket_cannot_be_rebound_to_another_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PaperCampaignRouteStore(Path(directory) / "campaign_routes.json")
            store.register(
                _handoff(
                    "ticket-1",
                    environment_id="env-1",
                    episode_id="episode-1",
                    action_id="action-1",
                )
            )
            with self.assertRaisesRegex(
                PaperCampaignRouteError,
                "already bound to another campaign learning route",
            ):
                store.register(
                    _handoff(
                        "ticket-1",
                        environment_id="env-1",
                        episode_id="episode-1",
                        action_id="action-2",
                    )
                )

    def test_same_agent_action_cannot_route_two_tickets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PaperCampaignRouteStore(Path(directory) / "campaign_routes.json")
            store.register(
                _handoff(
                    "ticket-1",
                    environment_id="env-1",
                    episode_id="episode-1",
                    action_id="action-1",
                )
            )
            with self.assertRaisesRegex(
                PaperCampaignRouteError,
                "one campaign action cannot route multiple PaperTickets",
            ):
                store.register(
                    _handoff(
                        "ticket-2",
                        environment_id="env-1",
                        episode_id="episode-1",
                        action_id="action-1",
                    )
                )

    def test_rehashed_duplicate_action_rows_fail_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign_routes.json"
            store = PaperCampaignRouteStore(path)
            store.register(
                _handoff(
                    "ticket-1",
                    environment_id="env-1",
                    episode_id="episode-1",
                    action_id="action-1",
                )
            )
            store.register(
                _handoff(
                    "ticket-2",
                    environment_id="env-2",
                    episode_id="episode-2",
                    action_id="action-2",
                )
            )
            state = store._read()
            first = state["routes"]["ticket-1"]
            state["routes"]["ticket-2"] = store._record(
                ticket_id="ticket-2",
                environment_id=first["environment_id"],
                episode_id=first["episode_id"],
                action_id=first["action_id"],
                status="ACTIVE",
            )
            store._write(state["routes"])

            with self.assertRaisesRegex(
                PaperCampaignRouteError,
                "one campaign action cannot route multiple PaperTickets",
            ):
                PaperCampaignRouteStore(path)

    def test_foreign_ticket_handoff_cannot_poison_route_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                _leg,
                _book,
                _ticket_id,
                _decision,
                _environment,
                _baseline,
                _observation,
                _bridge,
                runtime,
            ) = _campaign._fixture(root)
            foreign = PaperCampaignLearningHandoff(
                runtime,
                ticket_id="foreign-ticket",
            )
            store = PaperCampaignRouteStore(root / "campaign_routes.json")

            with self.assertRaisesRegex(
                PaperCampaignRouteError,
                "lacks durable settlement-learning binding",
            ):
                store.register(foreign)
            self.assertEqual(store.routes(), ())

    def test_tampered_durable_route_index_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign_routes.json"
            store = PaperCampaignRouteStore(path)
            store.register(
                _handoff(
                    "ticket-1",
                    environment_id="env-1",
                    episode_id="episode-1",
                    action_id="action-1",
                )
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["routes"]["ticket-1"]["action_id"] = "action-forged"
            path.write_text(json.dumps(raw), encoding="utf-8")

            with self.assertRaisesRegex(
                PaperCampaignRouteError,
                "digest mismatch",
            ):
                PaperCampaignRouteStore(path)

    def test_finalization_is_monotone_and_restart_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign_routes.json"
            store = PaperCampaignRouteStore(path)
            handoff = _handoff(
                "ticket-1",
                environment_id="env-1",
                episode_id="episode-1",
                action_id="action-1",
                has_outbox=True,
            )
            route = store.register(handoff)
            finalized = store.mark_finalized(route, handoff=handoff)
            self.assertEqual(finalized.status, "FINALIZED")
            self.assertEqual(
                store.mark_finalized(finalized, handoff=handoff),
                finalized,
            )
            self.assertEqual(
                PaperCampaignRouteStore(path).get("ticket-1").status,
                "FINALIZED",
            )

    def test_route_cannot_be_finalized_without_terminal_campaign_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign_routes.json"
            store = PaperCampaignRouteStore(path)
            handoff = _handoff(
                "ticket-1",
                environment_id="env-1",
                episode_id="episode-1",
                action_id="action-1",
                has_outbox=True,
                terminal=False,
            )
            route = store.register(handoff)

            with self.assertRaisesRegex(
                PaperCampaignRouteError,
                "has not reached canonical terminal checkpoint",
            ):
                store.mark_finalized(route, handoff=handoff)

            self.assertEqual(store.get("ticket-1").status, "ACTIVE")
            self.assertEqual(
                PaperCampaignRouteStore(path).get("ticket-1").status,
                "ACTIVE",
            )

    def test_route_cannot_be_finalized_before_learner_outbox_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaign_routes.json"
            store = PaperCampaignRouteStore(path)
            handoff = _handoff(
                "ticket-1",
                environment_id="env-1",
                episode_id="episode-1",
                action_id="action-1",
            )
            route = store.register(handoff)

            with self.assertRaisesRegex(
                PaperCampaignRouteError,
                "lacks canonical terminal settlement evidence",
            ):
                store.mark_finalized(route, handoff=handoff)

            self.assertEqual(
                PaperCampaignRouteStore(path).get("ticket-1").status,
                "ACTIVE",
            )


class PaperCampaignLearningRouterTests(unittest.TestCase):
    def test_two_campaign_routes_dispatch_without_cross_ticket_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PaperCampaignRouteStore(Path(directory) / "campaign_routes.json")
            first = _handoff(
                "ticket-1",
                environment_id="env-1",
                episode_id="episode-1",
                action_id="action-1",
                prepared=("ticket-1",),
            )
            second = _handoff(
                "ticket-2",
                environment_id="env-2",
                episode_id="episode-2",
                action_id="action-2",
            )
            store.register(first)
            store.register(second)
            handoffs = {"ticket-1": first, "ticket-2": second}
            router = PaperCampaignLearningRouter(
                store,
                resolve_handoff=lambda route: handoffs[route.ticket_id],
            )

            prepared = router.prepare_settlement(
                paper_book_path=Path(directory) / "paper.json",
                resolutions=(),
                at="2026-09-20T17:40:00+00:00",
            )
            self.assertEqual(prepared, ("ticket-1",))
            first.prepare_settlement.assert_called_once()
            second.prepare_settlement.assert_called_once()

    def test_settled_ticket_without_route_fails_before_learning_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PaperCampaignRouteStore(Path(directory) / "campaign_routes.json")
            known = _handoff(
                "ticket-1",
                environment_id="env-1",
                episode_id="episode-1",
                action_id="action-1",
            )
            store.register(known)
            router = PaperCampaignLearningRouter(
                store,
                resolve_handoff=lambda route: known,
            )

            with self.assertRaisesRegex(
                PaperCampaignRouteError,
                "settled ticket lacks durable campaign learning route",
            ):
                router.reconcile_after_settlement(
                    paper_book_path=Path(directory) / "paper.json",
                    resolutions=(),
                    settled_ticket_ids=("ticket-unknown",),
                    at="2026-09-20T17:40:00+00:00",
                )
            known.reconcile_after_settlement.assert_not_called()

    def test_reconcile_finalizes_only_route_with_durable_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PaperCampaignRouteStore(Path(directory) / "campaign_routes.json")
            first = _handoff(
                "ticket-1",
                environment_id="env-1",
                episode_id="episode-1",
                action_id="action-1",
                transitions=("transition-1",),
                has_outbox=True,
            )
            second = _handoff(
                "ticket-2",
                environment_id="env-2",
                episode_id="episode-2",
                action_id="action-2",
                has_outbox=False,
            )
            store.register(first)
            store.register(second)
            handoffs = {"ticket-1": first, "ticket-2": second}
            router = PaperCampaignLearningRouter(
                store,
                resolve_handoff=lambda route: handoffs[route.ticket_id],
            )

            transitions = router.reconcile_after_settlement(
                paper_book_path=Path(directory) / "paper.json",
                resolutions=(),
                settled_ticket_ids=("ticket-1",),
                at="2026-09-20T17:40:00+00:00",
            )
            self.assertEqual(transitions, ("transition-1",))
            self.assertEqual(store.get("ticket-1").status, "FINALIZED")
            self.assertEqual(store.get("ticket-2").status, "ACTIVE")

    def test_duplicate_transition_identity_across_routes_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PaperCampaignRouteStore(Path(directory) / "campaign_routes.json")
            first = _handoff(
                "ticket-1",
                environment_id="env-1",
                episode_id="episode-1",
                action_id="action-1",
                transitions=("transition-shared",),
            )
            second = _handoff(
                "ticket-2",
                environment_id="env-2",
                episode_id="episode-2",
                action_id="action-2",
                transitions=("transition-shared",),
            )
            store.register(first)
            store.register(second)
            handoffs = {"ticket-1": first, "ticket-2": second}
            router = PaperCampaignLearningRouter(
                store,
                resolve_handoff=lambda route: handoffs[route.ticket_id],
            )

            with self.assertRaisesRegex(
                PaperCampaignRouteError,
                "duplicate transition identity",
            ):
                router.reconcile_after_settlement(
                    paper_book_path=Path(directory) / "paper.json",
                    resolutions=(),
                    settled_ticket_ids=(),
                    at="2026-09-20T17:40:00+00:00",
                )

    def test_restart_resolver_must_match_durable_episode_and_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PaperCampaignRouteStore(Path(directory) / "campaign_routes.json")
            original = _handoff(
                "ticket-1",
                environment_id="env-1",
                episode_id="episode-1",
                action_id="action-1",
            )
            store.register(original)
            conflicting = _handoff(
                "ticket-1",
                environment_id="env-1",
                episode_id="episode-other",
                action_id="action-1",
            )
            router = PaperCampaignLearningRouter(
                store,
                resolve_handoff=lambda route: conflicting,
            )

            with self.assertRaisesRegex(
                PaperCampaignRouteError,
                "conflicts with durable ticket route",
            ):
                router.prepare_settlement(
                    paper_book_path=Path(directory) / "paper.json",
                    resolutions=(),
                    at="2026-09-20T17:40:00+00:00",
                )

    def test_handoff_cannot_prepare_another_routes_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PaperCampaignRouteStore(Path(directory) / "campaign_routes.json")
            handoff = _handoff(
                "ticket-1",
                environment_id="env-1",
                episode_id="episode-1",
                action_id="action-1",
                prepared=("ticket-2",),
            )
            store.register(handoff)
            router = PaperCampaignLearningRouter(
                store,
                resolve_handoff=lambda route: handoff,
            )

            with self.assertRaisesRegex(
                PaperCampaignRouteError,
                "outside its durable route",
            ):
                router.prepare_settlement(
                    paper_book_path=Path(directory) / "paper.json",
                    resolutions=(),
                    at="2026-09-20T17:40:00+00:00",
                )


if __name__ == "__main__":
    unittest.main()
