"""Plan 6 7B: diagnostics + agent evidence must export during incomplete setup.

This exercises the real WebView controller action, bounded worker, forensic
journal and paired-report verifier without building a Windows package.
A missing research plan MUST block the runtime, not incident diagnostics.
Expected RED at #2279 5d5c52dc: support.export rejects before worker start.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from autosport.gui_support_export import OneShotSupportExportWorker
from autosport.operator_support import OperatorSupportTrace, verify_report_pair
from autosport.research_strategy import RESEARCH_STRATEGY_ID
from autosport.windows_webview_shell import AutosportWebController


def _controller_with_verified_trace(tmp_path: Path, *, strategy: str):
    root = tmp_path / "workspace"
    root.mkdir()
    controller = AutosportWebController.__new__(AutosportWebController)
    controller.workspace = root
    controller.strategy_id = strategy
    controller.research_plan = None
    controller.status = ""
    controller.last_error = ""
    controller.log = []
    controller.support_export_worker = OneShotSupportExportWorker()
    trace = OperatorSupportTrace(
        root, auto_start=False,
        clock=lambda: datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc),
    )
    trace.heartbeat()
    controller._support_trace = trace
    controller._enable_support_trace = lambda: None
    return controller, trace


def _assert_two_real_reports(controller, tmp_path):
    result = controller._action_support_export({"directory": ""})
    assert result["status"] == "completed", result
    assert result.get("focus_id") == "support-export-status"
    worker = controller.support_export_worker
    assert worker._thread is not None
    worker._thread.join(timeout=10)
    assert not worker._thread.is_alive(), "report export worker did not finish"
    outcome = worker.poll()
    assert outcome is not None and outcome.completed is True, outcome
    assert outcome.files == ("Autosport-diagnostics.json", "Autosport-agents.json")
    diagnostic_path = tmp_path / outcome.files[0]
    agents_path = tmp_path / outcome.files[1]
    verify_report_pair(diagnostic_path, agents_path)
    return (
        json.loads(diagnostic_path.read_text(encoding="utf-8")),
        json.loads(agents_path.read_text(encoding="utf-8")),
    )


def test_baseline_can_export_without_research_plan(tmp_path):
    controller, trace = _controller_with_verified_trace(
        tmp_path, strategy="baseline-v1"
    )
    try:
        diagnostics, agents = _assert_two_real_reports(controller, tmp_path)
        assert diagnostics["coverage"]["record_count"] >= 1
        assert agents["research_plan_loaded"] is False
        assert agents["current_strategy_id"] == "baseline-v1"
        assert agents["real_money_execution"] is False
    finally:
        trace.close()


def test_incomplete_research_plan_still_allows_truthful_support_reports(tmp_path):
    controller, trace = _controller_with_verified_trace(
        tmp_path, strategy=RESEARCH_STRATEGY_ID
    )
    try:
        # PAPER/economic execution must stay blocked until a valid plan is bound.
        with pytest.raises(ValueError, match="requires --research-plan"):
            controller._product_runtime_target_workspace()

        # Incident diagnostics must NOT inherit that execution gate.
        diagnostics, agents = _assert_two_real_reports(controller, tmp_path)
        assert diagnostics["coverage"]["record_count"] >= 1
        assert agents["current_strategy_id"] == RESEARCH_STRATEGY_ID
        assert agents["research_plan_loaded"] is False
        assert agents["agent_workspace_scope"] == "ROOT"
        assert agents["verified_learning_evidence"]["proven_live_model_learning"] is False
        assert agents["verified_learning_evidence"]["has_live_execution_authority"] is False
    finally:
        trace.close()


def test_invalid_export_directory_is_rejected_without_creating_reports(tmp_path):
    controller, trace = _controller_with_verified_trace(
        tmp_path, strategy=RESEARCH_STRATEGY_ID
    )
    try:
        rejected = controller._action_support_export(
            {"directory": " has surrounding spaces "}
        )
        assert rejected["status"] == "rejected"
        assert controller.support_export_worker._thread is None
        assert not (tmp_path / "Autosport-diagnostics.json").exists()
        assert not (tmp_path / "Autosport-agents.json").exists()
    finally:
        trace.close()
