from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_EXTERNAL_UIA_AUDIT = _ROOT / "scripts" / "external_uia_audit.ps1"
_WINDOWS_WORKFLOW = _ROOT / ".github" / "workflows" / "windows-build.yml"


def _audit() -> str:
    return _EXTERNAL_UIA_AUDIT.read_text(encoding="utf-8")


def _windows_workflow() -> str:
    return _WINDOWS_WORKFLOW.read_text(encoding="utf-8")


def test_external_uia_process_family_snapshot_is_bounded_and_fail_closed() -> None:
    """Avoid per-descendant CIM round trips without bypassing real UIA evidence."""

    audit = _audit()
    start = audit.index("function Get-ProcessFamilyIds {")
    end = audit.index("function Find-UiaRootForProcessFamily {", start)
    helper = audit[start:end]
    assert helper.count("Get-CimInstance") == 1
    assert "Get-CimInstance -ClassName Win32_Process" in helper
    assert "-Property ProcessId, ParentProcessId" in helper
    assert "-OperationTimeoutSec 5 -ErrorAction Stop" in helper
    assert "$inventory.Count -gt 8192" in helper
    assert "$seen.Count -gt 512" in helper
    assert "$childrenByParent.ContainsKey($currentId)" in helper
    assert "$pending.Enqueue($childId)" in helper
    assert "SilentlyContinue" not in helper
    assert "Get-ProcessFamilyIds -RootProcessId $process.Id" in audit
    assert "SEMANTIC_FAMILY_IDS_READY" in audit
    assert "Find-UiaRootWithElementForProcessFamily" in audit


def test_external_uia_audit_loads_required_automation_assemblies() -> None:
    audit = _audit()

    assert "Add-Type -AssemblyName UIAutomationClient" in audit
    assert "Add-Type -AssemblyName UIAutomationTypes" in audit
    assert "Add-Type -AssemblyName System.Windows.Forms" in audit


def test_external_uia_audit_preloads_json_cmdlet_for_clean_background_worker() -> None:
    audit = _audit()

    module_import = "Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop"
    assert module_import in audit
    assert audit.index(module_import) < audit.index("Write-ExternalUiaProgress -Stage 'START'")
    assert audit.count("$report | ConvertTo-Json -Depth 8") == 2


def test_external_uia_live_region_probe_resolves_runtime_monitor_without_compiler_forwarders() -> None:
    """Maintain a real runtime Monitor lock without a C# Monitor type-reference gate."""

    audit = _audit()
    references = audit.split("$uiaReferences = @(", 1)[1].split(") | Select-Object -Unique", 1)[0]
    assert "[System.Windows.Automation.Automation].Assembly.Location" in references
    assert "[System.Windows.Automation.AutomationElementIdentifiers].Assembly.Location" in references
    assert "[System.Threading.Monitor].Assembly.Location" in references
    # Runtime type identity belongs to the CLR; the C# compiler must not be
    # required to statically resolve the forwarded Monitor type.
    assert 'typeof(object).Assembly.GetType(' in audit
    assert '"System.Threading.Monitor", throwOnError: true' in audit
    assert 'monitor.GetMethod(name, new[] { typeof(object) })' in audit
    assert 'Delegate.CreateDelegate(typeof(Action<object>), method)' in audit
    assert 'throw new InvalidOperationException("Monitor operation unavailable")' in audit
    assert 'private static readonly Action<object> EnterSync = ResolveMonitor("Enter");' in audit
    assert 'private static readonly Action<object> ExitSync = ResolveMonitor("Exit");' in audit
    assert "System.Threading.Monitor.Enter(_sync)" not in audit
    assert "System.Threading.Monitor.Exit(_sync)" not in audit
    assert "Interlocked" not in audit
    assert "Volatile" not in audit
    assert "lock (_sync)" not in audit
    assert audit.count("EnterSync(_sync)") == 4
    assert audit.count("ExitSync(_sync)") == 4
    assert "finally { ExitSync(_sync); }" in audit
    assert "EnterSync(_sync);\n            try" in audit
    assert "var automationId = element.Current.AutomationId ?? \"\";" in audit
    assert "var name = element.Current.Name ?? \"\";" in audit
    assert "_lastAutomationId = automationId;" in audit
    assert "_lastName = name;" in audit


def test_external_uia_core_compilation_includes_facades_without_skipping_gate() -> None:
    """PowerShell 7 Roslyn must resolve Monitor through the runtime facades."""
    audit = _audit()
    assert "$PSVersionTable.PSEdition -eq 'Core'" in audit
    assert "(Join-Path $PSHOME 'System.Runtime.dll')" in audit
    assert "(Join-Path $PSHOME 'System.Threading.dll')" in audit
    assert "Test-Path -LiteralPath $reference -PathType Leaf" in audit
    assert "$uiaReferences = @($uiaReferences + $runtimeFacades | Select-Object -Unique)" in audit
    assert "Add-Type -ReferencedAssemblies $uiaReferences -TypeDefinition" in audit
    assert "throw 'Required .NET reference facade for external UIA audit is unavailable'" in audit


def test_duplicate_dialog_probe_runs_real_uia_in_killable_mta_process() -> None:
    audit = _audit()
    helper_start = audit.index("function Invoke-IsolatedDuplicateDialogProbe {")
    helper_end = audit.index("function Find-UiaRootWithElementForProcessFamily {", helper_start)
    helper = audit[helper_start:helper_end]

    assert "'-NoLogo', '-NoProfile', '-NonInteractive', '-Mta'" in helper
    assert "$child.WaitForExit(6000)" in helper
    assert "$child.Kill($true)" in helper
    assert "Get-Process -Id $candidateId -ErrorAction Stop" in helper
    assert "AUTOSPORT_UIA_DUPLICATE_HANDLES" in helper
    assert "[System.Windows.Automation.AutomationElement]::FromHandle(" in helper
    assert "[IntPtr]::new($handleValue)" in helper
    assert "[System.Windows.Automation.AutomationElement]::RootElement" not in helper
    assert "[System.Windows.Automation.AutomationElement]::AutomationIdProperty" in helper
    assert "'Автоспорт — помилка запуску'" in helper
    assert "'уже відкритий'" in helper
    assert "'Economic і live state не змінено'" in helper
    assert "'SECOND_SEMANTIC_SURFACE'" in helper
    assert "'DIALOG_CLOSED'" in helper

    duplicate_start = audit.index(
        "$duplicateDeadline = [DateTime]::UtcNow.AddSeconds("
    )
    duplicate_end = audit.index(
        "if (-not $duplicateProcess.WaitForExit(5000))", duplicate_start
    )
    duplicate = audit[duplicate_start:duplicate_end]
    assert "[AutosportNativeWindows]::VisibleTopLevelWindowsForProcess(" in duplicate
    assert "$duplicateNativeHandles.Count -gt 4096" in duplicate
    assert "-ProcessIds $duplicateFamilyIds" in duplicate
    assert "-WindowHandles ([IntPtr[]]$duplicateNativeHandles)" in duplicate
    assert "$duplicateProbe -ceq 'DIALOG_CLOSED'" in duplicate
    assert "$report.duplicate_launch_dialog_title = 'Автоспорт — помилка запуску'" in duplicate
    assert "$duplicateWorker = Start-ThreadJob" not in duplicate
    assert "$duplicateRoot.Current.Name" not in duplicate


def test_real_duplicate_dialog_helper_regressions_run_in_early_windows_control() -> None:
    workflow = (_ROOT / ".github/workflows/windows-uia-environment-control.yml").read_text(
        encoding="utf-8"
    )
    assert "python -m pytest -q --noconftest tests/test_external_uia_duplicate_native_dialog.py" in workflow
    assert "'tests/test_external_uia_duplicate_native_dialog.py'" in workflow


def test_external_uia_audit_uses_runner_safe_legacy_action_pattern_lookup() -> None:
    audit = _audit()

    assert "[System.Windows.Automation.AutomationPattern]::LookupById(10018)" in audit
    assert "[System.Windows.Automation.LegacyIAccessiblePattern]" not in audit
    assert "$legacyObject.Current.DefaultAction" in audit
    assert "$action.Pattern.DoDefaultAction()" in audit
    assert "Kind = 'KeyboardButton'" in audit
    assert "$Element.SetFocus()" in audit
    assert "[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')" in audit
    assert "keyboard-actionable Button semantics" in audit


def test_external_uia_audit_preserves_keyboard_fallback_without_legacy_pattern() -> None:
    audit = _audit()
    helper_start = audit.index("function Get-ExternalActionPattern")
    helper_end = audit.index("function Test-Pattern")
    helper = audit[helper_start:helper_end]

    assert "if ($null -eq $legacyPattern) { return $null }" not in helper
    assert "if ($null -ne $legacyPattern) {" in helper
    assert helper.index("if ($null -ne $legacyPattern) {") < helper.index("Kind = 'KeyboardButton'")


def test_manual_result_uia_name_matches_shipped_webview_label() -> None:
    audit = _audit()
    html = (_ROOT / "src" / "autosport" / "windows_web" / "index.html").read_text(
        encoding="utf-8"
    )
    expected = "Результат і докази ручного розрахунку"

    result_line = next(
        line for line in audit.splitlines() if "automation_id = '334'" in line
    )
    assert f"name = '{expected}'" in result_line
    assert f'aria-label="{expected}"' in html


def test_external_uia_audit_requires_semantic_control_type_for_critical_controls() -> None:
    audit = _audit()
    expected_types = {
        "101": "ControlType.Button",
        "102": "ControlType.Button",
        "103": "ControlType.ComboBox",
        "104": "ControlType.ComboBox",
        "105": "ControlType.Button",
        "106": "ControlType.ComboBox",
        "107": "ControlType.Button",
        "108": "ControlType.Button",
        "201": "ControlType.Table",
        "202": "ControlType.Edit",
        "203": "ControlType.List",
        "204": "ControlType.List",
        "205": "ControlType.Edit",
        "301": "ControlType.ComboBox",
        "302": "ControlType.Edit",
        "303": "ControlType.Button",
        "304": "ControlType.List",
        "305": "ControlType.Button",
        "306": "ControlType.Edit",
        "307": "ControlType.List",
        "329": "ControlType.Button",
        "product-source-select": "ControlType.ComboBox",
        "product-source-save": "ControlType.Button",
        "product-runtime-start": "ControlType.Button",
        "product-runtime-stop": "ControlType.Button",
        "product-runtime-status": "ControlType.Edit",
        "emergency-stop-action": "ControlType.Button",
        "330": "ControlType.Button",
        "331": "ControlType.ComboBox",
        "332": "ControlType.Edit",
        "333": "ControlType.Button",
        "334": "ControlType.Edit",
        "335": "ControlType.Button",
        "336": "ControlType.Button",
    }

    for automation_id, control_type in expected_types.items():
        matching_lines = [
            line
            for line in audit.splitlines()
            if f"automation_id = '{automation_id}'" in line
        ]
        assert len(matching_lines) == 1
        assert f"expected_control_type = '{control_type}'" in matching_lines[0]

    assert "external UIA ControlType mismatch" in audit


def test_ticket_table_checks_data_names_without_rejecting_named_header_structure() -> None:
    audit = _audit()
    tickets_line = next(
        line for line in audit.splitlines() if "automation_id = '201'" in line
    )

    assert "expected_control_type = 'ControlType.Table'" in tickets_line
    assert "child_types = @('ControlType.DataItem', 'ControlType.Row')" in tickets_line
    assert "function Test-IsNamedStructuralHeaderRow" in audit
    assert "$childType -ne 'ControlType.HeaderItem'" in audit
    assert "$typeName -eq 'ControlType.Row'" in audit
    assert "(Test-IsNamedStructuralHeaderRow -Element $item)" in audit
    assert "$childStats.UnnamedCount -gt 0" in audit
    assert "exposes $($childStats.UnnamedCount) unnamed semantic collection items" in audit


