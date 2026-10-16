from __future__ import annotations

from pathlib import Path
import re


SCRIPT = Path(__file__).parents[1] / "scripts" / "external_uia_notification_listener.ps1"
TEXT = SCRIPT.read_text(encoding="utf-8")


def test_listener_uses_public_managed_uia_notification_surface() -> None:
    assert "AutomationElement]::NotificationEvent" in TEXT
    assert "Automation]::AddAutomationEventHandler" in TEXT
    assert "NotificationEventArgs" in TEXT
    assert "Automation]::RemoveAutomationEventHandler" in TEXT


def test_listener_never_reaches_into_provider_private_state_or_raises_events() -> None:
    forbidden = (
        "tk_uia._uiacore",
        "_BY_HWND",
        "UiaHostProviderFromHwnd",
        "UiaRaiseNotificationEvent",
    )
    for token in forbidden:
        assert token not in TEXT


def test_listener_defaults_to_digest_only_display_evidence() -> None:
    assert "[switch]$IncludeDisplayString" in TEXT
    assert "display_string_sha256" in TEXT
    assert "display_string_utf8_length" in TEXT
    assert re.search(
        r"if \(\$IncludeDisplayString\)\s*\{\s*\$record\.display_string",
        TEXT,
    )


def test_listener_binds_sender_and_notification_semantics() -> None:
    required = (
        "sender_process_id",
        "sender_automation_id",
        "notification_kind",
        "notification_processing",
        "activity_id",
        "process_family_ids",
    )
    for field in required:
        assert field in TEXT


def test_listener_records_focus_without_requesting_focus() -> None:
    assert "focus_before" in TEXT
    assert "focus_after" in TEXT
    assert "focus_changed" in TEXT
    assert ".SetFocus(" not in TEXT
    assert "SetFocus()" not in TEXT
    assert "focus_force" not in TEXT


def test_listener_has_bounded_waits_and_no_unbounded_event_loop() -> None:
    assert "TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 300" in TEXT
    assert "MinimumEvents -lt 0 -or $MinimumEvents -gt 10000" in TEXT
    assert "AddSeconds($TimeoutSeconds)" in TEXT
    assert "while ($true)" not in TEXT


def test_listener_capability_probe_does_not_claim_runtime_event_proof() -> None:
    assert "status = 'CAPABILITY_READY'" in TEXT
    assert "minimum_events = 0" in TEXT
    assert "CapabilityProbeOnly" in TEXT


def test_listener_hard_codes_prehuman_truth_false() -> None:
    for field in (
        "real_money_execution = $false",
        "human_tested = $false",
        "nvda_verified = $false",
        "whole_product_complete = $false",
    ):
        assert field in TEXT


def test_listener_uses_thread_safe_csharp_callback_recorder() -> None:
    assert "AutosportUiaNotificationRecord[] _items" in TEXT
    assert "public int Count" in TEXT
    assert "get { return _count; }" in TEXT
    assert TEXT.count("System.Runtime.CompilerServices.MethodImplOptions.Synchronized") == 3
    assert "lock (_gate)" not in TEXT
    assert "public bool TryDequeue(out AutosportUiaNotificationRecord item)" in TEXT
    assert "using System.Collections.Concurrent;" not in TEXT
    assert "AutomationEventHandler Handler" in TEXT
    assert "private void OnEvent" in TEXT
    assert "Records.Enqueue" in TEXT


def test_listener_unsubscribes_in_finally() -> None:
    finally_index = TEXT.index("} finally {")
    remove_index = TEXT.index("RemoveAutomationEventHandler")
    assert remove_index > finally_index


