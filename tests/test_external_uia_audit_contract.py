from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_EXTERNAL_UIA_AUDIT = _ROOT / "scripts" / "external_uia_audit.ps1"
_WINDOWS_WORKFLOW = _ROOT / ".github" / "workflows" / "windows-build.yml"


def _audit() -> str:
    return _EXTERNAL_UIA_AUDIT.read_text(encoding="utf-8")


def _windows_workflow() -> str:
    return _WINDOWS_WORKFLOW.read_text(encoding="utf-8")


def test_external_uia_audit_loads_required_automation_assemblies() -> None:
    audit = _audit()

    assert "Add-Type -AssemblyName UIAutomationClient" in audit
    assert "Add-Type -AssemblyName UIAutomationTypes" in audit
    assert "Add-Type -AssemblyName System.Windows.Forms" in audit


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
    second_launch = (
        "$duplicateProcess = Start-Process -FilePath $exePath "
        "-WorkingDirectory $launchWorkingDirectory -PassThru"
    )
    assert first_launch in audit
    assert second_launch in audit
    assert audit.index(first_launch) < audit.index(second_launch)
    assert "duplicate_launch_status = 'NOT_RUN'" in audit
    assert "$report.duplicate_launch_status = 'PASS'" in audit
    assert "$report.duplicate_launch_exit_code = [int]$duplicateProcess.ExitCode" in audit
    assert "$duplicateProcess.ExitCode -ne 2" in audit
    assert "Second packaged launch exposed a second semantic WebView operator surface" in audit
    assert "уже відкритий" in audit
    assert "Economic і live state не змінено" in audit


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
    assert "Wait-ForFocusedAutomationId -AutomationId '301'" in audit
    assert "Wait-ForFocusedAutomationId -AutomationId '204'" in audit
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
    assert "Wait-ForFocusedAutomationId -AutomationId '305'" in audit
    assert "owner disclosure close did not restore focus to automation_id=305" in audit

    assert "-AutomationId '336'" in audit
    assert "Invoke-ExternalAction -Element $manualClose" in audit
    assert "Wait-ForFocusedAutomationId -AutomationId '330'" in audit
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
    assert "Wait-ForFocusedAutomationId -AutomationId 'emergency-stop-status'" in audit
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