def test_collection_surfaces_require_expected_semantic_children() -> None:
    audit = _audit()

    for automation_id, child_type in {
        "203": "ControlType.ListItem",
        "204": "ControlType.ListItem",
        "304": "ControlType.ListItem",
        "307": "ControlType.ListItem",
    }.items():
        line = next(
            candidate
            for candidate in audit.splitlines()
            if f"automation_id = '{automation_id}'" in candidate
        )
        assert f"child_types = @('{child_type}')" in line

    assert "semantic_child_count = $childStats.CandidateCount" in audit
    assert "named_semantic_child_count = $childStats.NamedCount" in audit
    assert "unnamed_semantic_child_count = $childStats.UnnamedCount" in audit


def test_external_uia_audit_fails_closed_on_writable_readonly_value() -> None:
    audit = _audit()

    assert "[System.Windows.Automation.ValuePattern]::Pattern" in audit
    assert "$valuePattern.Current.IsReadOnly" in audit
    assert "value_read_only_required = $valueReadOnlyRequired" in audit
    assert "value_read_only = $valueReadOnly" in audit
    assert "$valueReadOnlyRequired -and $valueReadOnly -ne $true" in audit
    assert "external UIA ValuePattern is writable or read-only state unavailable" in audit



def test_external_uia_audit_launches_with_explicit_evidence_bound_working_directory() -> None:
    audit = _audit()

    assert "[string]$WorkingDirectory = ''" in audit
    assert "launch_working_directory = $null" in audit
    assert "$report.launch_working_directory = $launchWorkingDirectory" in audit
    assert (
        "$process = Start-Process -FilePath $exePath "
        "-WorkingDirectory $launchWorkingDirectory -PassThru"
    ) in audit


def test_external_uia_failure_preserves_bounded_launcher_lifetime_before_cleanup() -> None:
    """A vanished packaged GUI must not be misreported as an NVDA/UIA success."""
    audit = _audit()

    assert "launcher_exit_state = 'NOT_OBSERVED'" in audit
    assert "launcher_exit_code = $null" in audit
    assert "initial_window_observed = $false" in audit
    assert "$report.initial_window_observed = $true" in audit

    diagnostic = audit.split(
        "} finally {\n    # Preserve a bounded count from the actual pre-cleanup UIA search.",
        1,
    )[1].split("    if ($null -ne $liveRegionProbe)", 1)[0]

    assert "$process.Refresh()" in diagnostic
    assert "$process.HasExited" in diagnostic
    assert "$report.launcher_exit_state = 'EXITED'" in diagnostic
    assert "$report.launcher_exit_state = 'RUNNING'" in diagnostic
    assert "$report.launcher_exit_state = 'UNAVAILABLE'" in diagnostic
    assert "$report.launcher_exit_code = [int]$process.ExitCode" in diagnostic
    assert "command lines, workspace paths, or environment" in diagnostic
    assert "$report.status = 'PASS'" not in diagnostic
    assert "Stop-Process" not in diagnostic
    assert audit.index("$report.initial_window_observed = $true") < audit.index(
        "Timed out waiting for WebView2 semantic UIA readiness"
    )


def test_windows_candidate_external_uia_uses_system32_as_hostile_child_cwd() -> None:
    workflow = _windows_workflow()
    step_start = workflow.index("- name: External UIA fresh-extraction gate")
    step_end = workflow.index("- name: Upload external UIA failure evidence", step_start)
    step = workflow[step_start:step_end]

    assert "$hostileCwd = (Resolve-Path -LiteralPath (Join-Path $env:SystemRoot 'System32')).Path" in step
    assert "-WorkingDirectory $hostileCwd" in step
    assert "External UIA hostile CWD unexpectedly aliases the repository checkout" in step
    assert "External UIA evidence did not preserve the hostile child working directory" in step
    assert "extracted_external_uia_hostile_cwd_status" in step



def test_windows_candidate_external_uia_fences_package_root_against_writes() -> None:
    workflow = _windows_workflow()
    step_start = workflow.index("- name: External UIA fresh-extraction gate")
    step_end = workflow.index("- name: Upload external UIA failure evidence", step_start)
    step = workflow[step_start:step_end]

    deny = '& icacls $packageRoot /deny "*${currentSid}:(OI)(CI)(WD,AD,WEA,WA,DE,DC)" /T /C'
    cleanup = '& icacls $packageRoot /remove:d "*${currentSid}" /T /C'
    assert deny in step
    assert cleanup in step
    assert step.index("try {") < step.index(deny) < step.index("} finally {") < step.index(cleanup)
    assert "External UIA package write fence exited $LASTEXITCODE" in step
    assert "External UIA package write-fence cleanup exited $LASTEXITCODE" in step
    assert "extracted_external_uia_read_only_package_status" in step
    assert "-Exe (Join-Path $packageRoot 'Autosport.exe')" in step
    assert "-WorkingDirectory $hostileCwd" in step

def test_external_uia_audit_rejects_second_real_packaged_launch() -> None:
    audit = _audit()

    first_launch = (
        "$process = Start-Process -FilePath $exePath "
        "-WorkingDirectory $launchWorkingDirectory -PassThru"
    )
    second_launch = "$duplicateProcess.Start()"
    assert "$startInfo.UseShellExecute = $true" in audit
    assert "$startInfo.UseShellExecute = $false" not in audit
    assert "$startInfo.WorkingDirectory = $launchWorkingDirectory" in audit
    assert first_launch in audit
    assert second_launch in audit
    assert audit.index(first_launch) < audit.index(second_launch)
    assert "duplicate_launch_status = 'NOT_RUN'" in audit
    assert "$report.duplicate_launch_status = 'PASS'" in audit
    assert "$report.duplicate_launch_exit_code = [int]$duplicateProcess.ExitCode" in audit
    assert "$duplicateProcess.ExitCode -ne 2" in audit
    assert "'SECOND_SEMANTIC_SURFACE'" in audit
    assert "duplicate-dialog probe failed closed; fixed_code=$duplicateProbe" in audit
    assert "уже відкритий" in audit
    assert "Economic і live state не змінено" in audit


def test_missing_duplicate_dialog_exposes_bounded_process_state_without_qualifying() -> None:
    audit = _audit()
    start = audit.index("if (-not $duplicateDialogObserved) {")
    end = audit.index("$report.duplicate_launch_status = 'PASS'", start)
    refusal = audit[start:end]

    assert "$duplicateProcess.Refresh()" in refusal
    assert "$duplicateProcess.HasExited" in refusal
    assert '$duplicateExitState = "exited:$([int]$duplicateProcess.ExitCode)"' in refusal
    assert "$duplicateExitState = 'running'" in refusal
    assert "$duplicateExitState = 'unavailable'" in refusal
    assert "$duplicateObservedFamilyCount = @($duplicateFamilyIds).Count" in refusal
    assert "process=$duplicateExitState; family_count=$duplicateObservedFamilyCount" in refusal
    assert "throw \"Second packaged launch did not expose" in refusal
    assert "$report.duplicate_launch_status = 'PASS'" not in refusal
    assert "workspacePath" not in refusal


def test_duplicate_packaged_launch_uses_bounded_startup_budget_without_weakening_gate() -> None:
    audit = _audit()
    start = audit.index("$duplicateProcess = [System.Diagnostics.Process]::new()")
    end = audit.index("$report.duplicate_launch_status = 'PASS'", start)
    duplicate = audit[start:end]
    # A slow frozen application on the runner must not be incorrectly qualified.
    assert "$duplicateDeadline = [DateTime]::UtcNow.AddSeconds(" in duplicate
    assert "[Math]::Max(90, $TimeoutSeconds)" in duplicate
    assert "[Math]::Min(10, $TimeoutSeconds)" not in duplicate
    # Expensive cross-process enumeration is rate limited, and polling is bounded.
    assert "$nextDuplicateFamilyProbe = [DateTime]::MinValue" in duplicate
    assert "$nextDuplicateFamilyProbe = [DateTime]::UtcNow.AddMilliseconds(1000)" in duplicate
    assert "Start-Sleep -Milliseconds 250" in duplicate
    assert "while ([DateTime]::UtcNow -lt $duplicateDeadline)" in duplicate
    # Neither a running process nor a missing dialog may be mistaken for PASS.
    assert "if (-not $duplicateDialogObserved) {" in duplicate
    assert "throw \"Second packaged launch did not expose" in duplicate
    assert "-ProcessIds $duplicateFamilyIds" in duplicate
    assert "-WindowHandles ([IntPtr[]]$duplicateNativeHandles)" in duplicate
    assert "$duplicateProbe -ceq 'DIALOG_CLOSED'" in duplicate
    assert "$report.duplicate_launch_dialog_title = 'Автоспорт — помилка запуску'" in duplicate
    assert "Автоспорт — помилка запуску" in duplicate
    assert "$duplicateProcess.ExitCode -ne 2" in duplicate


def test_duplicate_root_uia_lookup_is_bounded_without_skipping_dialog_evidence() -> None:
    """A stalled provider cannot hold the isolated duplicate dialog gate indefinitely."""
    audit = _audit()
    start = audit.index("$duplicateProcess = [System.Diagnostics.Process]::new()")
    end = audit.index("$report.duplicate_launch_status = 'PASS'", start)
    duplicate = audit[start:end]
    assert "Write-ExternalUiaProgress -Stage 'DUPLICATE_UIA_ROOT_PROBE'" in duplicate
    assert "-ProcessIds $duplicateFamilyIds" in duplicate
    assert "-WindowHandles ([IntPtr[]]$duplicateNativeHandles)" in duplicate
    assert "$report.duplicate_dialog_probe_diagnostic = [string]$duplicateProbe" in duplicate
    assert "$duplicateProbe -ceq 'DIALOG_CLOSED'" in duplicate
    assert "'TIMEOUT'" in audit
    assert "$child.WaitForExit(6000)" in audit
    assert "$child.Kill($true)" in audit
    assert "$duplicateWorker = Start-ThreadJob" not in duplicate
    assert "$duplicateRoot.Current.Name" not in duplicate
    assert "$duplicateProcess.ExitCode -ne 2" in duplicate
    assert "'SECOND_SEMANTIC_SURFACE'" in audit


def test_windows_candidate_requires_duplicate_launch_evidence() -> None:
    workflow = _windows_workflow()
    step_start = workflow.index("- name: External UIA fresh-extraction gate")
    step_end = workflow.index("- name: Upload external UIA failure evidence", step_start)
    step = workflow[step_start:step_end]

    assert "$external.duplicate_launch_status -ne 'PASS'" in step
    assert "[int]$external.duplicate_launch_exit_code -ne 2" in step
    assert "External UIA duplicate-launch dialog title mismatch" in step
    assert "extracted_external_uia_duplicate_launch_status" in step



def test_external_uia_gate_covers_product_runtime_and_emergency_stop_controls() -> None:
    audit = _audit()

    expected = {
        "product-source-select": (
            "Джерело даних для тривалої симуляційної роботи",
            "ControlType.ComboBox",
        ),
        "product-source-save": (
            "Зберегти джерело даних",
            "ControlType.Button",
        ),
        "product-runtime-start": (
            "Запустити симуляційну роботу",
            "ControlType.Button",
        ),
        "product-runtime-stop": (
            "Зупинити симуляційну роботу",
            "ControlType.Button",
        ),
        "product-runtime-status": (
            "Стан тривалої симуляційної роботи",
            "ControlType.Edit",
        ),
        "emergency-stop-action": (
            "Активувати аварійний STOP",
            "ControlType.Button",
        ),
    }

    for automation_id, (name, control_type) in expected.items():
        lines = [
            line for line in audit.splitlines()
            if f"automation_id = '{automation_id}'" in line
        ]
        assert len(lines) == 1
        assert f"name = '{name}'" in lines[0]
        assert f"expected_control_type = '{control_type}'" in lines[0]

    emergency = next(
        line for line in audit.splitlines()
        if "automation_id = 'emergency-stop-action'" in line
    )
    assert "required_pattern = 'Action'" in emergency
    assert "require_external_focus = $true" in emergency

    runtime_status = next(
        line for line in audit.splitlines()
        if "automation_id = 'product-runtime-status'" in line
    )
    assert "required_pattern = 'Value'" in runtime_status
    assert "require_value_read_only = $true" in runtime_status

    for automation_id in ("product-runtime-start", "product-runtime-stop"):
        line = next(
            line for line in audit.splitlines()
            if f"automation_id = '{automation_id}'" in line
        )
        assert "allow_disabled = $true" in line


