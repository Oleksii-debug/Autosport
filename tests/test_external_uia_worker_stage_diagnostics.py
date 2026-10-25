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
    timeout = helper.split("if ($unfinished.Count -gt 0) {", 1)[1].split(
        "foreach ($state in $workerStates) { $state.Completion.Dispose() }", 1
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
    """A stalled FromPoint must not prevent an independent genuine focused UIA probe."""
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
    assert worker.index("::FocusedElement") < worker.index("::FromPoint(")
    assert "$pointPid = [int]$pointElement.Current.ProcessId" in worker
    assert "if ($ProcessIds -contains $pointPid)" in worker
    assert "$pointRoot.Current.AutomationId -ceq $AutomationId" in worker
    assert "$ProcessIds -contains [int]$pointParent.Current.ProcessId" in worker
    assert "$pointHops -lt 64 -and [DateTime]::UtcNow -lt $Deadline" in worker
    assert "$pointSemantic.Current.AutomationId -cne $AutomationId" in worker
    assert "$ProcessIds -contains [int]$pointSemantic.Current.ProcessId" in worker
    assert "New-BoundedUiaPair -Window $pointRoot -Element $pointSemantic" in worker
    assert "$report.status = 'PASS'" not in worker
    assert "$state.Completion.Wait(0)" in desktop
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
    assert "$workerMode, $processIdsCsv, $AutomationId, $RootProcessId, $pointX, $pointY," in desktop
    assert "$nativeRendererHandle, $rendererProcessId, $Deadline," in desktop
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
    assert "if ($unfinished.Count -gt 0)" in desktop
    assert "Stop-Job -Job $desktopJob" not in desktop
    assert "$report.status = 'PASS'" not in desktop


def test_foreground_activation_only_targets_visible_exact_launch_process() -> None:
    """Win32 activation cannot target a foreign HWND or count as UIA proof."""
    source = SCRIPT.read_text(encoding="utf-8")
    native = source.split("public static class AutosportNativeWindows", 1)[1].split(
        "function Get-ProcessFamilyIds", 1
    )[0]
    helper = native.split("public static bool TryClaimForegroundForProcess(", 1)[1].split(
        "public static", 1
    )[0]
    assert "window == IntPtr.Zero || expectedProcessId <= 0 || !IsWindowVisible(window)" in helper
    assert "GetWindowThreadProcessId(window, out ownerPid) == 0" in helper
    assert "ownerPid != (uint)expectedProcessId" in helper
    assert "GetForegroundWindow() == window" in helper
    assert "if (!SetForegroundWindow(window)) return false" in helper
    assert "return GetForegroundWindow() == window" in helper
    assert "AutomationElement" not in helper


def test_foreground_attempt_precedes_semantic_probe_without_fabricating_pass() -> None:
    """An OS focus claim is neither AutomationId=330 nor release acceptance."""
    source = SCRIPT.read_text(encoding="utf-8")
    activation = source.split(
        "# WinForms UIA calibration #40017780628 succeeded", 1
    )[1].split("    $semanticReady = $null", 1)[0]
    assert "$process.Refresh()" in activation
    assert "[AutosportNativeWindows]::TryClaimForegroundForProcess(" in activation
    assert "$process.MainWindowHandle, [int]$process.Id" in activation
    assert "$report.native_foreground_activation_status = 'CLAIMED'" in activation
    assert "$report.native_foreground_activation_status = 'NOT_CLAIMED'" in activation
    assert "$report.status = 'PASS'" not in activation
    assert "AutomationElement" not in activation
    assert source.index("TryClaimForegroundForProcess(") < source.index(
        "Write-ExternalUiaProgress -Stage 'SEMANTIC_LOCAL_PROBE'"
    )
    assert "if ($null -eq $semanticReady -or $null -eq $uiaRoot)" in source
    assert "if ($report.status -ne 'PASS')" in source
    assert "exit 1" in source


def test_foreground_retry_uses_only_exact_launcher_pid() -> None:
    """Retry a missing MainWindowHandle without accepting child-process HWNDs."""
    source = SCRIPT.read_text(encoding="utf-8")
    activation = source.split(
        "$report.native_foreground_activation_status = 'NOT_CLAIMED'", 1
    )[1].split("$semanticReady = $null", 1)[0]
    assert activation.count("TryClaimForegroundForProcess(") == 2
    assert "if ($report.native_foreground_activation_status -ne 'CLAIMED')" in activation
    assert "[AutosportNativeWindows]::VisibleTopLevelWindowsForProcess(" in activation
    assert "[int]$process.Id" in activation
    assert "[AutosportNativeWindows]::ForProcessFamily(" not in activation
    assert "foreach ($candidateHandle in $exactLauncherWindows)" in activation
    assert "$candidateHandle, [int]$process.Id" in activation
    assert "$lastFamilyIds" not in activation
    assert "status = 'PASS'" not in activation


def test_foreground_retry_remains_os_only_and_uia_gate_is_required() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    activation = source.split(
        "$report.native_foreground_activation_status = 'NOT_CLAIMED'", 1
    )[1].split("$semanticReady = $null", 1)[0]
    assert "AutomationElement" not in activation
    assert "Write-ExternalUiaProgress -Stage 'SEMANTIC_LOCAL_PROBE'" in source
    assert "Find-UiaRootWithElementForProcessFamily" in source
    assert "if ($null -eq $semanticReady -or $null -eq $uiaRoot)" in source
    assert 'throw "Timed out waiting for WebView2 semantic UIA readiness' in source


def test_foreground_os_census_distinguishes_launcher_and_child_top_level_windows() -> None:
    """Never confuse a renderer-owned top-level window with launcher foreground."""
    source = SCRIPT.read_text(encoding="utf-8")
    native = source.split("public static class AutosportNativeWindows", 1)[1].split(
        "function Get-ProcessFamilyIds", 1
    )[0]
    census = native.split("public static int[] TopLevelProcessOwnershipCounts(", 1)[1].split(
        "public static IntPtr[] VisibleTopLevelWindowsForProcess(", 1
    )[0]
    assert "EnumWindows(callback, IntPtr.Zero)" in census
    assert "Array.IndexOf(familyIds, launcherId) < 0" in census
    assert "Array.IndexOf(familyIds, (int)owner) < 0" in census
    assert "owner == (uint)launcherId ? 0 : 2" in census
    assert "IsWindowVisible(window) ? 0 : 1" in census
    assert "if (++matched > 4096) return false" in census
    assert "AutomationElement" not in census
    assert "GetWindowText" not in census
    for token in (
        "LAUNCHER_VISIBLE", "LAUNCHER_HIDDEN",
        "FAMILY_CHILD_VISIBLE", "FAMILY_CHILD_HIDDEN",
        "FAMILY_CHILD_VISIBLE_ONLY", "NO_VISIBLE_FAMILY_TOP_LEVEL",
        "UNAVAILABLE",
    ):
        assert token in source
    assert "native_foreground_top_level_counts = $null" in source
    assert "native_foreground_top_level_class = 'NOT_OBSERVED'" in source
    assert "$report.status = 'PASS'" not in source.split(
        "$report.native_foreground_activation_status = 'NOT_CLAIMED'", 1
    )[1].split("$semanticReady = $null", 1)[0]


def test_foreground_retry_uses_top_level_hwnds_only_and_preserves_real_uia_gate() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    native = source.split("public static class AutosportNativeWindows", 1)[1].split(
        "function Get-ProcessFamilyIds", 1
    )[0]
    helper = native.split("public static IntPtr[] VisibleTopLevelWindowsForProcess(", 1)[1].split(
        "public static IntPtr[] ForProcessFamily(", 1
    )[0]
    assert "EnumWindows(callback, IntPtr.Zero)" in helper
    assert "owner != (uint)launcherId || !IsWindowVisible(window)" in helper
    assert "if (count >= windows.Length) return false" in helper
    assert "GetWindow(" not in helper
    assert "AutomationElement" not in helper
    activation = source.split(
        "$report.native_foreground_activation_status = 'NOT_CLAIMED'", 1
    )[1].split("$semanticReady = $null", 1)[0]
    assert "VisibleTopLevelWindowsForProcess(" in activation
    assert "TryClaimForegroundForProcess(" in activation
    assert "FAMILY_CHILD_VISIBLE_ONLY" in activation
    assert "Find-UiaRootWithElementForProcessFamily" in source
    assert "if ($null -eq $semanticReady -or $null -eq $uiaRoot)" in source
    assert "if ($report.status -ne 'PASS')" in source
    assert "exit 1" in source


def test_focused_probe_precedes_known_blocking_point_without_bypassing_semantic_identity() -> None:
    """The focused fallback requires genuine process and AutomationId evidence."""
    source = SCRIPT.read_text(encoding="utf-8")
    desktop = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    worker = desktop.split("$desktopJob = Start-ThreadJob -ScriptBlock {", 1)[1].split(
        "} -ArgumentList @(", 1
    )[0]
    assert worker.index("SEMANTIC_DESKTOP_FOCUS_PROBE") < worker.index(
        "SEMANTIC_DESKTOP_POINT_PROBE"
    ) < worker.index("SEMANTIC_DESKTOP_ROOT_PROBE")
    focus = worker.split("SEMANTIC_DESKTOP_FOCUS_PROBE", 1)[1].split(
        "SEMANTIC_DESKTOP_POINT_PROBE", 1
    )[0]
    assert "$ProcessIds -contains $focusedPid" in focus
    assert "$ProcessIds -contains [int]$focusSemantic.Current.ProcessId" in focus
    assert "$focusSemantic.Current.AutomationId -cne $AutomationId" in focus
    assert "New-BoundedUiaPair -Window $focusedRoot -Element $focusSemantic" in focus
    assert "status = 'PASS'" not in focus
    assert "SendKeys" not in focus


def test_product_mta_differential_is_process_isolated_bounded_and_not_a_gate() -> None:
    """Real product/UIA provider differential cannot be misreported as PASS."""
    source = SCRIPT.read_text(encoding="utf-8")
    helper = source.split("function Get-IsolatedMtaFocusDiagnostic {", 1)[1].split(
        "function Find-UiaRootWithElementForProcessFamily {", 1
    )[0]
    assert "[System.Diagnostics.ProcessStartInfo]::new()" in helper
    assert "$start.UseShellExecute = $false" in helper
    assert "'-NoProfile', '-NonInteractive', '-Mta'" in helper
    assert "'-EncodedCommand'" in helper
    assert "[Text.Encoding]::Unicode.GetBytes($probe)" in helper
    assert "$child.WaitForExit(6000)" in helper
    assert "$child.Kill($true)" in helper
    assert "$started = $false" in helper
    assert "if ($started)" in helper
    assert "Get-Command pwsh -ErrorAction SilentlyContinue" in helper
    assert "return 'TIMEOUT'" in helper
    assert "return 'UNAVAILABLE'" in helper
    assert "$report.status = 'PASS'" not in helper
    assert "Write-ExternalUiaProgress -Stage 'SEMANTIC_UIA_OBSERVED'" not in helper


def test_product_mta_differential_requires_real_pid_and_exact_id() -> None:
    """Foreign/wrong-ID/failed COM reads are never a positive semantic claim."""
    source = SCRIPT.read_text(encoding="utf-8")
    helper = source.split("function Get-IsolatedMtaFocusDiagnostic {", 1)[1].split(
        "function Find-UiaRootWithElementForProcessFamily {", 1
    )[0]
    assert "AUTOSPORT_UIA_DIAGNOSTIC_PIDS" in helper
    assert "[System.Windows.Automation.AutomationElement]::FocusedElement" in helper
    assert "$focused.Current.ProcessId" in helper
    assert "$allowed -notcontains $actualPid" in helper
    assert "Write-Output 'FOREIGN_FOCUS'" in helper
    assert "$focused.Current.AutomationId -ceq '330'" in helper
    assert "Write-Output 'IN_FAMILY_OTHER_ID'" in helper
    assert "Write-Output 'EXACT_ID_330'" in helper
    assert "Write-Output 'PROVIDER_ERROR'" in helper
    assert "$child.ExitCode -ne 0" in helper
    assert "return 'INVALID_RESULT'" in helper
    assert "$code -cin @('NO_FOCUS', 'FOREIGN_FOCUS', 'EXACT_ID_330', 'IN_FAMILY_OTHER_ID')" in helper
    assert "Write-Output $focused" not in helper
    assert "Write-Output $actualPid" not in helper


def test_product_mta_differential_never_short_circuits_packaged_gates() -> None:
    """Even a real isolated ID is diagnostic; in-process and downstream gates remain."""
    source = SCRIPT.read_text(encoding="utf-8")
    field = "isolated_mta_focus_diagnostic_status"
    assert source.count(field) == 2
    assert field + " = 'NOT_RUN'" in source
    assign = "$report." + field + " = Get-IsolatedMtaFocusDiagnostic -ProcessIds ([int[]]$lastFamilyIds)"
    assert assign in source
    failure_guard = "if ($null -eq $semanticReady -or $null -eq $uiaRoot)"
    assert source.index(assign) > source.index(failure_guard)
    assert source.index(assign) > source.index("$semanticReady = $null")
    assert source.index(assign) < source.index(
        'throw "Timed out waiting for WebView2 semantic UIA readiness'
    )
    assert assign not in source.split("$semanticReady = $null", 1)[0]
    assert "Find-UiaRootWithElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '330'" in source
    assert "Write-ExternalUiaProgress -Stage 'SEMANTIC_UIA_OBSERVED'" in source
    assert "if ($report.status -ne 'PASS')" in source
    assert "exit 1" in source
    assert "keyboard_shortcuts_status = 'NOT_RUN'" in source
    assert "live_region_event_status = 'NOT_RUN'" in source
    assert "emergency_stop_activation_status = 'NOT_RUN'" in source


def test_desktop_fallback_routes_are_independent_and_never_accept_native_only_evidence() -> None:
    """A stalled FocusedElement must not starve process-root or point UIA probes."""
    source = SCRIPT.read_text(encoding="utf-8")
    desktop = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    worker = desktop.split("$desktopJob = Start-ThreadJob -ScriptBlock {", 1)[1].split(
        "} -ArgumentList @(", 1
    )[0]
    assert "foreach ($workerMode in @('ROOT', 'POINT', 'FOCUS', 'RENDERER'))" in desktop
    assert "[string]$Mode, [string]$ProcessIdsCsv" in worker
    assert "if ($Mode -eq 'FOCUS')" in worker
    assert "if ($Mode -eq 'POINT')" in worker
    assert "if ($Mode -eq 'ROOT')" in worker
    assert worker.index("if ($Mode -eq 'FOCUS')") < worker.index(
        "if ($Mode -eq 'POINT')"
    ) < worker.index("if ($Mode -eq 'ROOT')")
    assert "$workerMode, $processIdsCsv, $AutomationId" in desktop
    assert "$workerStates += [pscustomobject]@{" in desktop
    assert "$workerResults.TryDequeue([ref]$result)" in desktop
    assert "$state.Completion.Wait(0)" in desktop
    assert "if ($unfinished.Count -gt 0)" in desktop
    assert "$script:semanticFailureClass = 'DESKTOP_WORKER_TIMEOUT'" in desktop
    assert "Stop-Job -Job $desktopJob" not in desktop
    assert "Remove-Job -Job $desktopJob" not in desktop
    assert "$ProcessIds -contains $focusedPid" in worker
    assert "$ProcessIds -contains $pointPid" in worker
    assert "ProcessIdProperty" in worker
    assert "[string]$focusSemantic.Current.AutomationId -cne $AutomationId" in worker
    assert "[string]$pointSemantic.Current.AutomationId -cne $AutomationId" in worker
    assert "$report.status = 'PASS'" not in desktop

def test_independent_desktop_uia_routes_publish_fixed_failure_stages() -> None:
    """One stalled ROOT route must not conceal POINT/FOCUS evidence or grant PASS."""
    source = SCRIPT.read_text(encoding="utf-8")
    helper = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    timeout = helper.split("if ($unfinished.Count -gt 0) {", 1)[1].split(
        "$script:semanticWorkerTimedOut = $true", 1
    )[0]
    assert "$script:lastDesktopWorkerRouteStages = [ordered]@{" in source
    assert all(f"{mode} = 'NOT_RUN'" in source for mode in ("ROOT", "POINT", "FOCUS", "RENDERER"))
    assert "foreach ($state in $workerStates)" in timeout
    assert "$state.Progress.TryDequeue([ref]$workerStage)" in timeout
    assert "$state.Completion.Wait(0)" in timeout
    assert "$script:lastDesktopWorkerRouteStages[$state.Mode] = 'COMPLETED'" in timeout
    assert "$script:lastDesktopWorkerRouteStages[$state.Mode] = $safeStage" in timeout
    assert "$allowedDesktopWorkerStages -ccontains $routeStage" in timeout
    assert "'UNKNOWN_FIXED_STAGE'" in timeout
    assert "semantic_desktop_worker_route_stages = $null" in source
    assert "$report.semantic_desktop_worker_route_stages = $script:lastDesktopWorkerRouteStages" in source
    assert "Stop-Job" not in timeout
    assert "Receive-Job" not in timeout
    assert "$report.status = 'PASS'" not in timeout


def test_native_host_win32_differential_is_bounded_and_not_semantic_acceptance() -> None:
    """OS responsiveness and GUI focus cannot be substituted for real UIA."""
    source = SCRIPT.read_text(encoding="utf-8")
    native = source.split("public static string[] ProbeVisibleFamilyHost(", 1)[1].split(
        "// WebView2 can defer renderer accessibility", 1
    )[0]
    assert "IsVisibleFamilyHost(window, familyIds)" in native
    assert "GetAncestor(window, GA_ROOT) == window" in source
    assert "GetForegroundWindow() != window" in native
    assert "GetGUIThreadInfo(threadId, ref info)" in native
    assert "WM_NULL" in native and "SMTO_ABORTIFHUNG, 250" in native
    assert "ProcessIdForHandle(window) != owner" in native
    assert "RESPONSIVE" in native and "NO_REPLY" in native
    assert "FOCUS_IN_FAMILY" in native and "FOCUS_FOREIGN" in native
    assert "native_host_message_status = 'NOT_ATTEMPTED'" in source
    assert "native_host_focus_status = 'NOT_ATTEMPTED'" in source
    assert "$report.native_host_message_status = $script:nativeHostMessageStatus" in source
    assert "$report.native_host_focus_status = $script:nativeHostFocusStatus" in source
    assert "return new[] { messageStatus" in native
    assert "$report.status = 'PASS'" not in native


def test_process_isolated_from_handle_is_bounded_and_fail_closed() -> None:
    """Host/renderer provider stalls cannot hang the parent diagnostic."""
    source = SCRIPT.read_text(encoding="utf-8")
    worker = source.split("function Get-IsolatedMtaHandleDiagnostic {", 1)[1].split(
        "function Find-UiaRootWithElementForProcessFamily {", 1
    )[0]
    caller = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert "ProcessIdForHandle($Window)" in worker
    assert "AutomationElement]::FromHandle(" in worker
    assert "$allowed -notcontains [int]$element.Current.ProcessId" in worker
    assert "[string]$element.Current.AutomationId -ceq '330'" in worker
    assert "'-Mta'" in worker and "'-NonInteractive'" in worker
    assert "$child.WaitForExit(2500)" in worker
    assert "$child.Kill($true)" in worker
    assert "'TIMEOUT'" in worker and "'FOREIGN_ELEMENT'" in worker
    assert "'INVALID_RESULT'" in worker and "'PROVIDER_ERROR'" in worker
    assert "Get-IsolatedMtaHandleDiagnostic -Window $nativeHostHandle" in caller
    assert "Get-IsolatedMtaHandleDiagnostic -Window $nativeRendererHandle" in caller
    assert "[DateTime]::UtcNow.AddSeconds(6) -lt $Deadline" in caller
    assert "isolated_host_handle_status = 'NOT_ATTEMPTED'" in source
    assert "isolated_renderer_handle_status = 'NOT_ATTEMPTED'" in source
    assert "$report.isolated_host_handle_status = $script:isolatedHostHandleStatus" in source
    assert "$report.isolated_renderer_handle_status = $script:isolatedRendererHandleStatus" in source
    assert "$report.status = 'PASS'" not in worker
    assert "Receive-Job" not in worker


def test_optional_focused_mta_diagnostic_cannot_preempt_product_semantic_probe() -> None:
    """A diagnostic COM timeout must not poison a potentially healthy product gate."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("$semanticReady = $null")
    real_gate = source.index(
        "Find-UiaRootWithElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '330'",
        start,
    )
    failure = source.index("if ($null -eq $semanticReady -or $null -eq $uiaRoot)", real_gate)
    diagnostic = source.index(
        "$report.isolated_mta_focus_diagnostic_status = Get-IsolatedMtaFocusDiagnostic",
        failure,
    )
    assert start < real_gate < failure < diagnostic
    assert "Get-IsolatedMtaFocusDiagnostic" not in source[start:failure]
    assert source.count(
        "$report.isolated_mta_focus_diagnostic_status = Get-IsolatedMtaFocusDiagnostic"
    ) == 1
    assert "Get-IsolatedMtaFocusDiagnostic" not in source[
        source.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_UIA_OBSERVED'"):
    ]
    assert "if ($report.status -ne 'PASS')" in source
    assert "exit 1" in source


def test_renderer_hwnd_route_requires_real_in_family_semantic_descendant() -> None:
    """An observed native renderer is a candidate, never accessibility PASS."""
    source = SCRIPT.read_text(encoding="utf-8")
    desktop = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    worker = desktop.split("$desktopJob = Start-ThreadJob -ScriptBlock {", 1)[1].split(
        "} -ArgumentList @(", 1
    )[0]
    assert "$nativeRendererHandle -ne [IntPtr]::Zero" in desktop
    assert "$rendererProcessId = [AutosportNativeWindows]::ProcessIdForHandle($nativeRendererHandle)" in desktop
    assert "($ProcessIds -contains $rendererProcessId)" in desktop
    assert "if ($workerMode -eq 'RENDERER' -and -not $rendererCandidateValid) { continue }" in desktop
    assert "if ($Mode -eq 'RENDERER')" in worker
    renderer = worker.split("if ($Mode -eq 'RENDERER')", 1)[1].split(
        "if ($Mode -eq 'FOCUS')", 1
    )[0]
    assert "Find-BoundedUiaSemanticViaChildren" in renderer
    assert "-NativeWindowHandle $RendererHandle -ExpectedProcessId $RendererProcessId" in renderer
    assert "$rendererPair.Element.Current.AutomationId -cne $AutomationId" in renderer
    assert "$ProcessIds -contains [int]$rendererPair.Element.Current.ProcessId" in renderer
    assert "$rendererPair.Root.Current.ProcessId -ne $RendererProcessId" in renderer
    assert "$ResultQueue.Enqueue([object]$rendererPair)" in renderer
    assert "$report.status = 'PASS'" not in renderer
    assert "isolated_renderer_handle_status" not in renderer
    assert "'SEMANTIC_DESKTOP_RENDERER_PROBE'" in desktop
    assert "'SEMANTIC_DESKTOP_RENDERER_READY'" in desktop


def test_renderer_hwnd_route_cannot_bypass_downstream_packaged_acceptance() -> None:
    """A real renderer descendant still needs runtime, keyboard and STOP gates."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "Write-ExternalUiaProgress -Stage 'SEMANTIC_UIA_OBSERVED'" in source
    assert "keyboard_shortcuts_status = 'NOT_RUN'" in source
    assert "live_region_event_status = 'NOT_RUN'" in source
    assert "emergency_stop_activation_status = 'NOT_RUN'" in source
    assert "runtime_witness_status = 'NOT_REQUESTED'" in source
    assert "if ($report.status -ne 'PASS')" in source
    assert "exit 1" in source


def test_duplicate_process_creation_is_bounded_after_real_semantic_witness() -> None:
    """A poisoned primary UIA runspace must not hang on the second Start-Process."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_PROBE'")
    ready = source.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_READY'", start)
    launch = source[start:ready]
    assert source.index("Write-ExternalUiaProgress -Stage 'RUNTIME_WITNESS_OBSERVED'") < start
    assert "$duplicateStartWorker = Start-ThreadJob -ScriptBlock" in launch
    assert "$duplicateProcess = Start-Process -FilePath $exePath -WorkingDirectory $launchWorkingDirectory -PassThru" in launch
    assert launch.index("Start-Process -FilePath") < launch.index("$duplicateStartCompleted.Wait(6000)")
    assert launch.index("$duplicateStartCompleted.Wait(6000)") < launch.index("$duplicateStartResults.TryDequeue")
    assert launch.index("$duplicateStartResults.TryDequeue") < launch.index("$duplicateProcess = [System.Diagnostics.Process]$duplicateStarted")
    assert "Start-Process -FilePath $exePath -WorkingDirectory $launchWorkingDirectory -PassThru" not in source[ready:]


def test_duplicate_process_creation_timeout_and_empty_result_fail_closed() -> None:
    """No timeout or missing process receipt can qualify the duplicate-launch gate."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_PROBE'")
    ready = source.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_READY'", start)
    launch = source[start:ready]
    assert "if (-not $duplicateStartCompleted.Wait(6000)) {" in launch
    assert "throw 'Second packaged launch bounded process creation timed out'" in launch
    assert "if (-not $duplicateStartResults.TryDequeue([ref]$duplicateStarted) -or" in launch
    assert "throw 'Second packaged launch did not return a valid process'" in launch
    assert "$Completion.Set()" in launch
    assert "Stop-Job -Job $duplicateStartWorker" not in launch
    assert "duplicate_launch_status = 'PASS'" not in launch
    # Subsequent UIA dialog, absence-of-second-operator and exit-code gates remain.
    assert "$duplicateTitle = [string]$duplicateRoot.Current.Name" in source[ready:]
    assert "Second packaged launch exposed a second semantic WebView operator surface" in source[ready:]
    assert "$duplicateProcess.ExitCode -ne 2" in source[ready:]

