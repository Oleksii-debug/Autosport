from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


_BASE_PATH = Path(__file__).with_name("_paper_campaign_runtime_tests_base.py")
_SPEC = importlib.util.spec_from_file_location(
    "_paper_campaign_runtime_tests_base_process_kill",
    _BASE_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("cannot load campaign runtime regression base")
_legacy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_legacy)


_CHILD_EXIT_CODE = 73
_CHILD_CODE = textwrap.dedent(
    r"""
    import importlib.util
    import json
    import os
    import sys
    from pathlib import Path

    root = Path(sys.argv[1])
    base_path = Path(sys.argv[2])
    mode = sys.argv[3]
    spec = importlib.util.spec_from_file_location(
        "_paper_campaign_runtime_tests_base_process_kill_child",
        base_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load campaign runtime regression base")
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)

    (
        leg,
        _book,
        ticket_id,
        _decision,
        _environment,
        _baseline,
        _observation,
        bridge,
        _runtime,
    ) = legacy._fixture(root)

    marker = {"ticket_id": ticket_id, "mode": mode}
    if mode == "after_learning_ack":
        resolutions = legacy._settle(root, leg, "win")
        transitions = bridge.reconcile_after_settlement(
            paper_book_path=root / "paper_book.json",
            resolutions=resolutions,
            settled_ticket_ids=(ticket_id,),
            at=legacy.T4,
        )
        if len(transitions) != 1:
            raise AssertionError("expected one durable settlement->learning transition")
        marker["transition_id"] = transitions[0]
    elif mode == "after_settlement_before_outbox":
        resolution = legacy.SettlementResolution(
            event_identity=f"campaign-provider:{leg.event_id}",
            settlement_ref="campaign-result-1",
            quote_outcomes={leg.quote_key: "win"},
            evidence_id="campaign-evidence-1",
            evidence_sha256="c" * 64,
            available_at=legacy.T4,
        )
        prepared = bridge.prepare_settlement(
            paper_book_path=root / "paper_book.json",
            resolutions=(resolution,),
            at=legacy.T4,
        )
        if prepared != (ticket_id,):
            raise AssertionError("expected one durable pre-settlement intent")
        # Mutate the canonical PaperBook only after the settlement evidence has
        # been durably staged. The process exits before bridge reconciliation can
        # create a learner outbox or AgentLoop acknowledgement.
        legacy._settle(root, leg, "win")
    else:
        raise AssertionError(f"unsupported process-kill mode: {mode}")

    (root / "process-kill-ready.json").write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    # Deliberately bypass normal interpreter cleanup. Recovery must trust only
    # durable files written before this point.
    os._exit(73)
    """
)


def _fresh_runtime(root: Path):
    goal = _legacy.EconomicGoalContract(
        goal_id="campaign-goal",
        revision=1,
        bankroll_id="campaign-bankroll",
        currency="USD",
    )
    risk = _legacy.PaperRiskPolicy(economic_goal=goal)
    identity = _legacy.EnvironmentIdentity(
        source_id="campaign-source",
        config_id="campaign-config",
        data_id="campaign-data",
        protocol_id="campaign-protocol",
        cutoff_ts="2026-09-20T03:01:00+00:00",
        seed=17,
    )
    pristine = _legacy.CausalLearningEnvironment(
        identity,
        episode_key="campaign-episode",
        policy_id="campaign-policy",
        admissible_actions=frozenset({"PAPER_PROPOSAL"}),
    )
    baseline = pristine.checkpoint()
    resumed = _legacy.CausalLearningEnvironment.resume(
        identity,
        episode_key="campaign-episode",
        policy_id="campaign-policy",
        admissible_actions=frozenset({"PAPER_PROPOSAL"}),
        checkpoint=baseline,
    )
    bridge = _legacy.PaperSettlementLearningBridge(
        root / "paper-learning-bridge.json",
        paper_book_path=root / "paper_book.json",
        decision_ledger=_legacy.JsonlDecisionLedger(root / "decisions.jsonl"),
        agent_loop=_legacy.AgentLoopRuntime(root / "agent-loop.json"),
        economic_goal=goal,
        risk_policy=risk,
    )
    return _legacy.PaperCampaignRuntime(
        environment=resumed,
        settlement_bridge=bridge,
    )