def test_external_uia_pass_requires_ordinary_main_window_close_not_cleanup_kill() -> None:
    audit = _audit()

    assert "normal_close_status = 'NOT_RUN'" in audit
    assert "normal_close_exit_code = $null" in audit
    assert "[System.Windows.Automation.WindowPattern]::Pattern" in audit
    assert "([System.Windows.Automation.WindowPattern]$closePatternObject).Close()" in audit
    assert "[System.Windows.Forms.SendKeys]::SendWait('%{F4}')" in audit
    assert "$closeDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(10, $TimeoutSeconds))" in audit
    assert "alive_process_ids=" in audit
    assert "$process.WaitForExit()" in audit
    assert "$report.normal_close_exit_code = [int]$process.ExitCode" in audit
    assert "$report.normal_close_status = 'PASS'" in audit
    assert audit.index("$report.normal_close_status = 'PASS'") < audit.index(
        "$report.status = 'PASS'"
    )

    cleanup_force = "Stop-Process -Id $cleanupId -Force"
    assert cleanup_force in audit
    assert audit.index("$report.status = 'PASS'") < audit.index(cleanup_force)


def test_windows_candidate_requires_external_uia_normal_close_evidence() -> None:
    workflow = _windows_workflow()
    step_start = workflow.index("- name: External UIA fresh-extraction gate")
    step_end = workflow.index("- name: Upload external UIA failure evidence", step_start)
    step = workflow[step_start:step_end]

    assert "$external.normal_close_status -ne 'PASS'" in step
    assert "[int]$external.normal_close_exit_code -ne 0" in step
    assert "External UIA evidence did not prove ordinary packaged close/teardown" in step
    assert "extracted_external_uia_normal_close_status" in step


def test_external_uia_audit_binds_packaged_session_to_actual_runtime_witness() -> None:
    audit = _audit()

    assert "[string]$Workspace = ''" in audit
    assert "runtime_witness_status = 'NOT_REQUESTED'" in audit
    assert "runtime_witness_path = $null" in audit
    assert "runtime_browser_version = $null" in audit
    assert "webview2-runtime-witness.json" in audit
    assert "native_core_webview2_environment" in audit
    assert "$runtimeWitness.schema_version -ne 1" in audit
    assert "[string]$runtimeWitness.renderer -ne 'edgechromium'" in audit
    assert "$runtimeWitness.real_money_execution -ne $false" in audit
    assert "$runtimeWitness.human_tested -ne $false" in audit
    assert "$runtimeWitness.nvda_verified -ne $false" in audit
    assert "$runtimeWitness.whole_product_complete -ne $false" in audit
    assert "$report.runtime_browser_version = $browserVersion" in audit
    assert "$report.runtime_witness_status = 'PASS'" in audit


def test_external_uia_runtime_witness_wait_is_bounded_and_never_fabricates_evidence() -> None:
    """DOM UIA readiness may precede the native before_load witness write."""

    audit = _audit()
    start = audit.index("$runtimeWitnessPath = Join-Path $workspacePath")
    end = audit.index("$report.runtime_witness_status = 'PASS'", start)
    binding = audit[start:end]

    # Wait only for the actual product-emitted file, within the launch deadline.
    assert "while (" in binding
    assert "-not (Test-Path -LiteralPath $runtimeWitnessPath -PathType Leaf) -and" in binding
    assert "[DateTime]::UtcNow -lt $deadline" in binding
    assert "Start-Sleep -Milliseconds 100" in binding
    assert binding.count("Test-Path -LiteralPath $runtimeWitnessPath -PathType Leaf") == 2
    assert 'throw "Packaged WebView2 session did not publish the actual runtime witness"' in binding

    # Preserve the exact witness-read and fail-closed schema/truth verification.
    assert "Get-Content -LiteralPath $runtimeWitnessPath -Raw | ConvertFrom-Json" in binding
    assert "$runtimeWitness.schema_version -ne 1" in binding
    assert "$runtimeWitness.observation_source" in binding
    assert "native_core_webview2_environment" in binding
    assert "$runtimeWitness.real_money_execution -ne $false" in binding
    assert "$runtimeWitness.nvda_verified -ne $false" in binding
    assert "Set-Content" not in binding
    assert "New-Item" not in binding