def test_capability_probe_executes_on_windows(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess
    import sys

    if sys.platform != "win32":
        import pytest

        pytest.skip("UI Automation capability probe is Windows-only")

    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    output = tmp_path / "uia-notification-capability.json"
    completed = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-Output",
            str(output),
            "-CapabilityProbeOnly",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(output.read_text(encoding="utf-8-sig"))
    assert report["status"] == "CAPABILITY_READY"
    assert report["notification_event_args_type"] == (
        "System.Windows.Automation.NotificationEventArgs"
    )
    assert report["real_money_execution"] is False
    assert report["human_tested"] is False
    assert report["nvda_verified"] is False
    assert report["whole_product_complete"] is False

def test_zero_event_runtime_threshold_is_not_a_passing_uia_observation() -> None:
    # The independent capability-only probe stays CAPABILITY_READY; a live
    # subscription must demand at least one real, process-bound notification.
    guard = "if (-not $CapabilityProbeOnly -and $MinimumEvents -eq 0) {"
    assert guard in TEXT
    assert "MinimumEvents must be at least 1 for runtime notification evidence" in TEXT
    assert TEXT.index(guard) < TEXT.index("if ($CapabilityProbeOnly) {")
    assert "if ($report.event_count -lt $MinimumEvents)" in TEXT


def test_zero_event_runtime_threshold_is_rejected_on_windows(tmp_path: Path) -> None:
    import shutil
    import subprocess
    import sys
    import pytest

    if sys.platform != "win32":
        pytest.skip("Runtime notification threshold probe requires Windows UIA")
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    output = tmp_path / "uia-zero-event-rejection.json"
    completed = subprocess.run(
        [
            pwsh, "-NoProfile", "-File", str(SCRIPT),
            "-Output", str(output), "-RootProcessId", "1",
            "-MinimumEvents", "0",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "MinimumEvents must be at least 1 for runtime notification evidence" in (
        completed.stdout + completed.stderr
    )
    # Preflight writes a fail-closed sentinel instead of retaining stale PASS.
    import json

    report = json.loads(output.read_text(encoding="utf-8-sig"))
    assert report["status"] == "FAIL"
    assert report["real_money_execution"] is False
    assert report["nvda_verified"] is False


def test_rejected_invocation_overwrites_stale_pass_on_windows(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess
    import sys
    import pytest

    if sys.platform != "win32":
        pytest.skip("Requires Windows PowerShell UIA runtime")
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    output = tmp_path / "stale-prior-pass.json"
    output.write_text('{"status":"PASS","event_count":99}', encoding="utf-8")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(SCRIPT), "-Output", str(output),
         "-RootProcessId", "1", "-MinimumEvents", "0"],
        check=False, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    report = json.loads(output.read_text(encoding="utf-8-sig"))
    assert report["status"] == "FAIL"
    assert report["source"] == "external_windows_uia_notification_client"
    assert report["real_money_execution"] is False
    assert report["human_tested"] is False
    assert report["nvda_verified"] is False
    assert report["whole_product_complete"] is False


def test_fail_closed_startup_precedes_external_uia_calls() -> None:
    # If Add-Type fails, a previously published PASS cannot survive.
    assert "$startupFailure | ConvertTo-Json" in TEXT
    assert TEXT.index("$startupFailure | ConvertTo-Json") < TEXT.index(
        "Add-Type -AssemblyName UIAutomationClient"
    )
    assert "status = 'FAIL'" in TEXT.split(
        "Add-Type -AssemblyName UIAutomationClient", 1
    )[0]


def test_process_family_discovery_uses_one_bounded_identity_snapshot() -> None:
    section = TEXT.split("function Get-ProcessFamilyIds {", 1)[1].split(
        "function Find-UiaRootForProcessFamily {", 1
    )[0]
    assert section.count("Get-CimInstance") == 1
    assert "-Property ProcessId, ParentProcessId" in section
    assert "-OperationTimeoutSec 5" in section
    assert "$inventory.Count -gt 8192" in section
    assert "$seen.Count -gt 512" in section
    assert "ParentProcessId = $currentId" not in section
    assert "CommandLine" not in section


def test_listener_does_not_serialize_untrusted_exception_messages() -> None:
    assert "$_.Exception.Message" not in TEXT
    assert "External UIA notification listener failed at stage=$listenStage" in TEXT
    assert "External UIA notification listener unsubscribe failed" in TEXT
    for stage in ("DISCOVER", "SUBSCRIBE", "LISTEN", "COLLECT", "VERIFY"):
        assert f"$listenStage = '{stage}'" in TEXT


def test_listener_hard_deadline_is_out_of_process_and_fail_closed() -> None:
    # In-process timeout polling cannot interrupt blocked COM/UIA calls.
    assert "[switch]$ListenerWorker" in TEXT
    assert "if (-not $CapabilityProbeOnly -and -not $ListenerWorker)" in TEXT
    assert "System.Diagnostics.Process" in TEXT
    assert "RedirectStandardOutput = $true" in TEXT
    assert "RedirectStandardError = $true" in TEXT
    assert "BeginOutputReadLine()" in TEXT
    assert "BeginErrorReadLine()" in TEXT
    assert "$proc.WaitForExit($budgetMs)" in TEXT
    assert "$proc.Kill($true)" in TEXT
    assert "$outputPath.worker." in TEXT
    assert "@($candidate.events).Count" in TEXT
    assert "candidate.real_money_execution -ne $false" in TEXT
    assert "Remove-Item -LiteralPath $workerOutputPath" in TEXT


def test_listener_invalid_root_fails_on_real_windows_without_claiming_events(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess
    import sys
    import pytest

    if sys.platform != "win32":
        pytest.skip("Requires a real Windows UI Automation runtime")
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    output = tmp_path / "uia-invalid-root.json"
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(SCRIPT), "-Output", str(output),
         "-RootProcessId", str(2**31 - 1), "-TimeoutSeconds", "1",
         "-MinimumEvents", "1"],
        check=False, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode != 0
    report = json.loads(output.read_text(encoding="utf-8-sig"))
    assert report["status"] == "FAIL"
    assert report["event_count"] == 0
    assert report["human_tested"] is False
    assert report["nvda_verified"] is False
    assert report["real_money_execution"] is False


def test_listener_csharp_compiler_binds_explicit_loaded_uia_references() -> None:
    # Fail-closed regression for Windows PowerShell 7 CS0234 with implicit Add-Type.
    assert "[System.Windows.Automation.Automation].Assembly.Location" in TEXT
    assert "[System.Windows.Automation.AutomationElement].Assembly.Location" in TEXT
    assert "[System.Windows.Automation.NotificationEventArgs].Assembly.Location" in TEXT
    assert "[System.Array].Assembly.Location" in TEXT
    assert "[System.Object].Assembly.Location" in TEXT
    assert "System.Private.CoreLib" in TEXT
    assert "(Join-Path $PSHOME 'System.Runtime.dll')" in TEXT
    assert "(Join-Path $PSHOME 'System.Collections.Concurrent.dll')" in TEXT
    assert "Add-Type -TypeDefinition $recorderSource -Language CSharp -ReferencedAssemblies $uiaReferences" in TEXT
    assert "Required .NET reference facade for UIA notification listener is unavailable" in TEXT
    # The original narrow list failed CS1069 (forwarded ConcurrentQueue)
    # on Windows .NET 10. Prove that the complete trusted CLR reference set
    # now participates in the exact Add-Type compilation instead.
    assert "[AppContext]::GetData('TRUSTED_PLATFORM_ASSEMBLIES')" in TEXT
    assert "$trustedAssemblyManifest -split [System.IO.Path]::PathSeparator" in TEXT
    assert "$uiaReferences + $runtimeFacades + $trustedFrameworkReferences" in TEXT
    assert "Trusted .NET framework reference manifest lacks System.Private.CoreLib" in TEXT


def test_trusted_clr_manifest_precedes_listener_compilation() -> None:
    manifest = TEXT.index("[AppContext]::GetData('TRUSTED_PLATFORM_ASSEMBLIES')")
    compile_site = TEXT.index("Add-Type -TypeDefinition $recorderSource -Language CSharp")
    assert TEXT.index("$startupFailure | ConvertTo-Json") < manifest < compile_site
    assert "-not [System.IO.Path]::IsPathFullyQualified($reference)" in TEXT
    assert "Trusted .NET framework reference manifest contains unavailable assembly" in TEXT
    assert "Trusted .NET framework reference manifest has invalid bounds" in TEXT
    assert "CAPABILITY_READY" in TEXT
    assert "AutosportUiaNotificationRecord[] _items" in TEXT
    assert "real_money_execution = $false" in TEXT
    assert "nvda_verified = $false"


def test_invalid_runtime_event_requirement_fails_before_uia_compiler() -> None:
    # The failure sentinel precedes validation; validation precedes foreign UIA.
    validate = "if (-not $CapabilityProbeOnly -and $MinimumEvents -eq 0) {"
    assert TEXT.count(validate) == 1
    assert TEXT.index("$startupFailure | ConvertTo-Json") < TEXT.index(validate)
    assert TEXT.index(validate) < TEXT.index("Add-Type -AssemblyName UIAutomationClient")
    assert TEXT.index("Add-Type -AssemblyName UIAutomationClient") < TEXT.index(
        "Add-Type -TypeDefinition $recorderSource -Language CSharp"
    )


def test_listener_precompiler_failure_has_zero_event_report_shape() -> None:
    # Compilation failure cannot leave a stale PASS or omit report fields.
    startup = TEXT.split("$startupFailure = [ordered]@{", 1)[1].split("\n}", 1)[0]
    assert "status = 'FAIL'" in startup
    assert "event_count = 0" in startup
    assert "events = @()" in startup
    for field in ("real_money_execution", "human_tested", "nvda_verified", "whole_product_complete"):
        assert f"{field} = $false" in startup
