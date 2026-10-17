from __future__ import annotations
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import pytest
from autosport.operator_support import OperatorSupportTrace, SupportExportError

class Clock:
    def __init__(self):self.now = datetime(2026,10,16,10,0,0,tzinfo=timezone.utc)
    def __call__(self):return self.now
    def advance(self,seconds=1):self.now += timedelta(seconds=seconds)

def trace(tmp_path):
    workspace=tmp_path/'workspace'
    workspace.mkdir()
    c=Clock()
    return OperatorSupportTrace(workspace,clock=c,auto_start=False),c,workspace

def test_export_has_every_second_and_different_agent_report(tmp_path):
    t,c,w=trace(tmp_path)
    t.record(component='interface',action='product_runtime.start',status='completed')
    for _ in range(4):
        t.heartbeat()
        c.advance(1)
    t.record(component='runtime',action='product.tick',status='active',cycle=42)
    diagnostics, agents = t.export_reports(tmp_path,selected_strategy='research-replay-v1', research_plan_loaded=True)
    first=json.loads(diagnostics.read_text())
    second=json.loads(agents.read_text())
    assert first['coverage']['record_count'] == 7
    assert first['coverage']['missing_heartbeat_seconds_between_observations'] == 0
    assert sum(x['heartbeat'] for x in first['seconds'])==4
    assert first['activities'][-1]['cycle']==42
    assert first['real_money_execution'] is False
    assert second['research_plan_loaded'] is True
    assert second['current_strategy_id']=='research-replay-v1'
    assert 'market-mirror' in second['current_agent_names']
    assert second['learning_verification']=='NOT_INFERRED_FROM_CODE_OR_ARTIFACT_PRESENCE'
    t.close()

def test_missing_second_is_explicit_not_invented(tmp_path):
    t,c,w=trace(tmp_path)
    t.heartbeat()
    c.advance(8)
    t.heartbeat()
    p,_=t.export_reports(tmp_path,selected_strategy='observe-only-v1')
    d=json.loads(p.read_text())
    assert d['coverage']['missing_heartbeat_seconds_between_observations']==7
    assert len([r for r in d['seconds'] if r['heartbeat']])==2
    t.close()

def test_raw_secret_and_arbitrary_payload_cannot_be_logged(tmp_path):
    t,c,w=trace(tmp_path)
    for kwargs in (
        {'component':'credential','action':'replay.run','status':'completed'},
        {'component':'agent','action':'raw.prompt','status':'completed'},
        {'component':'agent','action':'replay.run','status':'token-secret'},
        {'component':'agent','action':'replay.run','status':'completed','cycle':True},
    ):
        with pytest.raises(ValueError): t.record(**kwargs)
    assert len(t._journal.snapshot()) == 1
    t.close()

def test_output_inside_workspace_and_sibling_rejected(tmp_path):
    t,c,w=trace(tmp_path)
    with pytest.raises(ValueError): t.export_reports(w,selected_strategy='observe-only-v1')
    sibling=tmp_path/'sibling';sibling.mkdir()
    with pytest.raises(ValueError): t.export_reports(sibling,selected_strategy='observe-only-v1')
    t.close()

def test_day_rotation_keeps_historical_records(tmp_path):
    t,c,w=trace(tmp_path)
    t.heartbeat()
    c.advance(86400)
    t.heartbeat()
    d,_=t.export_reports(tmp_path,selected_strategy='observe-only-v1')
    report=json.loads(d.read_text())
    assert len([x for x in report['seconds'] if x['heartbeat']])==2
    assert report['coverage']['missing_heartbeat_seconds_between_observations']==86399
    t.close()

