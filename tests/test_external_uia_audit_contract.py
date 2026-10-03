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