def test_external_uia_failure_classifies_only_known_windows_before_cleanup() -> None:
    """Classify native windows once; never reenter a stalled COM provider in cleanup."""
    audit = _audit()
    assert "observed_window_class = 'NOT_OBSERVED'" in audit
    native = audit.split(
        "Write-ExternalUiaProgress -Stage 'NATIVE_ROOT_NAME_PROBE'", 1
    )[1].split("    $semanticReady = $null", 1)[0]
    assert "[string]$uiaRoot.Current.Name" in native
    assert "[System.StringComparison]::Ordinal" in native
    assert "observed_window_class = 'NATIVE_EMERGENCY_STOP'" in native
    assert "observed_window_class = 'NATIVE_STARTUP_ERROR'" in native
    assert "observed_window_class = 'PRODUCT_MAIN_WINDOW'" in native
    cleanup = audit.split(
        "} catch {\n    if ($script:lastDesktopWorkerStage -ne 'NOT_RUN') {", 1
    )[1].split("} finally {", 1)[1]
    assert "Never reenter the UIA COM provider during failure cleanup" in cleanup
    assert "$report.observed_window_class =" not in cleanup
    assert not any(
        "$uiaRoot.Current.Name" in line
        for line in cleanup.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert "$report | ConvertTo-Json -Depth 8 | Set-Content" in cleanup
    assert "if ($report.status -ne 'PASS')" in audit

def test_windows_candidate_requires_packaged_runtime_witness_binding() -> None:
    workflow = _windows_workflow()
    step_start = workflow.index("- name: External UIA fresh-extraction gate")
    step_end = workflow.index("- name: Upload external UIA failure evidence", step_start)
    step = workflow[step_start:step_end]

    assert "$isolatedRoot = Join-Path $env:RUNNER_TEMP 'Autosport-external-uia'" in step
    assert "$isolatedLocalAppData = Join-Path $isolatedRoot 'localappdata'" in step
    assert "$expectedWorkspace = Join-Path $isolatedRoot 'workspace'" in step
    assert "$env:LOCALAPPDATA = $isolatedLocalAppData" in step
    assert "$env:AUTOSPORT_WORKSPACE = $expectedWorkspace" in step
    assert "-Workspace $expectedWorkspace" in step
    assert "$external.runtime_witness_status -ne 'PASS'" in step
    assert "$external.runtime_browser_version" in step
    assert "webview2-runtime-witness.json" in step
    assert "External UIA evidence did not bind the packaged session" in step
    assert "extracted_external_uia_runtime_witness_status" in step
    assert "extracted_external_uia_runtime_browser_version" in step


def test_external_uia_gate_scrubs_prior_runner_state_and_restores_environment() -> None:
    workflow = _windows_workflow()
    step_start = workflow.index("- name: External UIA fresh-extraction gate")
    step_end = workflow.index("- name: Upload external UIA failure evidence", step_start)
    step = workflow[step_start:step_end]

    assert "$hadLocalAppData = Test-Path Env:LOCALAPPDATA" in step
    assert "$hadWorkspaceOverride = Test-Path Env:AUTOSPORT_WORKSPACE" in step
    assert "$originalLocalAppData = $env:LOCALAPPDATA" in step
    assert "$originalWorkspaceOverride = $env:AUTOSPORT_WORKSPACE" in step
    assert "Remove-Item -LiteralPath $isolatedRoot -Recurse -Force" in step
    assert "New-Item -ItemType Directory -Path $isolatedLocalAppData -Force" in step
    assert "New-Item -ItemType Directory -Path $expectedWorkspace -Force" in step
    assert "$env:LOCALAPPDATA = $isolatedLocalAppData" in step
    assert "$env:AUTOSPORT_WORKSPACE = $expectedWorkspace" in step

    finally_index = step.index("} finally {")
    assert finally_index < step.index("$env:LOCALAPPDATA = $originalLocalAppData")
    assert finally_index < step.index("$env:AUTOSPORT_WORKSPACE = $originalWorkspaceOverride")
    assert "Remove-Item Env:LOCALAPPDATA -ErrorAction SilentlyContinue" in step
    assert "Remove-Item Env:AUTOSPORT_WORKSPACE -ErrorAction SilentlyContinue" in step
    assert "extracted_external_uia_isolated_workspace" in step
    assert "extracted_external_uia_isolated_localappdata" in step


def test_external_uia_exercises_packaged_f2_and_f8_focus_contract() -> None:
    audit = _audit()

    assert "keyboard_shortcuts_status = 'NOT_RUN'" in audit
    assert "f2_focus_automation_id = $null" in audit
    assert "f8_focus_automation_id = $null" in audit
    assert "[System.Windows.Forms.SendKeys]::SendWait('{F2}')" in audit
    assert "[System.Windows.Forms.SendKeys]::SendWait('{F8}')" in audit
    assert "function Wait-ForFocusedAutomationId" in audit
    assert "[System.Windows.Automation.AutomationElement]::FocusedElement" in audit
    assert "Start-Sleep -Milliseconds 50" in audit
    assert "Wait-ForFocusedAutomationId -RootProcessId $process.Id -AutomationId '301' -Deadline $shortcutDeadline" in audit
    assert "Wait-ForFocusedAutomationId -RootProcessId $process.Id -AutomationId '204' -Deadline $shortcutDeadline" in audit
    assert "$null -eq $focusedF2" in audit
    assert "$null -eq $focusedF8" in audit
    assert "Start-Sleep -Milliseconds 150" not in audit
    assert "$report.keyboard_shortcuts_status = 'PASS'" in audit


def test_windows_candidate_requires_external_keyboard_shortcut_evidence() -> None:
    workflow = _windows_workflow()
    step_start = workflow.index("- name: External UIA fresh-extraction gate")
    step_end = workflow.index("- name: Upload external UIA failure evidence", step_start)
    step = workflow[step_start:step_end]

    assert "$external.keyboard_shortcuts_status -ne 'PASS'" in step
    assert "[string]$external.f2_focus_automation_id -ne '301'" in step
    assert "[string]$external.f8_focus_automation_id -ne '204'" in step
    assert "External UIA evidence did not prove packaged F2/F8 keyboard focus navigation" in step
    assert "extracted_external_uia_keyboard_shortcuts_status" in step
    assert "extracted_external_uia_f2_focus_automation_id" in step
    assert "extracted_external_uia_f8_focus_automation_id" in step


def test_external_uia_closes_disclosures_and_proves_trigger_focus_handoff() -> None:
    audit = _audit()

    owner_close = next(
        line for line in audit.splitlines() if "automation_id = '329'" in line
    )
    assert "name = 'Закрити економічні межі'" in owner_close
    assert "required_pattern = 'Action'" in owner_close
    assert "require_external_focus = $true" in owner_close
    assert "expected_control_type = 'ControlType.Button'" in owner_close

    assert "-AutomationId '329'" in audit
    assert "Invoke-ExternalAction -Element $ownerClose" in audit
    assert "Wait-ForFocusedAutomationId -RootProcessId $process.Id -AutomationId '305' -Deadline $focusDeadline" in audit
    assert "owner disclosure close did not restore focus to automation_id=305" in audit

    assert "-AutomationId '336'" in audit
    assert "Invoke-ExternalAction -Element $manualClose" in audit
    assert "Wait-ForFocusedAutomationId -RootProcessId $process.Id -AutomationId '330' -Deadline $focusDeadline" in audit
    assert "manual disclosure close did not restore focus to automation_id=330" in audit
    assert "packaged disclosure focus-handoff audit failed" in audit


def test_external_uia_activates_packaged_durable_emergency_stop() -> None:
    audit = _audit()

    assert "emergency_stop_activation_status = 'NOT_RUN'" in audit
    assert "emergency_stop_status_text = $null" in audit
    assert "emergency_stop_journal_path = $null" in audit
    assert "execution-stop.jsonl" in audit
    assert "pre-existing STOP journal" in audit
    assert "-AutomationId 'emergency-stop-action'" in audit
    assert "Invoke-ExternalAction -Element $stopButton" in audit
    assert "-AutomationId 'emergency-stop-status'" in audit
    assert "'^Аварійний STOP (активовано|активний)'" in audit
    assert "'підтверджено стійкий запис ревізії'" in audit
    assert "Wait-ForFocusedAutomationId -RootProcessId $process.Id -AutomationId 'emergency-stop-status' -Deadline $stopFocusDeadline" in audit
    assert "confirmation did not retain focus on the dedicated status" in audit
    assert "packaged emergency STOP durable journal is empty" in audit
    assert "$report.emergency_stop_activation_status = 'PASS'" in audit


def test_windows_candidate_requires_packaged_durable_emergency_stop_evidence() -> None:
    workflow = _windows_workflow()
    step_start = workflow.index("- name: External UIA fresh-extraction gate")
    step_end = workflow.index("- name: Upload external UIA failure evidence", step_start)
    step = workflow[step_start:step_end]

    assert "$external.emergency_stop_activation_status -ne 'PASS'" in step
    assert "$external.emergency_stop_status_text" in step
    assert "(Join-Path $expectedWorkspace 'execution-stop.jsonl')" in step
    assert "External UIA evidence did not prove packaged durable emergency STOP activation" in step
    assert "extracted_external_uia_emergency_stop_activation_status" in step
    assert "extracted_external_uia_emergency_stop_status_text" in step


def test_external_uia_observes_live_region_changed_for_packaged_emergency_stop() -> None:
    audit = _audit()

    assert "AutosportExternalLiveRegionProbe" in audit
    assert "AutomationElementIdentifiers.LiveRegionChangedEvent" in audit
    assert "Automation.AddAutomationEventHandler" in audit
    assert "Automation.RemoveAutomationEventHandler" in audit
    assert "live_region_event_status = 'NOT_RUN'" in audit
    assert "live_region_event_count = 0" in audit
    assert "live_region_event_automation_id = $null" in audit
    assert "live_region_event_text = $null" in audit
    assert "-AutomationId 'emergency-stop-status'" in audit
    assert "$liveRegionProbe = [AutosportExternalLiveRegionProbe]::new($stopStatusBefore)" in audit
    assert "$liveRegionProbe.Count -lt 1" in audit
    assert "[string]$liveRegionProbe.LastAutomationId -ne 'emergency-stop-status'" in audit
    assert "[string]$liveRegionProbe.LastName -ne $confirmedStopText" in audit
    assert "$report.live_region_event_status = 'PASS'" in audit
    assert "$liveRegionProbe.Dispose()" in audit


def test_windows_candidate_requires_external_live_region_event_evidence() -> None:
    workflow = _windows_workflow()
    step_start = workflow.index("- name: External UIA fresh-extraction gate")
    step_end = workflow.index("- name: Upload external UIA failure evidence", step_start)
    step = workflow[step_start:step_end]

    assert "$external.live_region_event_status -ne 'PASS'" in step
    assert "[int]$external.live_region_event_count -lt 1" in step
    assert "[string]$external.live_region_event_automation_id -ne 'emergency-stop-status'" in step
    assert "External UIA evidence did not prove packaged LiveRegionChanged observation" in step
    assert "extracted_external_uia_live_region_event_status" in step
    assert "extracted_external_uia_live_region_event_count" in step
    assert "extracted_external_uia_live_region_event_automation_id" in step
    assert "extracted_external_uia_live_region_event_text" in step


def test_external_uia_failure_records_only_bounded_precleanup_process_family_count() -> None:
    """A missing semantic host must retain useful process-lifetime evidence, never PASS."""
    audit = _audit()
    assert "last_observed_process_family_count = 0" in audit
    probe = "$report.last_observed_process_family_count = [int]@($lastFamilyIds).Count"
    assert audit.count(probe) == 1
    finalizer = audit[audit.index("} finally {\n    # Preserve a bounded count"): ]
    assert finalizer.index(probe) < finalizer.index("$process.Refresh()")
    assert finalizer.index(probe) < finalizer.index("Stop-Process")
    assert "Export neither descendant PIDs nor native window names" in finalizer
    assert "launcher_exit_state = 'NOT_OBSERVED'" in audit
    assert "$report.status = 'PASS'" not in finalizer


def test_windows_candidate_bounds_blocking_external_uia_without_false_pass() -> None:
    """A hung third-party UIA provider cannot consume the entire Windows runner."""
    workflow = _windows_workflow()
    start = workflow.index("- name: External UIA fresh-extraction gate")
    end = workflow.index("- name: Upload external UIA failure evidence", start)
    gate = workflow[start:end]

    worker = gate.index("$auditJob = Start-Job -ScriptBlock")
    actual_audit = gate.index("& $auditScript", worker)
    wait = gate.index("$completedAudit = Wait-Job -Job $auditJob -Timeout 180")
    receive = gate.index("Receive-Job -Job $auditJob -ErrorAction Stop")
    evidence = gate.index("$external = Get-Content $evidence -Raw | ConvertFrom-Json")

    assert worker < actual_audit < wait < receive < evidence
    assert "-Exe (Join-Path $packageRoot 'Autosport.exe')" in gate
    assert "-WorkingDirectory $hostileCwd" in gate
    assert "-Workspace $expectedWorkspace" in gate
    assert "if ($null -eq $completedAudit)" in gate
    assert "real accessibility evidence remains unqualified" in gate
    assert "if ($auditJob.State -ne 'Completed')" in gate
    assert "Stop-Job -Job $auditJob" not in gate
    assert "if ($auditJob.State -ne 'Running')" in gate
    assert "Remove-Job -Job $auditJob -Force -ErrorAction SilentlyContinue" in gate
    assert "if (-not (Test-Path -LiteralPath $evidence -PathType Leaf))" in gate
    assert "did not publish independent evidence" in gate
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in gate
    assert "External UIA evidence did not prove packaged durable emergency STOP" in gate
    assert "$external.live_region_event_status -ne 'PASS'" in gate
    assert "$external.keyboard_shortcuts_status -ne 'PASS'" in gate


def test_external_uia_progress_is_fixed_stage_only_not_a_pass_or_user_data() -> None:
    """A blocked UIA provider leaves an atomic, bounded diagnostic, never PASS."""
    audit = _audit()
    assert "[string]$ProgressOutput = ''" in audit
    assert "function Write-ExternalUiaProgress" in audit
    # Compare the actual fixed public stage contract with the watchdog; a
    # new bounded worker probe must be accepted by both or CI fails closed.
    public_contract = audit.split("function Write-ExternalUiaProgress {", 1)[1].split(
        "Write-ExternalUiaProgress -Stage 'START'", 1
    )[0]
    progress_tokens = public_contract.split("[ValidateSet(", 1)[1].split(")]", 1)[0]
    watchdog_tokens = _windows_workflow().split("$allowedStages = @(", 1)[1].split(")", 1)[0]
    progress_stages = tuple(token.strip().strip("'") for token in progress_tokens.split(","))
    watchdog_stages = tuple(token.strip().strip("'") for token in watchdog_tokens.split(","))
    assert progress_stages == watchdog_stages
    assert len(progress_stages) == len(set(progress_stages))
    assert progress_stages[0] == "START"
    assert progress_stages[-1] == "AUDIT_COMPLETE"
    for stage in (
        "SEMANTIC_DESKTOP_POINT_PROBE",
        "SEMANTIC_DESKTOP_POINT_READY",
        "SEMANTIC_DESKTOP_POINT_ID_PROBE",
        "SEMANTIC_DESKTOP_POINT_PARENT_PROBE",
        "SEMANTIC_DESKTOP_POINT_CHILD_PROBE",
    ):
        assert stage in progress_stages
    assert "'SEMANTIC_DESCENDANT_PROBE','SEMANTIC_PRIMARY_DESCENDANT_PROBE','SEMANTIC_CHILD_DESCENDANT_PROBE','SEMANTIC_UIA_OBSERVED'" in audit
    assert "'SEMANTIC_DESCENDANT_PROBE''SEMANTIC_UIA_OBSERVED'" not in audit
    assert "'SEMANTIC_DESCENDANT_PROBE','SEMANTIC_PRIMARY_DESCENDANT_PROBE','SEMANTIC_CHILD_DESCENDANT_PROBE','SEMANTIC_UIA_OBSERVED'" in _windows_workflow()
    assert "'SEMANTIC_DESCENDANT_PROBE''SEMANTIC_UIA_OBSERVED'" not in _windows_workflow()
    assert "[System.IO.File]::WriteAllText($temporary, $Stage, [System.Text.Encoding]::ASCII)" in audit
    assert "[System.IO.File]::Move($temporary, $target, $true)" in audit
    assert "if ([string]::IsNullOrWhiteSpace($ProgressOutput)) { return }" in audit
    stages = (
        "Write-ExternalUiaProgress -Stage 'START'",
        "Write-ExternalUiaProgress -Stage 'UIA_ASSEMBLIES_READY'",
        "Write-ExternalUiaProgress -Stage 'WINDOW_LAUNCHED'",
        "Write-ExternalUiaProgress -Stage 'NATIVE_WINDOW_OBSERVED'",
        "Write-ExternalUiaProgress -Stage 'SEMANTIC_UIA_OBSERVED'",
        "Write-ExternalUiaProgress -Stage 'RUNTIME_WITNESS_OBSERVED'",
        "Write-ExternalUiaProgress -Stage 'AUDIT_COMPLETE'",
    )
    for stage in stages:
        assert audit.count(stage) == 1
    assert audit.index(stages[0]) < audit.index("Add-Type -AssemblyName UIAutomationClient")
    assert audit.index(stages[1]) < audit.index("function Get-ExternalActionPattern")
    assert audit.index(stages[2]) < audit.index("Find-UiaRootForProcessFamily -ProcessIds $lastFamilyIds")
    assert audit.index(stages[3]) < audit.index("Find-UiaRootWithElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '330'")
    assert audit.index(stages[4]) < audit.index("runtime_witness_status = 'PASS'")
    assert audit.index(stages[5]) > audit.index("runtime_witness_status = 'PASS'")
    assert audit.index(stages[6]) > audit.index("$report.status = 'PASS'")
    assert "Write-ExternalUiaProgress -Stage 'PASS'" not in audit


def test_windows_uia_watchdog_records_fresh_allowlisted_stage_and_stays_fail_closed() -> None:
    workflow = _windows_workflow()
    start = workflow.index("- name: External UIA fresh-extraction gate")
    end = workflow.index("- name: Upload external UIA failure evidence", start)
    gate = workflow[start:end]
    assert "if (Test-Path -LiteralPath $stageEvidence) { Remove-Item" in gate
    assert "param($packageRoot, $evidence, $hostileCwd, $expectedWorkspace, $auditScript, $stageEvidence)" in gate
    assert "-ProgressOutput $stageEvidence" in gate
    assert "$completedAudit = Wait-Job -Job $auditJob -Timeout 180" in gate
    assert "$observedStage = 'NOT_EMITTED'" in gate
    assert "$observedStage = 'INVALID'" in gate
    assert "if ($allowedStages -ccontains $candidateStage)" in gate
    assert "throw \"External UIA audit exceeded 180-second watchdog at stage=$observedStage;" in gate
    assert "Receive-Job -Job $auditJob -ErrorAction Stop" in gate
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in gate
    assert "dist/fresh-extraction-external-uia-stage.txt" in workflow
    assert "name: Autosport-external-uia-failure-evidence" in workflow


def test_external_uia_preblocking_progress_identifies_native_fallback_or_search_stage() -> None:
    """Real watchdog failure must identify which provider probe was entered.

    Native emergency/startup fallback remains a hard failure, never a fake UIA
    PASS. The stage artifact contains only fixed ASCII tokens, not UIA names.
    """
    audit = _audit()
    workflow = _windows_workflow()
    start = audit.index("Write-ExternalUiaProgress -Stage 'NATIVE_WINDOW_OBSERVED'")
    name_probe = audit.index("Write-ExternalUiaProgress -Stage 'NATIVE_ROOT_NAME_PROBE'", start)
    local_probe = audit.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_LOCAL_PROBE'", name_probe)
    family_probe = audit.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_FAMILY_PROBE'", local_probe)
    semantic = audit.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_UIA_OBSERVED'", family_probe)
    assert start < name_probe < local_probe < family_probe < semantic
    assert name_probe < audit.index("$initialNativeName = [string]$uiaRoot.Current.Name")
    assert local_probe < audit.index("$uiaRoot.Current.AutomationId", local_probe)
    assert "Find-UiaSemanticViaChildren -Root $uiaRoot" not in audit
    assert family_probe < audit.index("Find-UiaRootWithElementForProcessFamily", family_probe)
    preprobe = audit[name_probe:local_probe]
    assert "NATIVE_EMERGENCY_STOP" in preprobe
    assert "NATIVE_STARTUP_ERROR" in preprobe
    assert "throw 'Packaged native emergency window is not semantic WebView2 readiness'" in preprobe
    assert "throw 'Packaged native startup-error window is not semantic WebView2 readiness'" in preprobe
    assert "$report.status = 'PASS'" not in preprobe
    for stage in ("NATIVE_ROOT_NAME_PROBE", "SEMANTIC_LOCAL_PROBE", "SEMANTIC_FAMILY_PROBE"):
        assert f"Write-ExternalUiaProgress -Stage '{stage}'" in audit
        assert f"'{stage}'" in workflow
    assert "[System.IO.File]::WriteAllText($temporary, $Stage, [System.Text.Encoding]::ASCII)" in audit
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in workflow


def test_external_uia_family_scan_reports_exact_preblocking_stage_without_false_pass() -> None:
    """The actual Windows watchdog must distinguish process discovery, UIA desktop enumeration and provider calls."""
    audit = _audit()
    workflow = _windows_workflow()
    family_start = audit.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_FAMILY_PROBE'")
    ids = audit.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_FAMILY_IDS_READY'", family_start)
    scan = audit.index("Find-UiaRootWithElementForProcessFamily -ProcessIds $lastFamilyIds", ids)
    assert family_start < ids < scan
    fn_start = audit.index("function Find-UiaRootWithElementForProcessFamily {")
    fn_end = audit.index("function Wait-ForUiaElement {", fn_start)
    fn = audit[fn_start:fn_end]
    positions = [fn.index(f"Write-ExternalUiaProgress -Stage '{stage}'") for stage in (
        "SEMANTIC_DESKTOP_ENUM_PROBE",
        "SEMANTIC_DESKTOP_WINDOWS_READY",
        "SEMANTIC_DESKTOP_COUNT_PROBE",
        "SEMANTIC_DESKTOP_ITEM_PROBE",
        "SEMANTIC_WINDOW_PROPERTY_PROBE",
        "SEMANTIC_DESCENDANT_PROBE",
    )]
    assert positions == sorted(positions)
    assert positions[0] < fn.index("$desktop.FindFirst(")
    assert fn.index("$desktop.FindFirst(") < positions[1] < fn.index("$windowCount = $windows.Count")
    assert positions[2] < fn.index("$window.Current.AutomationId")
    assert positions[3] < fn.index("$element = Find-BoundedUiaSemanticViaChildren -Root $window")
    assert "$report.status = 'PASS'" not in fn
    assert "$desktop.FindAll(" not in fn
    assert "$windows = @()" in fn
    assert "if ($null -ne $firstWindow) { $windows = @($firstWindow) }" in fn
    for stage in (
        "SEMANTIC_FAMILY_IDS_READY",
        "SEMANTIC_DESKTOP_ENUM_PROBE",
        "SEMANTIC_DESKTOP_WINDOWS_READY",
        "SEMANTIC_DESKTOP_COUNT_PROBE",
        "SEMANTIC_DESKTOP_ITEM_PROBE",
        "SEMANTIC_WINDOW_PROPERTY_PROBE",
        "SEMANTIC_DESCENDANT_PROBE",
    ):
        assert f"'{stage}'" in workflow
        assert f"Write-ExternalUiaProgress -Stage '{stage}'" in audit
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in workflow



def test_external_uia_scoped_collection_iteration_has_watchdog_visible_boundaries() -> None:
    """A synchronous provider stall cannot be mistaken for semantic UIA success."""
    audit = _audit()
    workflow = _windows_workflow()
    family = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    scan = family.index("$firstWindow = $desktop.FindFirst(")
    ready = family.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_WINDOWS_READY'", scan)
    count = family.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_COUNT_PROBE'", ready)
    get_count = family.index("$windowCount = $windows.Count", count)
    indexed = family.index("for ($windowIndex = 0; $windowIndex -lt $windowCount; $windowIndex++)", get_count)
    item = family.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_ITEM_PROBE'", indexed)
    get_item = family.index("$window = $windows[$windowIndex]", item)
    property_stage = family.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_WINDOW_PROPERTY_PROBE'", get_item)
    property_read = family.index("$window.Current.AutomationId", property_stage)
    children = family.index("Find-BoundedUiaSemanticViaChildren -Root $window", property_read)
    assert scan < ready < count < get_count < indexed < item < get_item < property_stage < property_read < children
    assert "foreach ($window in $windows)" not in family
    assert "[System.Windows.Automation.AutomationElement]::ProcessIdProperty" in family
    assert "$report.status = 'PASS'" not in family
    for stage in ("SEMANTIC_DESKTOP_COUNT_PROBE", "SEMANTIC_DESKTOP_ITEM_PROBE"):
        assert f"'{stage}'" in workflow
        assert f"'{stage}'" in audit.split("function Write-ExternalUiaProgress", 1)[1].split("function", 1)[0]
    assert "$completedAudit = Wait-Job -Job $auditJob -Timeout 180" in workflow
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in workflow


def test_external_uia_distinguishes_primary_from_child_descendant_provider_stalls() -> None:
    """Keep real semantic UIA mandatory while diagnosing which process owns a blocking provider."""
    audit = _audit()
    workflow = _windows_workflow()
    start = audit.index("function Find-UiaRootWithElementForProcessFamily {")
    end = audit.index("function Wait-ForUiaElement {", start)
    helper = audit[start:end]
    assert "[int]$RootProcessId" in helper
    assert "foreach ($candidateProcessId in @($ProcessIds | Sort-Object -Unique))" in helper
    assert "[System.Windows.Automation.AutomationElement]::ProcessIdProperty" in helper
    assert "$candidateProcessId = [int]$window.Current.ProcessId" not in helper
    assert "if ([int]$candidateProcessId -eq $RootProcessId)" in helper
    assert "-AutomationId '330' -RootProcessId $process.Id" in audit
    before_find_first = helper.split("$element = Find-BoundedUiaSemanticViaChildren -Root $window", 1)[0]
    for stage in ("SEMANTIC_PRIMARY_DESCENDANT_PROBE", "SEMANTIC_CHILD_DESCENDANT_PROBE"):
        statement = f"Write-ExternalUiaProgress -Stage '{stage}'"
        assert statement in before_find_first
        assert f"'{stage}'" in workflow
        assert f"'{stage}'" in audit.split("function Write-ExternalUiaProgress", 1)[1].split("function", 1)[0]
    assert "$window.Current.Name" not in helper
    assert "$report.status = 'PASS'" not in helper
    assert "Find-BoundedUiaSemanticViaChildren -Root $window -AutomationId $AutomationId" in helper
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in workflow


def test_external_uia_local_native_probe_never_enters_blocking_unscoped_subtree() -> None:
    """The fresh-extracted Windows audit must rely on real family-scoped semantic UIA."""
    audit = _audit()
    workflow = _windows_workflow()
    start = audit.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_LOCAL_PROBE'")
    root = audit.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_LOCAL_ROOT_ID_PROBE'", start)
    property_read = audit.index("$uiaRoot.Current.AutomationId", root)
    family = audit.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_FAMILY_PROBE'", property_read)
    local = audit[start:family]
    assert start < root < property_read < family
    assert "Find-UiaSemanticViaChildren -Root $uiaRoot" not in audit
    assert "Write-ExternalUiaProgress -Stage 'SEMANTIC_LOCAL_DESCENDANT_PROBE'" not in audit
    assert "Find-UiaRootWithElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '330'" in audit[family:]
    assert "SEMANTIC_LOCAL_ROOT_ID_PROBE" in workflow
    assert "$completedAudit = Wait-Job -Job $auditJob -Timeout 180" in workflow
    assert "$external.status -ne 'PASS'" in workflow
    assert "Write-ExternalUiaProgress -Stage 'PASS'" not in audit

def test_external_uia_semantic_readiness_uses_bounded_child_tree_not_recursive_provider_query() -> None:
    """A blocking recursive UIA provider must not become a false PASS or a 120s unbounded search."""
    audit = _audit()
    helper = audit.split("function Find-UiaSemanticViaChildren {", 1)[1].split(
        "function Find-UiaRootForProcessFamily {", 1
    )[0]
    assert "[System.Collections.Generic.Queue[System.Windows.Automation.AutomationElement]]::new()" in helper
    assert "[System.Windows.Automation.TreeWalker]::RawViewWalker" in helper
    assert "$walker.GetFirstChild($node)" in helper
    assert "$walker.GetNextSibling($child)" in helper
    assert ".FindAll(" not in helper
    assert "if ($visited -ge 4096)" in helper
    assert "$pending.Count -ge 4096" in helper
    assert "[DateTime]::UtcNow -lt $Deadline" in helper
    assert "[System.Windows.Automation.TreeScope]::Descendants" not in helper
    assert "$uiaRoot.FindFirst(" not in audit
    family = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert "$window.FindFirst(" not in family
    assert "Find-BoundedUiaSemanticViaChildren -Root $window" in family
    assert "Find-UiaSemanticViaChildren -Root $uiaRoot" not in audit
    assert "$report.status = 'PASS'" not in helper
    workflow = _windows_workflow()
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in workflow


def test_external_uia_native_hwnd_candidates_preserve_real_semantic_and_process_proof() -> None:
    """Native HWND discovery may bypass a blocked collection, never the UIA gate."""
    audit = _audit()
    workflow = _windows_workflow()
    assert "public static class AutosportNativeWindows" in audit
    assert "GetWindowThreadProcessId(IntPtr window, out uint processId)" in audit
    assert "Array.IndexOf(allowedIds, (int)processId) >= 0" in audit
    assert "if (!EnumWindows(callback, IntPtr.Zero))" in audit
    family = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert family.index("[AutosportNativeWindows]::ForProcessFamily($ProcessIds)") < family.index("$desktop.FindFirst(")
    assert family.index("AutosportNativeWindows]::ProcessIdForHandle($nativeHandle)") < family.index("$desktop.FindFirst(")
    assert "($ProcessIds -contains $nativeProcessId)" in family
    assert "Find-BoundedUiaSemanticViaChildren -AutomationId $AutomationId" in family
    assert "Find-BoundedUiaSemanticViaChildren -AutomationId $AutomationId -Deadline $Deadline -NativeWindowHandle $nativeHandle -ExpectedProcessId $nativeProcessId" in family
    assert "return $nativeSemantic" in family
    assert "$report.status = 'PASS'" not in family
    for stage in ("SEMANTIC_NATIVE_HANDLES_PROBE", "SEMANTIC_NATIVE_UIA_PROBE",
                  "SEMANTIC_NATIVE_DESCENDANT_PROBE"):
        assert f"Write-ExternalUiaProgress -Stage '{stage}'" in family
        assert f"'{stage}'" in workflow


def test_external_uia_native_hwnd_compiler_avoids_generic_forwarder() -> None:
    """Use a bounded HWND array: Roslyn on the Windows runner rejects List<>.

    HWNDs select candidates only. The actual UIA semantic/keyboard/STOP gate
    must remain mandatory and a truncated enumeration must fail closed.
    """
    audit = _audit()
    native = audit.split("public static class AutosportNativeWindows", 1)[1].split(
        "public sealed class AutosportExternalLiveRegionProbe", 1
    )[0]
    assert "new System.Collections.Generic.List<IntPtr>()" not in native
    assert "var windows = new IntPtr[4096];" in native
    assert "if (count >= windows.Length) return false;" in native
    assert "windows[count++] = window;" in native
    assert "if (!EnumWindows(callback, IntPtr.Zero))" in native
    assert "throw new InvalidOperationException(\"Native HWND enumeration failed or exceeded limit\")" in native
    assert "var result = new IntPtr[count];" in native
    assert "Array.Copy(windows, result, count);" in native
    assert "[AutosportNativeWindows]::ForProcessFamily($ProcessIds)" in audit
    assert "AutosportNativeWindows]::ProcessIdForHandle($nativeHandle)" in audit
    assert "return [pscustomobject]@{ Root = $Root; Element = $node }" in audit
    assert "Add-Type -ReferencedAssemblies $uiaReferences -TypeDefinition @'" in audit
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in _windows_workflow()



def test_external_uia_os_child_hwnd_candidates_are_bounded_and_never_replace_semantic_proof() -> None:
    """Child HWNDs prioritize the actual WebView2 provider, not native readiness."""
    audit = _audit()
    native = audit.split("public static class AutosportNativeWindows", 1)[1].split(
        "public sealed class AutosportExternalLiveRegionProbe", 1
    )[0]
    family = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert 'private static extern IntPtr GetWindow(IntPtr window, uint command);' in native
    assert 'private const uint GW_CHILD = 5;' in native
    assert 'private const uint GW_HWNDNEXT = 2;' in native
    assert 'var rootCount = count;' in native
    assert 'if (count >= windows.Length) return false;' in native
    assert 'Array.IndexOf(allowedIds, (int)processId) >= 0' in native
    assert 'EnumChildWindows(' not in native
    assert 'var child = GetWindow(scheduled[cursor], GW_CHILD);' in native
    assert 'child = GetWindow(child, GW_HWNDNEXT);' in native
    assert 'if (++probes > visited.Length)' in native
    assert 'Array.IndexOf(visited, child, 0, visitedCount)' in native
    assert 'visitedCount >= visited.Length || scheduledCount >= scheduled.Length' in native
    assert 'throw new InvalidOperationException("Child HWND probe cap exceeded");' in native
    assert 'throw new InvalidOperationException("Child HWND enumeration limit exceeded");' in native
    assert 'Array.Reverse(result);' in native
    assert 'AutosportNativeWindows]::ProcessIdForHandle($nativeHandle)' in family
    assert '($ProcessIds -contains $nativeProcessId)' in family
    assert 'Find-BoundedUiaSemanticViaChildren -AutomationId $AutomationId' in family
    assert 'Find-BoundedUiaSemanticViaChildren -AutomationId $AutomationId -Deadline $Deadline -NativeWindowHandle $nativeHandle -ExpectedProcessId $nativeProcessId' in family
    assert "SEMANTIC_NATIVE_DESCENDANT_PROBE" in family
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in _windows_workflow()


def test_external_uia_prioritizes_render_hwnds_without_forging_semantic_pass() -> None:
    """The last real packaged audit hung on visible Chrome_WidgetWin_ FromHandle.

    Prefer render-content HWND when available, retain all candidate windows,
    and require genuine external UIA/keyboard/live-region/STOP evidence.
    """
    audit = _audit()
    native = audit.split("public static class AutosportNativeWindows", 1)[1].split(
        "public sealed class AutosportExternalLiveRegionProbe", 1
    )[0]
    family = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert "private static extern bool IsWindowVisible(IntPtr window);" in native
    assert "private static extern int GetClassName(IntPtr window, System.Text.StringBuilder className, int maxCount);" in native
    assert 'name.StartsWith("Chrome_WidgetWin_", StringComparison.Ordinal)' in native
    assert 'name.Equals("Chrome_RenderWidgetHostHWND", StringComparison.Ordinal)' in native
    assert "var priority = new int[count];" in native
    assert 'var renderHost = name.Equals("Chrome_RenderWidgetHostHWND", StringComparison.Ordinal);' in native
    assert 'var chromeWidget = name.StartsWith("Chrome_WidgetWin_", StringComparison.Ordinal);' in native
    assert "priority[i] = renderHost ? 0 : chromeWidget ? (visible ? 1 : 2)" in native
    assert ": (visible ? 3 : 4);" in native
    assert "var ordered = new IntPtr[count];" in native
    assert "for (var bucket = 0; bucket < 5; bucket++)" in native
    assert "if (priority[i] == bucket) ordered[write++] = result[i];" in native
    assert "if (write != count)" in native
    assert 'throw new InvalidOperationException("Native HWND candidate order incomplete")' in native
    assert "return ordered;" in native
    assert "Array.Reverse(result);" in native
    assert "if (count >= windows.Length)" in native
    assert "-ExpectedProcessId $nativeProcessId" in family
    assert "Find-BoundedUiaSemanticViaChildren -AutomationId $AutomationId" in family
    assert "if ($null -ne $nativeSemantic)" in family
    assert "return $nativeSemantic" in family
    assert "$report.status = 'PASS'" not in family


def test_external_uia_native_hwnd_walk_fails_closed_on_cycle_or_unbounded_churn() -> None:
    """Native GetWindow child/sibling discovery must not hang or certify readiness.

    The former recursive EnumChildWindows API exceeded the independent Windows
    watchdog. HWND discovery is only candidate discovery, never UIA PASS.
    """
    audit = _audit()
    native = audit.split("public static class AutosportNativeWindows", 1)[1].split(
        "public sealed class AutosportExternalLiveRegionProbe", 1
    )[0]
    family = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert "EnumChildWindows(" not in native
    assert "var visited = new IntPtr[4096];" in native
    assert "var scheduled = new IntPtr[4096];" in native
    assert "for (var cursor = 0; cursor < scheduledCount; cursor++)" in native
    assert "if (++probes > visited.Length)" in native
    assert "Array.IndexOf(visited, child, 0, visitedCount)" in native
    assert "throw new InvalidOperationException(\"Child HWND probe cap exceeded\")" in native
    assert "throw new InvalidOperationException(\"Child HWND enumeration limit exceeded\")" in native
    assert "Array.Reverse(result);" in native
    assert "[AutosportNativeWindows]::ForProcessFamily($ProcessIds)" in family
    assert "AutosportNativeWindows]::ProcessIdForHandle($nativeHandle)" in family
    assert "Find-BoundedUiaSemanticViaChildren -AutomationId $AutomationId" in family
    assert "Find-BoundedUiaSemanticViaChildren" in family
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in _windows_workflow()


def test_external_uia_provider_child_traversal_is_time_bounded_without_fake_acceptance() -> None:
    """A synchronous Windows UIA provider hang must not consume the global watchdog.

    Keep authentic AutomationId validation in the worker and all downstream
    keyboard, live-region, emergency STOP and native witness gates mandatory.
    """
    audit = _audit()
    workflow = _windows_workflow()
    helper = audit.split("function Find-BoundedUiaSemanticViaChildren {", 1)[1].split(
        "function Find-UiaRootWithElementForProcessFamily {", 1
    )[0]
    family = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert "Start-ThreadJob -ScriptBlock ${function:Find-UiaSemanticViaChildren}" in helper
    assert "$workerCompleted.Wait($budgetSeconds * 1000)" in helper
    assert "[Math]::Min(4, $remainingSeconds)" in helper
    assert "if (-not $workerTerminal)" in helper
    assert "return $null" in helper
    assert "if ($result -isnot [System.Windows.Automation.AutomationElement])" in helper
    assert "Stop-Job -Job $job" not in helper
    assert "$nativeSemantic = Find-BoundedUiaSemanticViaChildren -AutomationId $AutomationId -Deadline $Deadline -NativeWindowHandle $nativeHandle -ExpectedProcessId $nativeProcessId" in family
    assert "$element = Find-BoundedUiaSemanticViaChildren -Root $window" in family
    assert "Find-UiaSemanticViaChildren -Root $nativeRoot" not in family
    assert "Find-UiaSemanticViaChildren -Root $window" not in family
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in workflow
    assert "$completedAudit = Wait-Job -Job $auditJob -Timeout 180" in workflow
    assert "SEMANTIC_NATIVE_DESCENDANT_PROBE" in workflow
    assert "AutomationId -ceq $AutomationId" in audit
    assert "live_region_event_status" in workflow
    assert "emergency_stop_activation_status" in workflow


def test_external_uia_watchdog_does_not_block_on_provider_cleanup() -> None:
    """A blocked UIA COM worker must remain FAILURE without hanging cancellation.

    Windows Candidate 38276832352 ran into the real descendant UIA provider
    and exceeded its watchdog; synchronous Stop-Job after timeout can itself
    wait on that provider. The out-of-process worker is not semantic evidence.
    """
    workflow = _windows_workflow()
    start = workflow.index("- name: External UIA fresh-extraction gate")
    end = workflow.index("- name: Upload external UIA failure evidence", start)
    gate = workflow[start:end]
    assert "$auditJob = Start-Job -ScriptBlock" in gate
    assert "$completedAudit = Wait-Job -Job $auditJob -Timeout 180" in gate
    assert "if ($null -eq $completedAudit)" in gate
    assert "real accessibility evidence remains unqualified" in gate
    assert "Stop-Job -Job $auditJob" not in gate
    assert "if ($auditJob.State -ne 'Running')" in gate
    assert "Remove-Job -Job $auditJob -Force" in gate
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in gate
    assert "$external.live_region_event_status -ne 'PASS'" in gate
    assert "$external.keyboard_shortcuts_status -ne 'PASS'" in gate
    assert "$external.emergency_stop_activation_status -ne 'PASS'" in gate


def test_external_uia_raw_treewalker_sibling_cycle_fails_closed() -> None:
    """A cyclic or huge provider sibling chain must not invent semantic readiness.

    TreeWalker is a bounded alternative to the blocking FindAll(Children) path;
    individual COM calls remain inside the killable/timeout-protected UIA worker.
    """
    audit = _audit()
    helper = audit.split("function Find-UiaSemanticViaChildren {", 1)[1].split(
        "function Find-UiaRootForProcessFamily {", 1
    )[0]
    worker = audit.split("function Find-BoundedUiaSemanticViaChildren {", 1)[1].split(
        "function Find-UiaRootWithElementForProcessFamily {", 1
    )[0]

    assert "$walker = [System.Windows.Automation.TreeWalker]::RawViewWalker" in helper
    assert "$child = $walker.GetFirstChild($node)" in helper
    assert "$child = $walker.GetNextSibling($child)" in helper
    assert "if ($siblingProbes -ge 4096)" in helper
    assert "if ($pending.Count -ge 4096)" in helper
    assert "if ($visited -ge 4096)" in helper
    assert "[DateTime]::UtcNow -lt $Deadline" in helper
    assert "[string]$node.Current.AutomationId -ceq $AutomationId" in helper
    assert ".FindAll(" not in helper
    assert "Start-ThreadJob -ScriptBlock ${function:Find-UiaSemanticViaChildren}" in worker
    assert "$workerCompleted.Wait($budgetSeconds * 1000)" in worker
    assert "if (-not $workerTerminal)" in worker
    assert "if ($result -isnot [System.Windows.Automation.AutomationElement])" in worker
    assert "$report.status = 'PASS'" not in helper
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in _windows_workflow()


def test_external_uia_native_treewalker_uses_same_mta_apartment_and_verified_hwnd_pid() -> None:
    """An unresponsive foreign COM apartment cannot qualify packaged UIA semantics."""
    audit = _audit()
    worker = audit.split("function Find-UiaSemanticViaChildren {", 1)[1].split(
        "function Find-UiaRootForProcessFamily {", 1
    )[0]
    wrapper = audit.split("function Find-BoundedUiaSemanticViaChildren {", 1)[1].split(
        "function Find-UiaRootWithElementForProcessFamily {", 1
    )[0]
    native = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]

    assert "[System.Threading.Thread]::CurrentThread.GetApartmentState()" in worker
    assert "[System.Threading.ApartmentState]::MTA" in worker
    assert "throw 'External UIA semantic worker requires MTA'" in worker
    assert "throw 'External UIA semantic worker requires a verified process'" in worker
    assert "$Root = [System.Windows.Automation.AutomationElement]::FromHandle($NativeWindowHandle)" in worker
    assert "[int]$Root.Current.ProcessId -ne $ExpectedProcessId" in worker
    assert "throw 'External UIA semantic worker window identity changed'" in worker

    assert "$workerRoot = if ($NativeWindowHandle -ne [IntPtr]::Zero) { $null } else { $Root }" in wrapper
    assert "$workerRoot, $AutomationId, $Deadline, $NativeWindowHandle, $ExpectedProcessId" in wrapper
    assert "$workerCompleted.Wait($budgetSeconds * 1000)" in wrapper
    assert "if (-not $workerTerminal)" in wrapper
    assert "Stop-Job -Job $job" not in wrapper
    assert "$nativeProcessId = [AutosportNativeWindows]::ProcessIdForHandle($nativeHandle)" in native
    assert "[int]$nativeRoot.Current.ProcessId" not in native
    assert "-NativeWindowHandle $nativeHandle -ExpectedProcessId $nativeProcessId" in native
    # The observed watchdog never reached the worker START stage. The native
    # path must not bind a COM AutomationElement in the caller before its
    # bounded MTA job is started: only the verified primitive HWND/PID cross.
    assert "Find-BoundedUiaSemanticViaChildren -AutomationId $AutomationId" in native
    assert "Find-BoundedUiaSemanticViaChildren -Root $nativeRoot" not in native
    assert "Find-BoundedUiaSemanticViaChildren -Root $nativeRoot" not in audit
    assert "($ProcessIds -contains $nativeProcessId)" in native
    assert "$report.status = 'PASS'" not in worker + wrapper + native

    for stage in (
        "SEMANTIC_WORKER_START_PROBE",
        "SEMANTIC_WORKER_WAIT_PROBE",
        "SEMANTIC_WORKER_RESULT_PROBE",
    ):
        assert f"Write-ExternalUiaProgress -Stage '{stage}'" in wrapper
        assert f"'{stage}'" in audit.split("function Write-ExternalUiaProgress", 1)[1].split("function", 1)[0]

