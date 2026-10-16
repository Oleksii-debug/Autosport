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

def test_recorder_fault_cannot_export_false_complete(tmp_path):
    t,c,w=trace(tmp_path)
    t._fault='RECORDER_ERROR'
    with pytest.raises(SupportExportError): t.export_reports(tmp_path,selected_strategy='observe-only-v1')
    t.close()

def test_existing_memory_not_exfiltrated(tmp_path):
    t,c,w=trace(tmp_path)
    (w/'decisions.jsonl').write_text('secret token password',encoding='utf-8')
    _,p=t.export_reports(tmp_path,selected_strategy='observe-only-v1')
    contents=p.read_text(encoding='utf-8')
    assert 'secret' not in contents and 'password' not in contents
    assert 'PRESENT_UNVERIFIED' in contents
    t.close()
