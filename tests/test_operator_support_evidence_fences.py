"""Causal and report-pair integrity checks; no compiled Windows artifact required."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from autosport.learning_evidence_projection import verified_learning_projection
from autosport.operator_support import (
    OperatorSupportTrace, SupportExportError,
    verify_report_pair, strategy_goal_uk, agent_purpose_uk,
)
from autosport.strategies import available_strategies

NOW = datetime(2026, 10, 16, 0, 15, tzinfo=timezone.utc)


class FakeRegistry:
    def __init__(self, path):
        self.path = path

    def causal_records(self, name, *, as_of):
        assert as_of == NOW.isoformat()
        return ()


class FakeClock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self):
        self.now += timedelta(seconds=1)


def test_future_supervisor_snapshot_cannot_pollute_historical_learning(tmp_path):
    (tmp_path / 'scientific-registry.json').write_text('{}')
    (tmp_path / 'research-supervisor.json').write_text('{}')

    def factory(path, registry):
        run = SimpleNamespace(
            run_id='future', updated_at='2026-10-18T01:00:00Z',
            phase=SimpleNamespace(value='COMPLETE'),
            status=SimpleNamespace(value='COMPLETED'),
        )
        return SimpleNamespace(list_runs=lambda: (run,))

    with pytest.raises(ValueError, match='causal cutoff'):
        verified_learning_projection(
            tmp_path, as_of=NOW, registry_factory=FakeRegistry,
            supervisor_factory=factory,
        )


def test_future_agent_loop_snapshot_cannot_pollute_historical_learning(tmp_path):
    (tmp_path / 'agent-loop.json').write_text('{}')

    def factory(path):
        return SimpleNamespace(snapshot=lambda: SimpleNamespace(
            updated_at='2026-10-17T00:20:00+00:00'
        ))

    with pytest.raises(ValueError, match='causal cutoff'):
        verified_learning_projection(tmp_path, as_of=NOW, agent_loop_factory=factory)


def test_agent_loop_non_utc_or_invalid_timestamp_is_never_accepted(tmp_path):
    (tmp_path / 'agent-loop.json').write_text('{}')
    for value in ('2026-10-15T12:00:00', 'invalid'):
        def factory(path):
            return SimpleNamespace(snapshot=lambda: SimpleNamespace(updated_at=value))
        with pytest.raises(ValueError, match='timezone|timestamp'):
            verified_learning_projection(tmp_path, as_of=NOW, agent_loop_factory=factory)


def test_exported_diagnostic_and_agents_are_one_verified_pair(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    clock = FakeClock()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=clock)
    try:
        trace.record(component='runtime', action='product.tick', status='active', cycle=1)
        diagnostic, agents = trace.export_reports(
            tmp_path, selected_strategy='observe-only-v1'
        )
        evidence = verify_report_pair(diagnostic, agents)
        assert json.loads(diagnostic.read_text())['report_pair_sha256'] == evidence
        assert json.loads(agents.read_text())['report_pair_sha256'] == evidence
        tampered = json.loads(agents.read_text())
        tampered['current_strategy_id'] = 'baseline-v1'
        agents.write_text(json.dumps(tampered), encoding='utf-8')
        with pytest.raises(SupportExportError, match='digest mismatch'):
            verify_report_pair(diagnostic, agents)
    finally:
        trace.close()


def test_mixed_reports_from_different_exports_are_rejected(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    clock = FakeClock()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=clock)
    try:
        first, _ = trace.export_reports(tmp_path, selected_strategy='observe-only-v1')
        previous = first.read_bytes()
        clock.advance()
        trace.record(component='runtime', action='product.tick', status='active', cycle=7)
        _, agents = trace.export_reports(tmp_path, selected_strategy='observe-only-v1')
        first.write_bytes(previous)
        with pytest.raises(SupportExportError, match='different snapshot identities'):
            verify_report_pair(first, agents)
    finally:
        trace.close()


def test_every_declared_agent_goal_has_ukrainian_operator_explanation():
    for strategy in available_strategies():
        assert any('а' <= c <= 'я' for c in strategy_goal_uk(strategy.strategy_id))
        for agent in strategy.agent_names:
            assert any('а' <= c <= 'я' for c in agent_purpose_uk(agent))


def test_agent_report_reads_only_the_selected_canonical_strategy_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    selected = workspace / "strategies" / "research-replay-v1-identity"
    selected.mkdir(parents=True)
    (selected / "decisions.jsonl").write_text(
        "raw secret payload must never appear in a support report", encoding="utf-8"
    )
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=FakeClock())
    try:
        _, agent_file = trace.export_reports(
            tmp_path,
            selected_strategy="research-replay-v1",
            research_plan_loaded=True,
            agent_workspace=selected,
        )
        report = json.loads(agent_file.read_text(encoding="utf-8"))
        assert report["agent_workspace_scope"] == "CANONICAL_STRATEGY"
        decisions = next(item for item in report["memory_and_learning_artifacts"]
                         if item["type"] == "decisions.jsonl")
        assert decisions["state"] == "PRESENT_UNVERIFIED"
        assert "raw secret payload" not in agent_file.read_text(encoding="utf-8")

        _, base_file = trace.export_reports(
            tmp_path, selected_strategy="observe-only-v1"
        )
        baseline = json.loads(base_file.read_text(encoding="utf-8"))
        assert baseline["agent_workspace_scope"] == "ROOT"
        base_decisions = next(
            item for item in baseline["memory_and_learning_artifacts"]
            if item["type"] == "decisions.jsonl"
        )
        assert base_decisions["state"] == "NOT_PRESENT"
    finally:
        trace.close()


def test_agent_report_refuses_foreign_and_symlink_strategy_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "unrelated"
    external.mkdir()
    (workspace / "strategies").mkdir()
    link = workspace / "strategies" / "outbound"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("creating directory symlinks requires host privileges")
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=FakeClock())
    try:
        for invalid in (external, link, workspace / "arbitrary" / "subdir"):
            with pytest.raises(SupportExportError, match="outside canonical"):
                trace.export_reports(
                    tmp_path, selected_strategy="observe-only-v1",
                    agent_workspace=invalid,
                )
    finally:
        trace.close()


def test_webview_learning_and_report_paths_follow_selected_strategy_identity():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    source = (root / "src/autosport/windows_webview_shell.py").read_text(
        encoding="utf-8"
    )
    assert "selected_agent_workspace = self._product_runtime_target_workspace()" in source
    assert "self.learning_status_worker.start(selected_agent_workspace)" in source
    assert "agent_workspace=selected_agent_workspace" in source
    assert "workspace_for_strategy(self.workspace, strategy_id, plan)" in source

def test_diagnostics_and_science_use_one_causal_instant(tmp_path):
    class TickingClock:
        def __init__(self):
            self.now = NOW

        def __call__(self):
            instant = self.now
            self.now += timedelta(seconds=1)
            return instant

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=TickingClock())
    try:
        a, b = trace.export_reports(tmp_path, selected_strategy="observe-only-v1")
        diagnostics = json.loads(a.read_text(encoding="utf-8"))
        agents = json.loads(b.read_text(encoding="utf-8"))
        assert datetime.fromisoformat(
            diagnostics["generated_at_utc"].replace("Z", "+00:00")
        ) == datetime.fromisoformat(
            agents["verified_learning_evidence"]["science"]["cutoff_utc"]
        )
    finally:
        trace.close()


def test_agent_report_rejects_in_scope_strategy_symlink_alias(tmp_path):
    root = tmp_path / "workspace"
    strategies = root / "strategies"
    strategies.mkdir(parents=True)
    target = strategies / "strategy-a"
    target.mkdir()
    alias = strategies / "strategy-b"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on test host")
    trace = OperatorSupportTrace(root, auto_start=False, clock=FakeClock())
    try:
        with pytest.raises(SupportExportError, match="symbolic link"):
            trace.export_reports(
                tmp_path, selected_strategy="observe-only-v1",
                agent_workspace=alias,
            )
    finally:
        trace.close()


def test_diagnostic_recorder_fault_not_masked_by_dropped_events(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    trace = OperatorSupportTrace(root, auto_start=False, clock=FakeClock())
    try:
        with trace._lost_activity_lock:
            trace._lost_activity_count = 3
        with trace._guard:
            trace._fault = "RECORDER_ERROR"
        path, _ = trace.export_reports(
            tmp_path, selected_strategy="observe-only-v1"
        )
        coverage = json.loads(path.read_text(encoding="utf-8"))["coverage"]
        assert coverage["recorder_status"] == "RECORDER_ERROR"
        assert coverage["recorder_fault_present"] is True


def test_bad_science_never_blocks_verified_diagnostics(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=FakeClock())
    try:
        trace.record(component="runtime", action="product.tick", status="active", cycle=23)

        def corrupt_science(*args, **kwargs):
            raise ValueError("secret-token /private/learning.json")

        monkeypatch.setattr("autosport.operator_support.verified_learning_projection", corrupt_science)
        diagnostic, agents = trace.export_reports(tmp_path, selected_strategy="observe-only-v1")
        assert verify_report_pair(diagnostic, agents)
        d = json.loads(diagnostic.read_text(encoding="utf-8"))
        a = json.loads(agents.read_text(encoding="utf-8"))
        assert d["activities"][-1]["cycle"] == 23
        assert d["coverage"]["scientific_evidence_verification_failed"] is True
        assert d["coverage"]["artifact_inventory_verification_failed"] is False
        assert a["verified_learning_evidence"]["science"]["state"] == "VERIFICATION_FAILED"
        assert a["learning_verification"] == "SOURCE_VERIFICATION_FAILED_NO_LEARNING_CLAIM"
        assert a["verified_learning_evidence"]["proven_live_model_learning"] is False
        assert "secret-token" not in agents.read_text(encoding="utf-8")
        assert "/private/learning.json" not in diagnostic.read_text(encoding="utf-8")
    finally:
        trace.close()


def test_malformed_science_projection_fails_closed_without_erasing_diagnostics(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=FakeClock())
    try:
        monkeypatch.setattr(
            "autosport.operator_support.verified_learning_projection",
            lambda *args, **kwargs: {"science": {"state": "UNTRUSTED_SUCCESS"}},
        )
        diagnostic, agent_file = trace.export_reports(tmp_path, selected_strategy="observe-only-v1")
        assert verify_report_pair(diagnostic, agent_file)
        d = json.loads(diagnostic.read_text(encoding="utf-8"))
        a = json.loads(agent_file.read_text(encoding="utf-8"))
        assert d["coverage"]["scientific_evidence_verification_failed"] is True
        assert a["verified_learning_evidence"]["science"]["state"] == "VERIFICATION_FAILED"
        assert a["verified_learning_evidence"]["proven_live_model_learning"] is False
        assert a["memory_and_learning_artifacts"]
    finally:
        trace.close()


def test_bad_inventory_does_not_erase_valid_science(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    foreign = tmp_path / "secret.txt"
    foreign.write_text("hidden-secret", encoding="utf-8")
    try:
        (workspace / "decisions.jsonl").symlink_to(foreign)
    except OSError:
        pytest.skip("symlinks unavailable")
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=FakeClock())
    try:
        diagnostic, agents = trace.export_reports(tmp_path, selected_strategy="observe-only-v1")
        assert verify_report_pair(diagnostic, agents)
        d = json.loads(diagnostic.read_text(encoding="utf-8"))
        a = json.loads(agents.read_text(encoding="utf-8"))
        assert d["coverage"]["artifact_inventory_verification_failed"] is True
        assert d["coverage"]["scientific_evidence_verification_failed"] is False
        assert a["memory_and_learning_artifacts"] == []
        assert a["verified_learning_evidence"]["science"]["state"] == "NOT_PRESENT"
        assert "hidden-secret" not in agents.read_text(encoding="utf-8")
    finally:
        trace.close()


def test_recomputed_hash_cannot_turn_failed_science_into_success(tmp_path, monkeypatch):
    import hashlib
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trace = OperatorSupportTrace(workspace, auto_start=False, clock=FakeClock())
    try:
        def corrupt(*args, **kwargs):
            raise ValueError("broken")
        monkeypatch.setattr("autosport.operator_support.verified_learning_projection", corrupt)
        diagnostic, agent_file = trace.export_reports(tmp_path, selected_strategy="observe-only-v1")
        d = json.loads(diagnostic.read_text(encoding="utf-8"))
        a = json.loads(agent_file.read_text(encoding="utf-8"))
        d.pop("report_pair_sha256")
        a.pop("report_pair_sha256")
        a["verified_learning_evidence"]["science"]["state"] = "CANONICAL_REGISTRY_VERIFIED"
        digest = hashlib.sha256(json.dumps({"diagnostics": d, "agents": a},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False).encode("utf-8")).hexdigest()
        d["report_pair_sha256"] = digest
        a["report_pair_sha256"] = digest
        diagnostic.write_text(json.dumps(d), encoding="utf-8")
        agent_file.write_text(json.dumps(a), encoding="utf-8")
        with pytest.raises(SupportExportError, match="failed scientific evidence"):
            verify_report_pair(diagnostic, agent_file)
    finally:
        trace.close()


@pytest.mark.parametrize("projection", [
    {"science": {"state": "NOT_PRESENT", "cutoff_utc": "2030-01-01T00:00:00+00:00"},
     "proven_live_model_learning": False,
     "economic_improvement_proven_by_this_report": False,
     "has_live_execution_authority": False},
    {"science": {"state": "CANONICAL_REGISTRY_VERIFIED",
                 "cutoff_utc": NOW.isoformat()},
     "proven_live_model_learning": True,
     "economic_improvement_proven_by_this_report": False,
     "has_live_execution_authority": False},
])
def test_false_science_cutoff_or_claim_preserves_diagnostics_with_failure_marker(
    tmp_path, monkeypatch, projection,
):
    root = tmp_path / "workspace"
    root.mkdir()
    trace = OperatorSupportTrace(root, auto_start=False, clock=FakeClock())
    monkeypatch.setattr(
        "autosport.operator_support.verified_learning_projection",
        lambda *args, **kwargs: projection,
    )
    try:
        diag, agents = trace.export_reports(
            tmp_path, selected_strategy="observe-only-v1"
        )
        assert verify_report_pair(diag, agents)
        d = json.loads(diag.read_text(encoding="utf-8"))
        a = json.loads(agents.read_text(encoding="utf-8"))
        assert d["coverage"]["scientific_evidence_verification_failed"] is True
        assert a["verified_learning_evidence"]["science"]["state"] == "VERIFICATION_FAILED"
        assert a["verified_learning_evidence"]["proven_live_model_learning"] is False
    finally:
        trace.close()


def test_recomputed_pair_digest_cannot_claim_live_training_from_unverified_source(tmp_path):
    import hashlib
    root = tmp_path / "workspace"
    root.mkdir()
    trace = OperatorSupportTrace(root, auto_start=False, clock=FakeClock())
    try:
        diagnostic, agent_file = trace.export_reports(
            tmp_path, selected_strategy="observe-only-v1"
        )
        d = json.loads(diagnostic.read_text(encoding="utf-8"))
        a = json.loads(agent_file.read_text(encoding="utf-8"))
        d.pop("report_pair_sha256")
        a.pop("report_pair_sha256")
        a["verified_learning_evidence"]["proven_live_model_learning"] = True
        digest = hashlib.sha256(json.dumps(
            {"diagnostics": d, "agents": a}, sort_keys=True,
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        d["report_pair_sha256"] = digest
        a["report_pair_sha256"] = digest
        diagnostic.write_text(json.dumps(d), encoding="utf-8")
        agent_file.write_text(json.dumps(a), encoding="utf-8")
        with pytest.raises(SupportExportError, match="cannot claim proven"):
            verify_report_pair(diagnostic, agent_file)
    finally:
        trace.close()
        assert coverage["lost_activity_count"] == 3
        assert coverage["complete_activity_capture_proven"] is False
    finally:
        trace.close()