def test_external_uia_worker_diagnostic_stages_are_accepted_by_windows_watchdog() -> None:
    """Cross-check every real UIA progress token against the fail-closed watchdog.

    Windows candidate 38340243440 masked a genuine UIA stall as stage=INVALID
    because the worker's timeout stage was missing from this workflow.
    """
    import re

    source = _audit()
    workflow = _windows_workflow()
    declaration = source.split("function Write-ExternalUiaProgress {", 1)[1].split(
        "function ", 1
    )[0]
    emitted = set(re.findall(r"Write-ExternalUiaProgress -Stage '([A-Z_]+)'", source))
    # Only ValidateSet declares progress tokens; later fixed diagnostics are
    # not progress tokens even when no PowerShell function separates them.
    assert "[ValidateSet(" in declaration
    validate_set = declaration.split("[ValidateSet(", 1)[1].split(")]", 1)[0]
    declared = set(re.findall(r"'([A-Z_]+)'", validate_set))
    assert "NOT_RUN" not in declared
    allowlist = workflow.split("$allowedStages = @(", 1)[1].split(")", 1)[0]
    accepted = set(re.findall(r"'([A-Z_]+)'", allowlist))
    assert emitted and emitted <= declared
    assert declared == accepted
    assert "SEMANTIC_WORKER_TIMEOUT_PROBE" in emitted
    assert "if ($allowedStages -ccontains $candidateStage)" in workflow
    assert "$observedStage = 'INVALID'" in workflow
    assert "External UIA audit exceeded 180-second watchdog" in workflow

