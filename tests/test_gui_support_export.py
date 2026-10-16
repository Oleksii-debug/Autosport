from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from autosport.gui_support_export import OneShotSupportExportWorker
from autosport.operator_support import OperatorSupportTrace


def test_support_export_worker_writes_two_distinct_user_reports(tmp_path):
    workspace = tmp_path / 'w'
    workspace.mkdir()
    t = OperatorSupportTrace(workspace, auto_start=False, clock=lambda: datetime(2026,10,16,tzinfo=timezone.utc))
    t.heartbeat()
    worker = OneShotSupportExportWorker()
    assert worker.start(t, tmp_path, strategy='observe-only-v1', research_plan_loaded=False)
    assert worker.start(t, tmp_path, strategy='observe-only-v1', research_plan_loaded=False) is False
    assert worker._thread is not None
    worker._thread.join(timeout=10)
    result=worker.poll()
    assert result and result.completed and result.files
    assert result.files[0] != result.files[1]
    assert (tmp_path / result.files[0]).is_file()
    assert json.loads((tmp_path / result.files[1]).read_text())['current_strategy_id'] == 'observe-only-v1'
    assert worker.busy is False
    t.close()


def test_support_export_worker_reports_bad_inventory_without_raw_secret(tmp_path):
    workspace = tmp_path / 'w'
    workspace.mkdir()
    t = OperatorSupportTrace(workspace, auto_start=False, clock=lambda: datetime(2026,10,16,tzinfo=timezone.utc))
    # Refuse private symlink source content without suppressing forensic diagnosis.
    (workspace/'decisions.jsonl').symlink_to(tmp_path/'outside-secret.txt')
    try:
        worker = OneShotSupportExportWorker()
        assert worker.start(t, tmp_path, strategy='observe-only-v1', research_plan_loaded=False)
        worker._thread.join(timeout=10)
        result = worker.poll()
        assert result and result.completed is True
        assert result.files == ('Autosport-diagnostics.json', 'Autosport-agents.json')
        diagnostics = json.loads((tmp_path / result.files[0]).read_text(encoding='utf-8'))
        agents_text = (tmp_path / result.files[1]).read_text(encoding='utf-8')
        assert diagnostics['coverage']['artifact_inventory_verification_failed'] is True
        assert diagnostics['coverage']['scientific_evidence_verification_failed'] is False
        assert json.loads(agents_text)['memory_and_learning_artifacts'] == []
        assert 'outside-secret.txt' not in agents_text
    finally:
        t.close()


def test_learning_status_reads_verified_science_without_raw_records(monkeypatch, tmp_path):
    from autosport.gui_support_export import OneShotLearningStatusWorker
    import autosport.learning_evidence_projection as projection
    def result(_workspace):
        return {
            'science': {'state': 'CANONICAL_REGISTRY_VERIFIED', 'records_total': 4, 'types': [
                {'type': 'Experiment', 'count': 3}, {'type': 'PromotionDecision', 'count': 1},
            ], 'experiment_outcomes': {
                'POSITIVE': 1, 'NEGATIVE': 1, 'INCONCLUSIVE': 1,
            }, 'promotion_decisions': {'RETAIN': 1}},
            'research_runs': {'state': 'VERIFIED_SUPERVISOR_SNAPSHOTS', 'count': 2},
            'agent_loop': {'state': 'VERIFIED_RESTART_SNAPSHOT', 'sequence': 18},
        }
    monkeypatch.setattr(projection, 'verified_learning_projection', result)
    worker=OneShotLearningStatusWorker()
    assert worker.start(tmp_path)
    worker._thread.join(2)
    assert worker.busy is True
    response=worker.poll()
    assert response is not None and response.error_code is None
    text='\n'.join(response.lines)
    assert 'Експериментів: 3' in text
    assert 'Кроків циклу агентів: 18' in text
    assert 'Результати експериментів:' in text
    assert 'позитивні 1' in text
    assert 'негативні 1' in text
    assert 'відкати 0' in text
    assert 'реєстр перевірено' in text
    assert 'тут не доведені' in text
    assert 'secret-token' not in text
    assert worker.busy is False


def test_science_outcome_counts_must_be_consistent_before_announcement(monkeypatch, tmp_path):
    from autosport.gui_support_export import OneShotLearningStatusWorker
    import autosport.learning_evidence_projection as projection

    for invalid in (
        {'POSITIVE': 3},  # total differs from the canonical Experiment count
        {'POSITIVE': True, 'NEGATIVE': 1},  # bool is not a count
        {'PRIVATE_SOURCE_NAME': 2},  # no raw identifiers in spoken status
        {'POSITIVE': -1, 'NEGATIVE': 3},
        None,
    ):
        monkeypatch.setattr(projection, 'verified_learning_projection', lambda _root: {
            'science': {
                'state': 'CANONICAL_REGISTRY_VERIFIED',
                'records_total': 3,
                'types': [
                    {'type': 'Experiment', 'count': 2},
                    {'type': 'PromotionDecision', 'count': 1},
                ],
                'experiment_outcomes': invalid,
                'promotion_decisions': {'RETAIN': 1},
            },
            'research_runs': {'state': 'NOT_PRESENT'},
            'agent_loop': {'state': 'NOT_PRESENT'},
        })
        worker = OneShotLearningStatusWorker()
        assert worker.start(tmp_path)
        worker._thread.join(timeout=2)
        result = worker.poll()
        assert result is not None
        assert result.lines is None
        assert result.error_code == 'LEARNING_STATUS_UNAVAILABLE'
        assert 'PRIVATE_SOURCE_NAME' not in str(result)


def test_corrupt_scientific_state_is_explicit_unavailable(monkeypatch, tmp_path):
    from autosport.gui_support_export import OneShotLearningStatusWorker
    import autosport.learning_evidence_projection as projection
    def fail(_workspace):
        raise ValueError('API_KEY=secret-operator-trace')
    monkeypatch.setattr(projection, 'verified_learning_projection', fail)
    worker=OneShotLearningStatusWorker()
    assert worker.start(tmp_path)
    worker._thread.join(2)
    response=worker.poll()
    assert response.lines is None
    assert response.error_code == 'LEARNING_STATUS_UNAVAILABLE'
    assert 'secret' not in str(response)


def test_partial_recorder_is_exportable_and_ui_knows_it_is_incomplete(tmp_path):
    workspace=tmp_path/'w'; workspace.mkdir()
    trace=OperatorSupportTrace(workspace,auto_start=False,clock=lambda: datetime(2026,10,16,tzinfo=timezone.utc))
    trace.heartbeat()
    trace._fault='RECORDER_ERROR'
    worker=OneShotSupportExportWorker()
    assert worker.start(trace,tmp_path,strategy='observe-only-v1',research_plan_loaded=False)
    worker._thread.join(2)
    msg=worker.poll()
    assert msg and msg.completed is True and msg.recording_fault is True
    assert json.loads((tmp_path/'Autosport-diagnostics.json').read_text())['coverage']['recorder_status']=='RECORDER_ERROR'
    trace.close()