def test_duplicate_threadjob_argument_identity_and_fixed_outcomes() -> None:
    """An empty enumerable queue must not shift the completion event argument."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_PROBE'")
    ready = source.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_READY'", start)
    launch = source[start:ready]
    assert "$duplicateStartArguments = [object[]]::new(5)" in launch
    for slot, value in enumerate((
        "$exePath", "$launchWorkingDirectory", "$duplicateStartResults",
        "$duplicateStartOutcomes", "$duplicateStartCompleted",
    )):
        assert f"$duplicateStartArguments[{slot}] = {value}" in launch
    assert "} -ArgumentList $duplicateStartArguments" in launch
    assert "} -ArgumentList @(" not in launch
    assert "$duplicateStartOutcomes.TryDequeue([ref]$duplicateStartOutcome)" in launch
    assert "$duplicateStartResults.TryDequeue([ref]$duplicateStarted)" in launch
    assert "$duplicateProcess = [System.Diagnostics.Process]$duplicateStarted" in launch
    assert "if ($null -eq $duplicateProcess -or [int]$duplicateProcess.Id -le 0)" in launch
    assert "$report.duplicate_start_diagnostic = 'RETURNED'" in launch
    assert source.index("$report.duplicate_start_diagnostic = 'RETURNED'") < ready


def test_duplicate_threadjob_failures_never_promote_lock_or_leak_details() -> None:
    """Timeout, missing receipt, bad PID and launch exceptions are fail-closed."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_PROBE'")
    ready = source.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_READY'", start)
    launch = source[start:ready]
    assert "duplicate_start_diagnostic = 'NOT_RUN'" in source
    for code in ("TIMED_OUT", "LAUNCH_FAILED", "EMPTY_RESULT", "INVALID_PROCESS", "RETURNED"):
        assert f"$report.duplicate_start_diagnostic = '{code}'" in launch
    assert "Start-Process -FilePath $exePath -WorkingDirectory $launchWorkingDirectory -PassThru -ErrorAction Stop" in launch
    assert "if (-not $duplicateStartCompleted.Wait(6000)) {" in launch
    assert "throw 'Second packaged launch bounded process creation timed out'" in launch
    assert "throw 'Second packaged launch did not return a valid process'" in launch
    assert "throw 'Second packaged launch returned invalid process receipt'" in launch
    assert "$Outcomes.Enqueue('LAUNCH_FAILED')" in launch
    assert "$Outcomes.Enqueue('INVALID_PROCESS')" in launch
    assert "$Outcomes.Enqueue('RETURNED')" in launch
    assert "$Completion.Set()" in launch
    assert "Stop-Job -Job $duplicateStartWorker" not in launch
    assert "duplicate_launch_status = 'PASS'" not in launch
    remainder = source[ready:]
    assert "$duplicateRoot.Current.Name" in remainder
    assert "$duplicateProcess.ExitCode -ne 2" in remainder
    assert "keyboard_shortcuts_status" in remainder
    assert "live_region_event_status" in remainder

def test_external_uia_threadjob_pool_cannot_starve_duplicate_launch() -> None:
    """Blocked UIA providers must not exhaust the five-thread default pool."""
    source = SCRIPT.read_text(encoding="utf-8")
    # The pool limit is session-global and is set by the first Start-ThreadJob.
    # Each call explicitly reserves enough capacity for four concurrent desktop
    # routes, independent handle/focus probes, and later duplicate-lock checks.
    assert source.count("Start-ThreadJob -ScriptBlock") == 5
    assert source.count("-ThrottleLimit 16") == 5
    assert "Start-ThreadJob -ScriptBlock ${function:Find-UiaSemanticViaChildren}" in source
    start = source.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_PROBE'")
    ready = source.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_READY'", start)
    launch = source[start:ready]
    assert "} -ArgumentList $duplicateStartArguments -ThrottleLimit 16" in launch
    assert "if (-not $duplicateStartCompleted.Wait(6000)) {" in launch
    assert "$report.duplicate_start_diagnostic = 'TIMED_OUT'" in launch
    assert "$report.duplicate_start_diagnostic = 'RETURNED'" in launch
    assert "duplicate_launch_status = 'PASS'" not in launch