def test_native_uia_com_reads_are_owned_by_bounded_mta_worker() -> None:
    """No UIA COM object may be read during native caller-to-worker admission."""
    audit = _audit()
    native = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split("function Wait-ForUiaElement {", 1)[0]
    worker = audit.split("function Find-UiaSemanticViaChildren {", 1)[1].split("function Find-UiaRootForProcessFamily {", 1)[0]
    wrapper = audit.split("function Find-BoundedUiaSemanticViaChildren {", 1)[1].split("function Find-UiaRootWithElementForProcessFamily {", 1)[0]
    assert "ProcessIdForHandle($nativeHandle)" in native
    assert "FromHandle($nativeHandle)" not in native
    assert "$nativeRoot.Current" not in native
    assert "$nativeSemantic = Find-BoundedUiaSemanticViaChildren" in native
    assert "$Root = [System.Windows.Automation.AutomationElement]::FromHandle($NativeWindowHandle)" in worker
    assert "[int]$Root.Current.ProcessId -ne $ExpectedProcessId" in worker
    assert "[string]$node.Current.AutomationId -ceq $AutomationId" in worker
    assert "return [pscustomobject]@{ Root = $Root; Element = $node }" in worker
    assert "$result.Root -isnot [System.Windows.Automation.AutomationElement]" in wrapper
    assert "$result.Element -isnot [System.Windows.Automation.AutomationElement]" in wrapper
    assert "$workerCompleted.Wait($budgetSeconds * 1000)" in wrapper
    assert "$report.status = 'PASS'" not in worker + wrapper + native

