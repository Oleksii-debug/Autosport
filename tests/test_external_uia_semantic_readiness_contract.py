from pathlib import Path


_AUDIT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "external_uia_audit.ps1"


def _audit_script_text() -> str:
    return _AUDIT_SCRIPT.read_text(encoding="utf-8")


def test_external_uia_waits_for_webview_semantics_before_panel_activation_and_snapshot() -> None:
    script = _audit_script_text()

    startup_deadline = "$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)"
    semantic_state = "$semanticReady = $null"
    semantic_probe = (
        "$semanticSurface = Find-UiaRootWithElementForProcessFamily "
        "-ProcessIds $lastFamilyIds -AutomationId '330'"
    )
    root_binding = "$uiaRoot = $semanticSurface.Root"
    element_binding = "$semanticReady = $semanticSurface.Element"
    semantic_timeout = (
        'throw "Timed out waiting for WebView2 semantic UIA readiness '
        '(automation_id=330) across packaged process family"'
    )
    owner_open = "$ownerOpen = Find-UiaElementForProcessFamily"
    manual_open = "$manualOpen = $semanticReady"
    tree_snapshot = "$descendants = $uiaRoot.FindAll("

    assert script.count(startup_deadline) == 1
    assert (
        script.index(startup_deadline)
        < script.index(semantic_state)
        < script.index(semantic_probe)
        < script.index(root_binding)
        < script.index(element_binding)
        < script.index(semantic_timeout)
        < script.index(owner_open)
        < script.index(manual_open)
        < script.index(tree_snapshot)
    )


def test_external_uia_semantic_readiness_reuses_bounded_startup_deadline() -> None:
    script = _audit_script_text()

    semantic_state_index = script.index("$semanticReady = $null")
    semantic_timeout_index = script.index(
        'throw "Timed out waiting for WebView2 semantic UIA readiness '
    )
    semantic_block = script[semantic_state_index:semantic_timeout_index]

    assert "while ([DateTime]::UtcNow -lt $deadline)" in semantic_block
    assert "Get-ProcessFamilyIds -RootProcessId $process.Id" in semantic_block
    assert "Find-UiaRootWithElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '330'" in semantic_block
    assert "$uiaRoot = $semanticSurface.Root" in semantic_block
    assert "$semanticReady = $semanticSurface.Element" in semantic_block
    assert "Find-UiaRootForProcessFamily -ProcessIds $lastFamilyIds" not in semantic_block
    assert "Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '330'" not in semantic_block
    assert "Start-Sleep -Milliseconds 100" in semantic_block
    assert "AddSeconds(" not in semantic_block


def test_external_uia_semantic_surface_returns_one_root_element_pair() -> None:
    script = _audit_script_text()

    helper_start = script.index("function Find-UiaRootWithElementForProcessFamily")
    report_start = script.index("$report = [ordered]@{")
    helper = script[helper_start:report_start]

    assert "[System.Windows.Automation.TreeScope]::Children" in helper
    assert "if (-not ($ProcessIds -contains [int]$window.Current.ProcessId)) { continue }" in helper
    assert "return [pscustomobject]@{ Root = $window; Element = $window }" in helper
    assert "return [pscustomobject]@{ Root = $window; Element = $element }" in helper
    assert "[System.Windows.Automation.TreeScope]::Descendants" in helper


def test_external_uia_contract_matches_current_semantic_webview_copy_and_types() -> None:
    script = _audit_script_text()

    for current_name in (
        "Вибрати та перевірити набір даних",
        "Запустити симуляційний повтор",
        "Режим живих даних",
        "Оновити поточні котирування",
    ):
        assert current_name in script

    for stale_name in (
        "Вибрати набір даних для повтору",
        "Запустити паперовий повтор",
        "Режим живого спостереження",
        "Оновити поточний знімок",
    ):
        assert stale_name not in script

    ticket_spec = next(
        line for line in script.splitlines() if "automation_id = '201'" in line
    )
    assert "expected_control_type = 'ControlType.Table'" in ticket_spec
    assert "ControlType.DataItem" in ticket_spec
    assert "require_named_rows" not in script


def test_external_uia_button_activation_is_real_and_not_focus_only() -> None:
    script = _audit_script_text()

    helper_start = script.index("function Get-ExternalActionPattern")
    helper_end = script.index("function Get-SemanticChildStats")
    helper = script[helper_start:helper_end]

    assert "[System.Windows.Automation.InvokePattern]::Pattern" in helper
    assert "[System.Windows.Automation.LegacyIAccessiblePattern]::Pattern" in helper
    assert "$legacy.Current.DefaultAction" in helper
    assert ".DoDefaultAction()" in helper
    assert "required_pattern = 'Action'" in script
    assert "return $null -ne (Get-ExternalActionPattern -Element $Element)" in helper


def test_external_uia_opens_hidden_owner_and_manual_panels_before_auditing_children() -> None:
    script = _audit_script_text()

    owner_find = "-AutomationId '305'"
    owner_activate = "Invoke-ExternalAction -Element $ownerOpen"
    owner_wait = "-AutomationId '306' -Deadline $ownerDeadline"
    manual_activate = "Invoke-ExternalAction -Element $manualOpen"
    manual_wait = "-AutomationId '331' -Deadline $manualDeadline"
    audit_loop = "foreach ($spec in $expected)"

    assert (
        script.index(owner_find)
        < script.index(owner_activate)
        < script.index(owner_wait)
        < script.index(manual_activate)
        < script.index(manual_wait)
        < script.index(audit_loop)
    )


def test_external_uia_allows_empty_startup_collections_but_rejects_unnamed_items() -> None:
    script = _audit_script_text()

    assert "function Get-SemanticChildStats" in script
    assert "if ($childStats.UnnamedCount -gt 0)" in script
    assert "unnamed semantic collection items" in script
    assert "CandidateCount = 0; NamedCount = 0; UnnamedCount = 0" in script
    assert "namedRowCount -lt 1" not in script