def test_recorder_fault_exports_only_verified_partial_evidence(tmp_path):
    t,c,w=trace(tmp_path)
    t.heartbeat()
    t._fault='RECORDER_ERROR'
    t.observe(component='agent', action='learning.observed', status='active')
    diagnostic,agent=t.export_reports(tmp_path,selected_strategy='observe-only-v1')
    payload=json.loads(diagnostic.read_text())
    assert payload['coverage']['recorder_status']=='RECORDER_ERROR'
    assert payload['coverage']['complete_activity_capture_proven'] is False
    assert payload['coverage']['record_count']==2
    assert json.loads(agent.read_text())['learning_verification']=='NOT_INFERRED_FROM_CODE_OR_ARTIFACT_PRESENCE'
    t.close()

def test_existing_memory_not_exfiltrated(tmp_path):
    t,c,w=trace(tmp_path)
    (w/'decisions.jsonl').write_text('secret token password',encoding='utf-8')
    _,p=t.export_reports(tmp_path,selected_strategy='observe-only-v1')
    contents=p.read_text(encoding='utf-8')
    assert 'secret' not in contents and 'password' not in contents
    assert 'PRESENT_UNVERIFIED' in contents
    t.close()


def test_nonblocking_observe_preserves_original_second_even_after_later_flush(tmp_path):
    t, c, w = trace(tmp_path)
    t.observe(component='runtime', action='product.tick', status='active', cycle=9)
    c.advance(15)
    # No durable I/O happens in the UI enqueue path.
    assert len(t._journal.snapshot()) == 1
    diag, agents = t.export_reports(tmp_path, selected_strategy='observe-only-v1')
    report = json.loads(diag.read_text())
    assert report['activities'][0]['second_utc'] == '2026-10-16T10:00:00Z'
    assert report['activities'][0]['cycle'] == 9
    assert report['coverage']['event_time_basis'].startswith('observed_utc_second')
    assert report['coverage']['lost_activity_count'] == 0
    t.close()


def test_observation_buffer_overflow_is_reported_as_partial_not_as_healthy(tmp_path):
    t, c, w = trace(tmp_path)
    for i in range(1100):
        t.observe(component='runtime', action='product.tick', status='active', cycle=i)
    diag, agents = t.export_reports(tmp_path, selected_strategy='observe-only-v1')
    report = json.loads(diag.read_text())
    agent_report = json.loads(agents.read_text())
    assert report['coverage']['recorder_status'] == 'PARTIAL_DROPPED_EVENTS'
    assert report['coverage']['lost_activity_count'] == 76
    assert len(report['activities']) == 1024
    assert agent_report['lost_activity_count'] == 76
    t.close()


def test_keyboard_observe_never_waits_for_recorder_filesystem_lock(tmp_path):
    import threading
    t, c, w = trace(tmp_path)
    completed = threading.Event()
    # Simulates a background journal fsync holding the recorder lock.
    with t._guard:
        worker = threading.Thread(target=lambda: (
            t.observe(component='runtime', action='product.tick', status='active', cycle=12),
            completed.set(),
        ))
        worker.start()
        assert completed.wait(0.5), 'keyboard event waited on diagnostic disk lock'
    worker.join(1.0)
    diag, _ = t.export_reports(tmp_path, selected_strategy='observe-only-v1')
    assert json.loads(diag.read_text())['activities'][0]['cycle'] == 12
    t.close()


def test_stalled_sampler_cannot_hold_operator_shutdown_or_falsely_close_journal(tmp_path):
    trace = OperatorSupportTrace(tmp_path / "workspace", auto_start=False, clock=Clock())

    class StalledSampler:
        def __init__(self):
            self.join_calls = 0

        def join(self, timeout=None):
            assert timeout == 2.0
            self.join_calls += 1

        def is_alive(self):
            return True

    stalled = StalledSampler()
    trace._thread = stalled
    try:
        trace.close()
        assert stalled.join_calls == 1
        assert trace._stop.is_set()
        assert trace._closed is True
        assert trace._fault == "RECORDER_ERROR"
        assert trace.recorder_incomplete is True
        # A still-running journal writer must not receive concurrent close.
        assert trace._journal is not None
    finally:
        # The fixture has no sampler; close its test journal here.
        trace._journal.close({"observation_phase": trace._phase})


