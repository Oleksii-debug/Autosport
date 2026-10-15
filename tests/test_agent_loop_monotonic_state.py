from __future__ import annotations

import autosport.agent_loop as agent_loop_module
import pytest

from autosport.agent_loop import AgentLoopError, AgentLoopPhase, AgentLoopRuntime
from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    Observation,
)
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority


GOAL_SHA = "3" * 64
RISK_SHA = "4" * 64
SOURCE_SHA = "1" * 64
CONFIG_SHA = "2" * 64


def _environment() -> CausalLearningEnvironment:
    identity = EnvironmentIdentity(
        source_id="paper-source-v1",
        config_id="agent-loop-monotonic-config-v1",
        data_id="causal-dataset-v1",
        protocol_id="agent-loop-monotonic-protocol-v1",
        cutoff_ts="2026-09-19T14:00:00Z",
        seed=71,
    )
    return CausalLearningEnvironment(
        identity,
        episode_key="agent-loop-monotonic-episode-1",
        policy_id="policy-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )


def _runtime(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    authority_root = tmp_path / "machine-state"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
    )
    environment = _environment()
    runtime = AgentLoopRuntime.initialize_pristine(
        workspace / "agent-loop.json",
        loop_id="loop-monotonic-1",
        environment_checkpoint=environment.checkpoint(),
        policy_id="policy-v1",
        economic_goal_fingerprint=GOAL_SHA,
        risk_fingerprint=RISK_SHA,
        source_sha256=SOURCE_SHA,
        config_sha256=CONFIG_SHA,
        at="2026-09-19T13:00:00Z",
    )
    return environment, runtime


def _observation(environment: CausalLearningEnvironment) -> Observation:
    return Observation(
        environment_id=environment.environment_id,
        observed_at="2026-09-19T13:00:00Z",
        available_at="2026-09-19T13:00:01Z",
        evidence=(("market_state", "monotonic-snapshot-1"),),
    )


def _begin(runtime: AgentLoopRuntime, environment: CausalLearningEnvironment) -> None:
    runtime.begin_observation(
        _observation(environment),
        environment_identity=environment.identity,
        at="2026-09-19T13:00:01Z",
    )


def test_valid_older_agent_loop_state_is_rejected_after_newer_commit(
    tmp_path,
    monkeypatch,
) -> None:
    environment, runtime = _runtime(tmp_path, monkeypatch)
    _begin(runtime, environment)
    older_valid_bytes = runtime.path.read_bytes()

    advanced = runtime.advance(
        expected=AgentLoopPhase.OBSERVE,
        at="2026-09-19T13:00:02Z",
    )
    latest_valid_bytes = runtime.path.read_bytes()
    assert advanced.phase is AgentLoopPhase.ASSESS

    runtime.path.write_bytes(older_valid_bytes)

    with pytest.raises(AgentLoopError, match="monotonic|rolled back|authority"):
        AgentLoopRuntime(runtime.path)

    runtime.path.write_bytes(latest_valid_bytes)
    reopened = AgentLoopRuntime(runtime.path)
    assert reopened.snapshot().phase is AgentLoopPhase.ASSESS
    assert reopened.snapshot().state_sha256 == advanced.state_sha256


def test_workspace_move_preserves_agent_loop_rollback_fence(
    tmp_path,
    monkeypatch,
) -> None:
    environment, runtime = _runtime(tmp_path, monkeypatch)
    _begin(runtime, environment)
    older_valid_bytes = runtime.path.read_bytes()
    advanced = runtime.advance(
        expected=AgentLoopPhase.OBSERVE,
        at="2026-09-19T13:00:02Z",
    )

    moved_workspace = tmp_path / "workspace-moved"
    runtime.path.parent.rename(moved_workspace)
    moved_path = moved_workspace / runtime.path.name

    reopened = AgentLoopRuntime(moved_path)
    assert reopened.snapshot().state_sha256 == advanced.state_sha256
    assert reopened.snapshot().phase is AgentLoopPhase.ASSESS

    moved_path.write_bytes(older_valid_bytes)
    with pytest.raises(AgentLoopError, match="monotonic|rolled back|authority"):
        AgentLoopRuntime(moved_path)


def test_missing_agent_loop_state_cannot_rebootstrap_after_authority_exists(
    tmp_path,
    monkeypatch,
) -> None:
    environment, runtime = _runtime(tmp_path, monkeypatch)
    _begin(runtime, environment)
    path = runtime.path
    path.unlink()

    with pytest.raises(AgentLoopError, match="monotonic|prior history|missing"):
        AgentLoopRuntime.initialize_pristine(
            path,
            loop_id="loop-monotonic-1",
            environment_checkpoint=environment.checkpoint(),
            policy_id="policy-v1",
            economic_goal_fingerprint=GOAL_SHA,
            risk_fingerprint=RISK_SHA,
            source_sha256=SOURCE_SHA,
            config_sha256=CONFIG_SHA,
            at="2026-09-19T13:00:02Z",
        )

    assert not path.exists()


def test_crash_before_local_publish_aborts_prepare_and_exact_retry_converges(
    tmp_path,
    monkeypatch,
) -> None:
    environment, runtime = _runtime(tmp_path, monkeypatch)
    original_atomic_write_json = agent_loop_module.atomic_write_json

    def fail_publish(*_args, **_kwargs):
        raise OSError("injected crash before local AgentLoop publish")

    monkeypatch.setattr(agent_loop_module, "atomic_write_json", fail_publish)
    with pytest.raises(OSError, match="injected crash"):
        _begin(runtime, environment)

    monkeypatch.setattr(
        agent_loop_module,
        "atomic_write_json",
        original_atomic_write_json,
    )
    reopened = AgentLoopRuntime(runtime.path)
    assert reopened.snapshot().phase is AgentLoopPhase.BOOTSTRAP
    assert reopened.snapshot().sequence == 0

    _begin(reopened, environment)
    assert reopened.snapshot().phase is AgentLoopPhase.OBSERVE
    assert reopened.snapshot().sequence == 1


def test_crash_after_local_publish_finishes_exact_pending_commit_on_restart(
    tmp_path,
    monkeypatch,
) -> None:
    environment, runtime = _runtime(tmp_path, monkeypatch)
    _begin(runtime, environment)
    original_commit = MonotonicWorkspaceAuthority.commit

    def fail_commit(self, **_kwargs):
        raise RuntimeError("injected crash after local AgentLoop publish")

    monkeypatch.setattr(MonotonicWorkspaceAuthority, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected crash"):
        runtime.advance(
            expected=AgentLoopPhase.OBSERVE,
            at="2026-09-19T13:00:02Z",
        )

    monkeypatch.setattr(MonotonicWorkspaceAuthority, "commit", original_commit)
    reopened = AgentLoopRuntime(runtime.path)
    snapshot = reopened.snapshot()
    assert snapshot.phase is AgentLoopPhase.ASSESS
    assert snapshot.sequence == 2

    restarted_again = AgentLoopRuntime(runtime.path)
    assert restarted_again.snapshot().state_sha256 == snapshot.state_sha256
    assert restarted_again.snapshot().sequence == 2


def test_monotonic_authority_root_must_be_outside_agent_loop_workspace(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((workspace / ".authority").resolve()),
    )
    environment = _environment()
    path = workspace / "agent-loop.json"

    with pytest.raises(AgentLoopError, match="disjoint|monotonic"):
        AgentLoopRuntime.initialize_pristine(
            path,
            loop_id="loop-monotonic-1",
            environment_checkpoint=environment.checkpoint(),
            policy_id="policy-v1",
            economic_goal_fingerprint=GOAL_SHA,
            risk_fingerprint=RISK_SHA,
            source_sha256=SOURCE_SHA,
            config_sha256=CONFIG_SHA,
            at="2026-09-19T13:00:00Z",
        )

    assert not path.exists()
