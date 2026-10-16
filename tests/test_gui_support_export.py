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


def test_support_export_worker_does_not_leak_raw_exception(tmp_path):
    workspace = tmp_path / 'w'
    workspace.mkdir()
    t = OperatorSupportTrace(workspace, auto_start=False, clock=lambda: datetime(2026,10,16,tzinfo=timezone.utc))
    t._fault='RECORDER_ERROR'
    worker=OneShotSupportExportWorker()
    assert worker.start(t, tmp_path, strategy='observe-only-v1', research_plan_loaded=False)
    worker._thread.join(timeout=10)
    result=worker.poll()
    assert result and result.completed is False
    assert result.files is None
    assert result.error_code == 'SUPPORT_EXPORT_FAILED'
    t.close()