def test_external_uia_monotonic_worker_wait_fails_closed() -> None:
    """The real UIA COM worker cannot defeat the 4-second local wait budget.

    Windows Candidate 38328253786 proved a blocked Wait-Job -Timeout call
    at SEMANTIC_WORKER_WAIT_PROBE. A kernel event must remain independent of the ThreadJob's COM-held
    State lock; a timed-out worker is not semantic acceptance.
    """
    audit = _audit()
    wrapper = audit.split("function Find-BoundedUiaSemanticViaChildren {", 1)[1].split(
        "function Find-UiaRootWithElementForProcessFamily {", 1
    )[0]
    assert "$workerCompleted = [System.Threading.ManualResetEventSlim]::new($false)" in wrapper
    assert "$workerCompleted.Wait($budgetSeconds * 1000)" in wrapper
    assert "$workerResults = [System.Collections.Concurrent.ConcurrentQueue[object]]::new()" in wrapper
    assert "$job.State" not in wrapper
    assert "if (-not $workerTerminal)" in wrapper
    timeout_clause = wrapper.split("if (-not $workerTerminal) {", 1)[1].split("\n    }", 1)[0]
    assert "$script:semanticWorkerTimedOut = $true" in timeout_clause
    assert 'throw "External UIA semantic worker timed out' in timeout_clause
    assert "last_worker_stage=$lastWorkerStage" in timeout_clause
    assert "return $null" not in timeout_clause
    assert "Wait-Job -Job $job" not in wrapper
    assert "Stop-Job -Job $job" not in wrapper
    assert "SEMANTIC_WORKER_WAIT_PROBE" in wrapper
    assert "SEMANTIC_WORKER_RESULT_PROBE" in wrapper
    assert "$report.status = 'PASS'" not in wrapper


def test_stalled_uia_worker_aborts_provider_reentry_and_cleanup_probe() -> None:
    """A COM timeout must fail closed and serialize evidence without provider reentry."""
    audit = _audit()
    wrapper = audit.split("function Find-BoundedUiaSemanticViaChildren {", 1)[1].split(
        "function Find-UiaRootWithElementForProcessFamily {", 1
    )[0]
    timeout = wrapper.split("if (-not $workerTerminal) {", 1)[1].split("\n    }", 1)[0]
    assert "SEMANTIC_WORKER_TIMEOUT_PROBE" in timeout
    assert "$script:semanticWorkerTimedOut = $true" in timeout
    assert 'throw "External UIA semantic worker timed out' in timeout
    assert "last_worker_stage=$lastWorkerStage" in timeout
    assert "Stop-Job -Job" not in timeout and "Remove-Job -Job" not in timeout
    native = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert "catch [System.Windows.Automation.ElementNotAvailableException]" in native
    assert "catch {" not in native.split("foreach ($nativeHandle in $nativeWindows)", 1)[1].split(
        "# The existing UIA desktop search", 1
    )[0]
    cleanup = audit.split("} finally {\n    # Preserve a bounded count", 1)[1]
    assert "Never reenter the UIA COM provider during failure cleanup" in cleanup
    assert "$report.observed_window_class =" not in cleanup
    assert not any(
        "$uiaRoot.Current.Name" in line or "AutomationElement]::" in line
        for line in cleanup.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    assert "$report.semantic_desktop_worker_last_stage" in cleanup
    assert "$report | ConvertTo-Json -Depth 8 | Set-Content" in cleanup
    assert "if ($report.status -ne 'PASS')" in audit
    assert "exit 1" in audit

def test_external_uia_watchdog_distinguishes_native_enumeration_from_desktop_com() -> None:
    """The 120s failure must identify the exact boundary, not grant UIA PASS.

    A stall reported as NATIVE_HANDLES_PROBE used to also cover a desktop COM
    RootElement read when the native HWND list was empty. These probes cannot
    replace the real AutomationId, keyboard, live-region, or STOP requirements.
    """
    audit = _audit()
    workflow = _windows_workflow()
    native = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    for token in (
        "SEMANTIC_NATIVE_HANDLES_PROBE", "SEMANTIC_NATIVE_HANDLES_READY",
        "SEMANTIC_NATIVE_CANDIDATES_EXHAUSTED", "SEMANTIC_DESKTOP_ROOT_PROBE",
        "SEMANTIC_DESKTOP_ROOT_READY",
    ):
        assert f"Write-ExternalUiaProgress -Stage '{token}'" in native
        assert f"'{token}'" in workflow
    assert native.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_NATIVE_HANDLES_PROBE'") < native.index(
        "$nativeWindows = [AutosportNativeWindows]::ForProcessFamily($ProcessIds)"
    ) < native.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_NATIVE_HANDLES_READY'")
    assert native.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_NATIVE_HANDLES_READY'") < native.index(
        "foreach ($nativeHandle in $nativeWindows)"
    ) < native.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_NATIVE_CANDIDATES_EXHAUSTED'")
    assert native.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_ROOT_PROBE'") < native.index(
        "$desktop = [System.Windows.Automation.AutomationElement]::RootElement"
    ) < native.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_ROOT_READY'")
    assert native.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_ROOT_READY'") < native.index(
        "Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_ENUM_PROBE'"
    )
    assert "$report.status = 'PASS'" not in native
    assert "$completedAudit = Wait-Job -Job $auditJob -Timeout 180" in workflow


