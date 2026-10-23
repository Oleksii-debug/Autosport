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



def test_semantic_budget_exhaustion_is_classified_without_fabricated_uia_pass() -> None:
    """The real Windows deadline can expire before desktop fallback starts."""
    source = SCRIPT.read_text(encoding="utf-8")
    native = source.split("function Find-BoundedUiaSemanticViaChildren {", 1)[1].split(
        "function Find-UiaRootWithElementForProcessFamily {", 1
    )[0]
    desktop = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert "$script:semanticFailureClass = 'NOT_OBSERVED'" in source
    assert "$script:semanticFailureClass = 'NATIVE_WORKER_TIMEOUT'" in native
    assert "$script:semanticFailureClass = 'DESKTOP_BUDGET_EXHAUSTED'" in desktop
    assert "if ($remainingSeconds -le 0)" in desktop
    assert "$script:semanticFailureClass = 'DESKTOP_WORKER_TIMEOUT'" in desktop
    assert "if ($null -eq $semanticReady -or $null -eq $uiaRoot)" in source
    assert "$script:semanticFailureClass = 'SEMANTIC_NOT_OBSERVED'" in source
    # Deadline exhaustion is not UIA evidence and cannot enter PASS state.
    assert "$report.status = 'PASS'" not in desktop
    assert "if ($report.status -ne 'PASS')" in source
    assert "exit 1" in source


def test_semantic_failure_classes_are_fixed_and_redact_provider_exceptions() -> None:
    """Failure receipts must distinguish native/desktop exhaustion safely."""
    source = SCRIPT.read_text(encoding="utf-8")
    failure = source.split("} catch {\n    if ($script:lastDesktopWorkerStage -ne 'NOT_RUN') {", 1)[1].split(
        "} finally {", 1
    )[0]
    for category in (
        "desktop semantic worker timeout",
        "native semantic worker timeout",
        "desktop semantic budget exhausted",
        "semantic readiness not observed",
        "unexpected automation error",
    ):
        assert f"external UIA audit failed: {category}" in failure
    assert "$script:semanticFailureClass -eq 'NATIVE_WORKER_TIMEOUT'" in failure
    assert "$script:semanticFailureClass -eq 'DESKTOP_BUDGET_EXHAUSTED'" in failure
    assert "$script:semanticFailureClass -eq 'SEMANTIC_NOT_OBSERVED'" in failure
    assert "$_.Exception" not in failure
    assert "$report.status = 'PASS'" not in failure
    assert "$report.failures +=" in failure


def test_render_point_probe_is_bounded_real_uia_not_native_hwnd_acceptance() -> None:
    """The stalled desktop FocusedElement path has a genuine, bounded alternative."""
    source = SCRIPT.read_text(encoding="utf-8")
    desktop = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    worker = desktop.split("$desktopJob = Start-ThreadJob -ScriptBlock {", 1)[1].split(
        "} -ArgumentList @(", 1
    )[0]
    assert worker.index("SEMANTIC_DESKTOP_POINT_PROBE") < worker.index(
        "[System.Windows.Automation.AutomationElement]::FromPoint("
    ) < worker.index("SEMANTIC_DESKTOP_POINT_READY")
    assert worker.index("::FromPoint(") < worker.index("::FocusedElement")
    assert "$pointPid = [int]$pointElement.Current.ProcessId" in worker
    assert "if ($ProcessIds -contains $pointPid)" in worker
    assert "$pointRoot.Current.AutomationId -ceq $AutomationId" in worker
    assert "$ProcessIds -contains [int]$pointParent.Current.ProcessId" in worker
    assert "$pointHops -lt 64 -and [DateTime]::UtcNow -lt $Deadline" in worker
    assert "$pointSemantic.Current.AutomationId -cne $AutomationId" in worker
    assert "$ProcessIds -contains [int]$pointSemantic.Current.ProcessId" in worker
    assert "New-BoundedUiaPair -Window $pointRoot -Element $pointSemantic" in worker
    assert "$report.status = 'PASS'" not in worker
    assert "$workerCompleted.Wait($budgetSeconds * 1000)" in desktop
    assert "$script:semanticFailureClass = 'DESKTOP_WORKER_TIMEOUT'" in desktop


def test_render_point_is_win32_only_and_process_family_scoped() -> None:
    """No screen location or native HWND may become a semantic PASS witness."""
    source = SCRIPT.read_text(encoding="utf-8")
    native = source.split("public static class AutosportNativeWindows", 1)[1].split(
        "function Get-ProcessFamilyIds", 1
    )[0]
    desktop = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert "VisibleScreenCenterForHandle(IntPtr window)" in native
    assert "window == IntPtr.Zero || !IsWindowVisible(window)" in native
    assert "GetWindowRect(window, out rectangle)" in native
    assert "rectangle.Right <= rectangle.Left || rectangle.Bottom <= rectangle.Top" in native
    assert "if ($classBucket -eq 'RENDER_WIDGET_VISIBLE'" in desktop
    assert "[AutosportNativeWindows]::VisibleScreenCenterForHandle($candidateHandle)" in desktop
    assert "if ($pointCoordinates.Count -eq 2)" in desktop
    assert "$pointX = [int]$pointCoordinates[0]" in desktop
    assert "$pointY = [int]$pointCoordinates[1]" in desktop
    assert "$processIdsCsv, $AutomationId, $RootProcessId, $pointX, $pointY, $Deadline" in desktop
    assert "$report.status = 'PASS'" not in desktop


def test_render_point_timeout_stages_are_fixed_and_not_provider_text() -> None:
    """The new bounded worker cannot publish arbitrary COM data as evidence."""
    source = SCRIPT.read_text(encoding="utf-8")
    desktop = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    stage_names = (
        "SEMANTIC_DESKTOP_POINT_PROBE",
        "SEMANTIC_DESKTOP_POINT_READY",
        "SEMANTIC_DESKTOP_POINT_ID_PROBE",
        "SEMANTIC_DESKTOP_POINT_PARENT_PROBE",
        "SEMANTIC_DESKTOP_POINT_CHILD_PROBE",
    )
    allowed = desktop.split("$allowedDesktopWorkerStages = @(", 1)[1].split(")", 1)[0]
    global_progress = source.split("function Write-ExternalUiaProgress {", 1)[1].split(
        "Write-ExternalUiaProgress -Stage 'START'", 1
    )[0]
    progress_tokens = global_progress.split("[ValidateSet(", 1)[1].split(")]", 1)[0]
    progress_stages = tuple(token.strip().strip("'") for token in progress_tokens.split(","))
    for stage in stage_names:
        assert f"Write-ExternalUiaProgress -Stage '{stage}'" in desktop
        assert f"'{stage}'" in allowed
        # These are fixed audit-owned diagnostics and must be accepted by the
        # public watchdog without treating a stage as semantic UIA PASS.
        assert stage in progress_stages
    assert "PASS" not in progress_stages
    assert len(progress_stages) == len(set(progress_stages))
    assert "$allowedDesktopWorkerStages -ccontains $lastWorkerStage" in desktop
    assert "'UNKNOWN_FIXED_STAGE'" in desktop
    assert "if (-not $workerTerminal)" in desktop
    assert "Stop-Job -Job $desktopJob" not in desktop
    assert "$report.status = 'PASS'" not in desktop
