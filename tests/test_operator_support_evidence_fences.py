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
