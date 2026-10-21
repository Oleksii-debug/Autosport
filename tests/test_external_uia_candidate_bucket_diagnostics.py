"""Fail-closed Win32 candidate forensics, not a Windows UIA/NVDA PASS."""

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "external_uia_audit.ps1"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8-sig")


def test_candidate_class_bucket_is_privacy_bounded_win32_only() -> None:
    source = _script()
    method = source.split("public static string DiagnosticClassBucket(", 1)[1].split(
        "public static IntPtr[] ForProcessFamily(", 1
    )[0]
    assert "GetClassName(window, className, className.Capacity)" in method
    assert "IsWindowVisible(window)" in method
    for label in ("UNKNOWN", "RENDER_WIDGET", "CHROME_WIDGET", "OTHER"):
        assert f'"{label}"' in method
    assert "GetWindowText" not in method
    assert "return name" not in method
    assert "AutomationElement" not in method


def test_pre_com_bucket_snapshot_precedes_thread_job() -> None:
    source = _script()
    worker = source.split("function Find-BoundedUiaSemanticViaChildren {", 1)[1].split(
        "function Find-UiaRootWithElementForProcessFamily {", 1
    )[0]
    assert worker.index("$nativeCandidateBucket = if") < worker.index("Start-ThreadJob")
    assert "[AutosportNativeWindows]::DiagnosticClassBucket($NativeWindowHandle)" in worker
    assert "'NO_NATIVE_HANDLE'" in worker
    timeout = worker.split("if (-not $workerTerminal) {", 1)[1].split(
        "Write-ExternalUiaProgress -Stage 'SEMANTIC_WORKER_RESULT_PROBE'", 1
    )[0]
    assert "native_class_bucket=$nativeCandidateBucket" in timeout
    assert "last_worker_stage=$lastWorkerStage" in timeout
    assert "$script:semanticWorkerTimedOut = $true" in timeout
    assert "DiagnosticClassBucket" not in timeout
    assert "GetClassName" not in timeout
    assert "Stop-Job -Job $job" not in timeout
    assert "Remove-Job -Job $job" not in timeout


def test_diagnostics_do_not_replace_real_semantic_stop_and_keyboard_gates() -> None:
    source = _script()
    assert "AutomationElement]::FromHandle($NativeWindowHandle)" in source
    assert "[int]$Root.Current.ProcessId -ne $ExpectedProcessId" in source
    assert "[string]$node.Current.AutomationId -ceq $AutomationId" in source
    assert "emergency_stop_activation_status" in source
    assert "Wait-ForFocusedAutomationId" in source
    assert "live_region_event_status" in source
    assert "nvda_verified = $false" in source
