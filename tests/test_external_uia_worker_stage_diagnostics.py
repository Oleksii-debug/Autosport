"""Source contracts for bounded diagnostic stages of the REAL packaged UIA audit.

These checks cannot substitute for external executable/keyboard/UIA evidence.
"""
from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "external_uia_audit.ps1"


def _blocks() -> tuple[str, str]:
    source = SCRIPT.read_text(encoding="utf-8")
    worker_start = source.index("function Find-UiaSemanticViaChildren {")
    bounded_start = source.index("function Find-BoundedUiaSemanticViaChildren {")
    worker_end = source.index("function Find-UiaRootForProcessFamily {", worker_start)
    bounded_end = source.index("function Find-UiaRootWithElementForProcessFamily {", bounded_start)
    return source[worker_start:worker_end], source[bounded_start:bounded_end]


def test_actual_uia_calls_have_fixed_pre_call_diagnostic_stages() -> None:
    worker, _ = _blocks()
    for token, call in (
        ("FROM_HANDLE_ENTER", "AutomationElement]::FromHandle("),
        ("NATIVE_PID_READ_ENTER", "$Root.Current.ProcessId"),
        ("NODE_ID_READ_ENTER", "$node.Current.AutomationId"),
        ("FIRST_CHILD_ENTER", "$walker.GetFirstChild($node)"),
        ("NEXT_SIBLING_ENTER", "$walker.GetNextSibling($child)"),
    ):
        assert worker.index(token) < worker.index(call)
    assert "$CompletionSignal.Set()" in worker
    stages = re.findall(r"\$ProgressQueue\.Enqueue\('([^']+)'\)", worker)
    assert set(stages) == {
        "WORKER_ENTER", "MTA_APARTMENT_CHECK", "FROM_HANDLE_ENTER",
        "FROM_HANDLE_RETURNED", "NATIVE_PID_READ_ENTER", "NATIVE_PID_VALIDATED",
        "NODE_ID_READ_ENTER", "NODE_ID_MATCH", "FIRST_CHILD_ENTER",
        "FIRST_CHILD_RETURNED", "NEXT_SIBLING_ENTER", "NEXT_SIBLING_RETURNED",
    }


def test_timeout_reports_last_stage_without_reentering_poisoned_provider() -> None:
    _, bounded = _blocks()
    assert "ConcurrentQueue[string]" in bounded
    assert "$workerProgress.TryDequeue([ref]$workerStage)" in bounded
    assert "last_worker_stage=$lastWorkerStage" in bounded
    assert "$workerCompleted.Wait($budgetSeconds * 1000)" in bounded
    assert "if (-not $workerTerminal)" in bounded
    assert "throw \"External UIA semantic worker timed out" in bounded
    assert not re.search(r"(?m)^\s*(Wait-Job|Stop-Job|Receive-Job|Remove-Job)\b", bounded)


def test_stages_do_not_replace_real_semantic_or_process_witness() -> None:
    worker, bounded = _blocks()
    assert "$node.Current.AutomationId -ceq $AutomationId" in worker
    assert "$Root.Current.ProcessId -ne $ExpectedProcessId" in worker
    assert "SEMANTIC_WORKER_TIMEOUT_PROBE" in bounded
    assert "throw 'External UIA semantic worker returned an invalid element'" in bounded
    assert "if (-not $hasResult) { return $null }" in bounded
    assert "return $result" in bounded


def test_desktop_worker_timeout_stage_is_allowlisted_and_persisted_fail_closed() -> None:
    """A real Windows provider stall must retain a fixed stage, not arbitrary COM text."""
    source = SCRIPT.read_text(encoding="utf-8")
    helper = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    timeout = helper.split("if (-not $workerTerminal) {", 1)[1].split(
        "Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_WORKER_RESULT_PROBE'", 1
    )[0]
    assert "$script:lastDesktopWorkerStage = 'NOT_RUN'" in source
    assert "$allowedDesktopWorkerStages = @(" in timeout
    assert "'NOT_ENTERED'" in timeout
    assert "'SEMANTIC_DESKTOP_FOCUS_PROBE'" in timeout
    assert "'SEMANTIC_DESKTOP_ENUM_PROBE'" in timeout
    assert "'UNKNOWN_FIXED_STAGE'" in timeout
    assert "$allowedDesktopWorkerStages -ccontains $lastWorkerStage" in timeout
    assert "$script:semanticWorkerTimedOut = $true" in timeout
    assert 'throw "External desktop UIA worker timed out' in timeout
    assert "Stop-Job -Job" not in timeout
    assert "Receive-Job" not in timeout
    assert "semantic_desktop_worker_timeout = $false" in source
    assert "semantic_desktop_worker_last_stage = 'NOT_RUN'" in source
    assert "$report.semantic_desktop_worker_timeout = ($script:lastDesktopWorkerStage -ne 'NOT_RUN')" in source
    assert "$report.semantic_desktop_worker_last_stage = $script:lastDesktopWorkerStage" in source
    assert "'external UIA audit failed: desktop semantic worker timeout'" in source
    assert "if ($report.status -ne 'PASS')" in source
    assert "exit 1" in source
    # The output report must not derive its reason from provider exception text.
    serialized = source.split("$report.semantic_desktop_worker_last_stage =", 1)[1]
    assert "$_.Exception.Message" not in serialized


def test_failure_cleanup_never_reenters_uia_provider() -> None:
    """A blocked Windows provider must not suppress the fail-closed JSON receipt."""
    source = SCRIPT.read_text(encoding="utf-8")
    final_cleanup = source.split(
        "} catch {\n    if ($script:lastDesktopWorkerStage -ne 'NOT_RUN') {", 1
    )[1].split("} finally {", 1)[1]
    # The initial main-window classification occurs BEFORE the semantic probes.
    # Cleanup may only retain that bounded class; it must never query COM again.
    assert "if ([string]::Equals($initialNativeName, 'Автоспорт'" in source
    assert "observed_window_class = 'PRODUCT_MAIN_WINDOW'" in source
    assert "Never reenter the UIA COM provider during failure cleanup" in final_cleanup
    assert "$uiaRoot.Current.Name" not in final_cleanup.replace(
        "# $uiaRoot.Current.Name", "# redacted cleanup comment"
    )
    assert "AutomationElement]::" not in final_cleanup
    assert "Find-UiaRootWithElementForProcessFamily" not in final_cleanup
    assert "$report | ConvertTo-Json -Depth 8 | Set-Content" in final_cleanup
    assert "if ($report.status -ne 'PASS')" in source
    assert "exit 1" in source


def test_cleanup_preserves_existing_fixed_window_classification() -> None:
    """Do not overwrite a real pre-semantic native window classification."""
    source = SCRIPT.read_text(encoding="utf-8")
    initial = source.split(
        "Write-ExternalUiaProgress -Stage 'NATIVE_ROOT_NAME_PROBE'", 1
    )[1].split("    $semanticReady = $null", 1)[0]
    assert "observed_window_class = 'NATIVE_EMERGENCY_STOP'" in initial
    assert "observed_window_class = 'NATIVE_STARTUP_ERROR'" in initial
    assert "observed_window_class = 'PRODUCT_MAIN_WINDOW'" in initial
    cleanup = source.split(
        "} catch {\n    if ($script:lastDesktopWorkerStage -ne 'NOT_RUN') {", 1
    )[1].split("} finally {", 1)[1]
    assert "$report.observed_window_class =" not in cleanup
    assert "$report.semantic_desktop_worker_last_stage" in cleanup
    assert "$report.native_semantic_candidate_buckets" in cleanup