def test_actual_desktop_uia_provider_is_bounded_inside_real_mta_worker() -> None:
    """CI 38810732331 proved desktop COM can block; timeout is never PASS."""
    source = _audit()
    workflow = _windows_workflow()
    helper = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert "Start-ThreadJob -ScriptBlock {" in helper
    assert "External desktop UIA semantic worker requires MTA" in helper
    assert "ManualResetEventSlim]::new($false)" in helper
    assert "$state.Completion.Wait(0)" in helper
    assert "[Math]::Min(4, $remainingSeconds)" in helper
    assert "foreach ($workerMode in @('ROOT', 'POINT', 'FOCUS', 'RENDERER'))" in helper
    assert "if ($unfinished.Count -gt 0)" in helper
    assert "$script:semanticWorkerTimedOut = $true" in helper
    assert "last_desktop_stage=$lastWorkerStage" in helper
    assert "Stop-Job -Job $desktopJob" not in helper
    assert "Remove-Job -Job $desktopJob" not in helper
    assert "Find-BoundedUiaSemanticViaChildren -Root $window" in helper
    assert "$ResultQueue.Enqueue([object](New-BoundedUiaPair" in helper
    assert "ProcessIdProperty" in helper
    assert "SEMANTIC_DESKTOP_WORKER_TIMEOUT_PROBE" in workflow
    assert "if ($null -eq $external -or $external.status -ne 'PASS')" in workflow
    assert "$report.status = 'PASS'" not in helper

def test_desktop_uia_fallback_binds_actual_rawview_search_function() -> None:
    """Desktop fallback must pass the real RawView search, never a placeholder.

    This checks source binding only; packaged external UIA remains a hard gate.
    """
    source = _audit()
    fallback = source.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    assert "__SEARCH_FUNCTION__" not in source
    assert source.count("$childSearchScript = \u0024{function:Find-UiaSemanticViaChildren}") == 1
    assert "function Find-UiaSemanticViaChildren {" in source
    assert "[scriptblock]$ChildSearchScript" in fallback
    assert "& $ChildSearchScript -Root $Root -AutomationId $AutomationId -Deadline $Deadline" in fallback
    assert "Start-ThreadJob -ScriptBlock {" in fallback
    assert "$script:semanticWorkerTimedOut = $true" in fallback
    assert "$ResultQueue.Enqueue([object](New-BoundedUiaPair" in fallback


def test_external_uia_chrome_wrapper_defers_to_real_desktop_provider() -> None:
    """A proven Chrome_WidgetWin_ FromHandle hang must not preempt real fallback.

    Win32 window class is selection only. The independent desktop MTA worker
    still requires an actual provider AutomationId/PID and preserves FAIL.
    """
    audit = _audit()
    family = audit.split("function Find-UiaRootWithElementForProcessFamily {", 1)[1].split(
        "function Wait-ForUiaElement {", 1
    )[0]
    wrapper = "if ($nativeBucket -in @('CHROME_WIDGET_VISIBLE', 'CHROME_WIDGET_HIDDEN')) {"
    other = "if ($nativeBucket -in @('OTHER_VISIBLE', 'OTHER_HIDDEN', 'UNKNOWN')) {"
    assert "$nativeBucket = [AutosportNativeWindows]::DiagnosticClassBucket($nativeHandle)" in family
    assert wrapper in family and other in family
    native_probe = "Find-BoundedUiaSemanticViaChildren -AutomationId $AutomationId"
    assert family.index(wrapper) < family.index(other) < family.index(native_probe)
    assert family[family.index(other):family.index(native_probe)].count("continue") == 1
    assert "RENDER_WIDGET_VISIBLE" not in (wrapper + other)
    assert "RENDER_WIDGET_HIDDEN" not in (wrapper + other)
    assert "SEMANTIC_NATIVE_CANDIDATES_EXHAUSTED" in family
    assert "SEMANTIC_DESKTOP_WORKER_START_PROBE" in family
    assert "$desktop = [System.Windows.Automation.AutomationElement]::RootElement" in family
    assert "[System.Windows.Automation.AutomationElement]::ProcessIdProperty" in family
    assert "[string]$window.Current.AutomationId -eq $AutomationId" in family
    assert "$ResultQueue.Enqueue" in family
    assert "$script:semanticWorkerTimedOut = $true" in family
    assert "$report.status = 'PASS'" not in family


def test_external_uia_report_has_no_raw_exception_text_on_webview2_lineage() -> None:
    """No untrusted UIA/COM exception details in disk or console evidence."""
    audit = _audit()
    assert "Exception.Message" not in audit
    assert "Exception.GetType().Name" not in audit
    for expected in (
        "packaged keyboard shortcut audit failed: focus evidence unavailable",
        "owner economic panel open failed: activation evidence unavailable",
        "manual calculation panel open failed: activation evidence unavailable",
        "packaged emergency STOP activation audit failed: durable proof unavailable",
        "packaged disclosure focus-handoff audit failed: focus evidence unavailable",
        "external UIA audit failed: unexpected automation error",
    ):
        assert expected in audit
    assert "$report | ConvertTo-Json -Depth 8 | Set-Content" in audit
    assert "Write-Host ($report | ConvertTo-Json -Depth 8)" in audit


def test_external_uia_critical_identity_scan_is_bounded_and_rejects_duplicates() -> None:
    """The real WebView2 UIA witness may not hide duplicate critical IDs."""
    audit = _audit()
    start = audit.index("    $criticalIds = [System.Collections.Generic.HashSet[string]]")
    end = audit.index("    foreach ($spec in $expected) {", audit.index(
        "external UIA critical identity scan incomplete", start
    ))
    probe = audit[start:end]
    assert "$descendants = $uiaRoot.FindAll(" in audit[:start]
    assert "$descendants.Count -gt 4096" in probe
    assert "$criticalIds.Contains($candidateId)" in probe
    assert "$criticalCounts.ContainsKey($candidateId)" in probe
    assert "$criticalCounts[$expectedId] -gt 1" in probe
    assert "duplicate external UIA control identity" in probe
    assert "external UIA critical identity scan incomplete" in probe
    assert "if (-not ($lastFamilyIds -contains [int]$candidate.Current.ProcessId))" in probe
    assert "$report.failures += 'external UIA critical identity scan exceeded bounded collection limit'" in probe
    # No second full-desktop tree walk to defeat the WebView2 COM watchdog.
    assert probe.count("FindAll(") == 0

def test_duplicate_process_census_is_independently_bounded_and_fail_closed() -> None:
    """A stuck CIM census must not block the real duplicate-dialog UIA gate."""
    audit = _audit()
    workflow = _windows_workflow()
    start = audit.index("function Get-BoundedDuplicateProcessFamilyIds {")
    end = audit.index("# Traverse one UIA children level per provider call.", start)
    helper = audit[start:end]
    duplicate_start = audit.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_PROBE'")
    duplicate_end = audit.index("$report.duplicate_launch_status = 'PASS'", duplicate_start)
    duplicate = audit[duplicate_start:duplicate_end]

    assert "$familyFinder = ${function:Get-ProcessFamilyIds}" in helper
    assert "$worker = Start-ThreadJob -ScriptBlock" in helper
    assert "$completed = [System.Threading.ManualResetEventSlim]::new($false)" in helper
    assert "$completed.Wait(6000)" in helper
    assert "$results.TryDequeue([ref]$observedFamily)" in helper
    assert "$familyIds -contains $RootProcessId" in helper
    assert "throw 'Second packaged launch bounded process-family probe timed out'" in helper
    assert "Stop-Job" in helper and "Stop-Job -Job" not in helper
    assert "Get-BoundedDuplicateProcessFamilyIds -RootProcessId $duplicateProcess.Id" in duplicate
    assert "Get-ProcessFamilyIds -RootProcessId $duplicateProcess.Id" not in duplicate
    for stage in (
        "DUPLICATE_START_PROCESS_PROBE",
        "DUPLICATE_START_PROCESS_READY",
        "DUPLICATE_PROCESS_FAMILY_PROBE",
        "DUPLICATE_PROCESS_FAMILY_READY",
    ):
        assert f"Write-ExternalUiaProgress -Stage '{stage}'" in audit
        assert f"'{stage}'" in workflow
    assert duplicate.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_PROBE'") < duplicate.index("$duplicateProcess = [System.Diagnostics.Process]::new()")
    assert duplicate.index("Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_READY'") < duplicate.index("DUPLICATE_PROCESS_FAMILY_PROBE")
    assert duplicate.index("DUPLICATE_PROCESS_FAMILY_PROBE") < duplicate.index("Get-BoundedDuplicateProcessFamilyIds")
    assert duplicate.index("DUPLICATE_PROCESS_FAMILY_READY") < duplicate.index("DUPLICATE_UIA_ROOT_PROBE")
    assert "-ProcessIds $duplicateFamilyIds" in duplicate
    assert "-WindowHandles ([IntPtr[]]$duplicateNativeHandles)" in duplicate
    assert "$duplicateProbe -ceq 'DIALOG_CLOSED'" in duplicate
    assert "$duplicateProcess.ExitCode -ne 2" in duplicate
    assert "'SECOND_SEMANTIC_SURFACE'" in audit



def test_duplicate_gui_launch_precedes_every_primary_uia_provider_read() -> None:
    """Real Windows launch controls require a fresh client before duplicate exit2."""
    audit = _audit()
    primary = audit.index("$process = Start-Process -FilePath $exePath")
    duplicate_pass = audit.index("$report.duplicate_launch_status = 'PASS'", primary)
    before_duplicate = audit[primary:duplicate_pass]
    assert "[AutosportNativeWindows]::VisibleTopLevelWindowsForProcess(" in before_duplicate
    assert "if (-not $startupNativeObserved)" in before_duplicate
    assert "Packaged WebView2 runtime witness unavailable before semantic UIA probe" in before_duplicate
    assert "Find-UiaRootForProcessFamily -ProcessIds $lastFamilyIds" not in before_duplicate
    assert "$uiaRoot.Current" not in before_duplicate
    assert "$startInfo.UseShellExecute = $true" in before_duplicate
    assert "$duplicateStartCompleted.Wait(6000)" in before_duplicate
    assert "$duplicateProcess.ExitCode -ne 2" in before_duplicate
    after_duplicate = audit[duplicate_pass:]
    assert "$uiaRoot = Find-UiaRootForProcessFamily -ProcessIds $lastFamilyIds" in after_duplicate
    assert "$initialNativeName = [string]$uiaRoot.Current.Name" in after_duplicate
    assert "NATIVE_STARTUP_ERROR" in after_duplicate and "NATIVE_EMERGENCY_STOP" in after_duplicate
    assert "SEMANTIC_UIA_OBSERVED" in after_duplicate


def test_native_timeout_stage_and_primary_stack_remain_failure_only() -> None:
    audit = _audit()
    assert "$script:lastNativeWorkerStage = 'NOT_RUN'" in audit
    assert "$lastWorkerStage -cin $allowedNativeStages" in audit
    assert "$report.native_semantic_worker_last_stage = $script:lastNativeWorkerStage" in audit
    failure = audit.split("} catch {\n    if ($script:lastDesktopWorkerStage", 1)[1].split("} finally {", 1)[0]
    assert "$null -ne $process -and $script:semanticWorkerTimedOut" in failure
    assert "foreach ($familyId in $lastFamilyIds)" in failure
    assert "[System.IO.Path]::GetFileNameWithoutExtension($exePath)" in failure
    assert "$report.primary_python_stacks = Get-DuplicatePythonStackDiagnostics" in failure
    assert "$report.status = 'PASS'" not in failure