def _run_abrupt_child(root: Path, mode: str) -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    src_path = str(repo_root / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        src_path
        if not existing_pythonpath
        else src_path + os.pathsep + existing_pythonpath
    )
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD_CODE,
            str(root),
            str(_BASE_PATH.resolve()),
            mode,
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if child.returncode != _CHILD_EXIT_CODE:
        raise AssertionError(
            f"child returned {child.returncode}; stderr: {child.stderr}"
        )
    marker = json.loads(
        (root / "process-kill-ready.json").read_text(encoding="utf-8")
    )
    if marker.get("mode") != mode:
        raise AssertionError("process-kill marker mode mismatch")
    return marker


class PaperCampaignProcessKillRecoveryTests(unittest.TestCase):
    def _assert_terminal_exactly_once(
        self,
        *,
        root: Path,
        ticket_id: str,
        transition_id: str,
    ) -> None:
        recovered = _fresh_runtime(root)
        first = recovered.finalize_ticket(ticket_id=ticket_id, at=_legacy.T4)

        # Simulate another operator/process retry from durable state. The same
        # ticket must resolve to the same receipt without duplicate evidence.
        recovered_again = _fresh_runtime(root)
        second = recovered_again.finalize_ticket(ticket_id=ticket_id, at=_legacy.T4)

        self.assertEqual(first, second)
        self.assertEqual(first.transition_id, transition_id)

        after = json.loads(
            (root / "agent-loop.json").read_text(encoding="utf-8")
        )
        self.assertEqual(after["phase"], _legacy.AgentLoopPhase.CHECKPOINT.value)
        self.assertEqual(len(after["resolutions"]), 1)
        self.assertEqual(len(after["attributions"]), 1)
        self.assertEqual(len(after["postmortems"]), 1)
        self.assertEqual(after["resolutions"][0]["transition_id"], transition_id)
        self.assertEqual(after["resolutions"][0]["reward_value"], "10.00")
        self.assertEqual(after["checkpointed_transition_id"], transition_id)

        bridge_state = json.loads(
            (root / "paper-learning-bridge.json").read_text(encoding="utf-8")
        )
        self.assertEqual(tuple(bridge_state["bindings"]), (ticket_id,))
        binding = bridge_state["bindings"][ticket_id]
        self.assertEqual(binding["status"], "ACKED")
        self.assertEqual(binding["ack"]["transition_id"], transition_id)

    def test_abrupt_exit_after_durable_learning_converges_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = _run_abrupt_child(root, "after_learning_ack")
            ticket_id = marker["ticket_id"]
            transition_id = marker["transition_id"]

            # The child exited before campaign terminalization. Only the canonical
            # settlement->learning publication may already exist.
            before = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(before["resolutions"]), 1)
            self.assertEqual(before["resolutions"][0]["transition_id"], transition_id)
            self.assertEqual(before["attributions"], [])
            self.assertEqual(before["postmortems"], [])

            self._assert_terminal_exactly_once(
                root=root,
                ticket_id=ticket_id,
                transition_id=transition_id,
            )

    def test_abrupt_exit_after_settlement_before_learner_outbox_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = _run_abrupt_child(root, "after_settlement_before_outbox")
            ticket_id = marker["ticket_id"]

            # Settlement evidence was staged before the PaperBook mutation, but
            # the child died before reconcile_after_settlement. Thus no learner
            # resolution/outbox/ack may have been published yet.
            agent_before = json.loads(
                (root / "agent-loop.json").read_text(encoding="utf-8")
            )
            self.assertEqual(agent_before["resolutions"], [])
            self.assertEqual(agent_before["attributions"], [])
            self.assertEqual(agent_before["postmortems"], [])

            bridge_before = json.loads(
                (root / "paper-learning-bridge.json").read_text(encoding="utf-8")
            )
            binding_before = bridge_before["bindings"][ticket_id]
            self.assertEqual(binding_before["status"], "BOUND")
            self.assertIsNotNone(binding_before["settlement_intent"])
            self.assertIsNone(binding_before["outbox"])
            self.assertIsNone(binding_before["ack"])

            # A fresh process can recover solely from the durable intent plus the
            # already-settled PaperBook. No repeated external settlement payload
            # is required and no second settlement authority is invented.
            recovered = _fresh_runtime(root)
            transitions = recovered.settlement_bridge.reconcile_after_settlement(
                paper_book_path=root / "paper_book.json",
                resolutions=(),
                settled_ticket_ids=(),
                at=_legacy.T4,
            )
            self.assertEqual(len(transitions), 1)
            transition_id = transitions[0]

            self._assert_terminal_exactly_once(
                root=root,
                ticket_id=ticket_id,
                transition_id=transition_id,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