def test_finished_sampler_closes_normally_after_bounded_join(tmp_path):
    trace = OperatorSupportTrace(tmp_path / "workspace", auto_start=False, clock=Clock())

    class FinishedSampler:
        def join(self, timeout=None):
            assert timeout == 2.0

        def is_alive(self):
            return False

    trace._thread = FinishedSampler()
    trace.close()
    assert trace._closed is True
    assert trace._fault is None


def test_export_workers_do_not_hold_windows_process_open(tmp_path, monkeypatch):
    import autosport.gui_support_export as export_module

    class NonrunningThread:
        def __init__(self, **kwargs):
            self.daemon = kwargs["daemon"]

        def start(self):
            pass

    monkeypatch.setattr(export_module.threading, "Thread", NonrunningThread)
    trace = OperatorSupportTrace(tmp_path / "workspace", auto_start=False, clock=Clock())
    try:
        exporter = export_module.OneShotSupportExportWorker()
        assert exporter.start(trace, tmp_path, strategy="observe-only-v1", research_plan_loaded=False)
        assert exporter._thread.daemon is True
        scientist = export_module.OneShotLearningStatusWorker()
        assert scientist.start(tmp_path)
        assert scientist._thread.daemon is True
    finally:
        trace.close()


def test_tail_gap_remains_visible_without_a_second_heartbeat(tmp_path):
    t, c, w = trace(tmp_path)
    try:
        t.heartbeat()
        c.advance(9)
        diagnostics, _ = t.export_reports(tmp_path, selected_strategy='observe-only-v1')
        coverage = json.loads(diagnostics.read_text())['coverage']
        # Historical pairwise gap is zero, but seconds 1-8 lack a subsequent
        # verified heartbeat. Second 9 is the still-open export second.
        assert coverage['missing_heartbeat_seconds_between_observations'] == 0
        assert coverage['last_verified_heartbeat_age_seconds'] == 9
        assert coverage['tail_seconds_without_verified_heartbeat'] == 8
        assert coverage['complete_activity_capture_proven'] is False
    finally:
        t.close()


def test_no_heartbeat_does_not_claim_a_zero_length_tail(tmp_path):
    t, c, w = trace(tmp_path)
    try:
        diagnostics, _ = t.export_reports(tmp_path, selected_strategy='observe-only-v1')
        coverage = json.loads(diagnostics.read_text())['coverage']
        assert coverage['first_heartbeat_utc'] is None
        assert coverage['last_verified_heartbeat_age_seconds'] is None
        assert coverage['tail_seconds_without_verified_heartbeat'] is None
    finally:
        t.close()


def test_future_heartbeat_is_not_accepted_in_a_historical_report(tmp_path):
    t, c, w = trace(tmp_path)
    try:
        t.heartbeat()
        c.advance(-1)
        with pytest.raises(SupportExportError, match='causal report cutoff'):
            t.export_reports(tmp_path, selected_strategy='observe-only-v1')
    finally:
        t.close()


def test_journal_activity_after_rollback_cannot_be_reported_as_past(tmp_path):
    t, c, _ = trace(tmp_path)
    try:
        c.advance(5)
        t.record(component='runtime', action='product.tick', status='active')
        c.advance(-4)
        with pytest.raises(SupportExportError, match='causal report cutoff'):
            t.export_reports(tmp_path, selected_strategy='observe-only-v1')
    finally:
        t.close()


def test_delayed_observation_with_future_original_second_is_rejected(tmp_path):
    t, c, _ = trace(tmp_path)
    try:
        c.advance(5)
        t.observe(component='agent', action='learning.observed', status='active')
        c.advance(-4)
        # The durable write uses rolled-back time; original observation is future.
        with pytest.raises(SupportExportError, match='causal report cutoff'):
            t.export_reports(tmp_path, selected_strategy='observe-only-v1')
    finally:
        t.close()
