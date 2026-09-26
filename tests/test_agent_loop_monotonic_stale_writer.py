from __future__ import annotations

import pytest

from autosport.agent_loop import (
    AgentLoopPhase,
    AgentLoopRuntime,
    StaleAgentLoopStateError,
)
from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    Observation,
)


GOAL_SHA = "3" * 64
RISK_SHA = "4" * 64
SOURCE_SHA = "1" * 64
CONFIG_SHA = "2" * 64


def _environment() -> CausalLearningEnvironment:
    identity = EnvironmentIdentity(
        source_id="paper-source-v1",
        config_id="agent-loop-stale-writer-config-v1",
        data_id="causal-dataset-v1",
        protocol_id="agent-loop-stale-writer-protocol-v1",
        cutoff_ts="2026-09-19T14:00:00Z",
        seed=73,
    )
    return CausalLearningEnvironment(
        identity,
        episode_key="agent-loop-stale-writer-episode-1",
        policy_id="policy-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )


def test_preopened_stale_writer_cannot_create_sibling_successor(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "machine-state"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
    )
    environment = _environment()
    winner = AgentLoopRuntime.initialize_pristine(
        workspace / "agent-loop.json",
        loop_id="loop-stale-writer-1",
        environment_checkpoint=environment.checkpoint(),
        policy_id="policy-v1",
        economic_goal_fingerprint=GOAL_SHA,
        risk_fingerprint=RISK_SHA,
        source_sha256=SOURCE_SHA,
        config_sha256=CONFIG_SHA,
        at="2026-09-19T13:00:00Z",
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-19T13:00:00Z",
        available_at="2026-09-19T13:00:01Z",
        evidence=(("market_state", "stale-writer-snapshot-1"),),
    )
    at_n = winner.begin_observation(
        observation,
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )
    assert at_n.phase is AgentLoopPhase.OBSERVE

    stale = AgentLoopRuntime(winner.path)
    authority = winner._monotonic_authority(winner.path)
    history_at_n = tuple(
        record.record_sha256 for record in authority.read_history()
    )

    at_n_plus_1 = winner.advance(
        expected=AgentLoopPhase.OBSERVE,
        at="2026-09-19T13:00:02Z",
    )
    assert at_n_plus_1.phase is AgentLoopPhase.ASSESS
    bytes_at_n_plus_1 = winner.path.read_bytes()
    history_at_n_plus_1 = tuple(
        record.record_sha256 for record in authority.read_history()
    )
    assert history_at_n_plus_1 != history_at_n

    with pytest.raises(
        StaleAgentLoopStateError,
        match="expected phase OBSERVE, current phase is ASSESS",
    ):
        stale.advance(
            expected=AgentLoopPhase.OBSERVE,
            at="2026-09-19T13:00:03Z",
        )

    assert winner.path.read_bytes() == bytes_at_n_plus_1
    assert tuple(
        record.record_sha256 for record in authority.read_history()
    ) == history_at_n_plus_1

    reopened = AgentLoopRuntime(winner.path)
    at_n_plus_2 = reopened.advance(
        expected=AgentLoopPhase.ASSESS,
        at="2026-09-19T13:00:04Z",
    )
    assert at_n_plus_2.sequence == at_n_plus_1.sequence + 1
    assert at_n_plus_2.state_sha256 != at_n_plus_1.state_sha256
    assert len(authority.read_history()) > len(history_at_n_plus_1)
