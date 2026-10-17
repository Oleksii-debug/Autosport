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



def test_external_uia_avoids_early_unready_webview2_provider_probe() -> None:
    script = _audit_script_text()

    witness_wait = script.index("$preSemanticWitness = Join-Path")
    semantic_poll = script.index("$semanticReady = $null")
    semantic_provider_call = script.index(
        "Find-UiaRootWithElementForProcessFamily -ProcessIds $lastFamilyIds",
        semantic_poll,
    )
    guard = script[witness_wait:semantic_poll]

    assert witness_wait < semantic_poll < semantic_provider_call
    assert "webview2-runtime-witness.json" in guard
    assert "Test-Path -LiteralPath $preSemanticWitness -PathType Leaf" in guard
    assert "[DateTime]::UtcNow -lt $deadline" in guard
    assert "Start-Sleep -Milliseconds 100" in guard
    assert "runtime witness unavailable before semantic UIA probe" in guard
    assert "if ($null -ne $semanticReady) { break }" in script
    assert "Timed out waiting for WebView2 semantic UIA readiness" in script


def test_external_uia_semantic_readiness_reuses_bounded_startup_deadline() -> None:
    script = _audit_script_text()

    semantic_state_index = script.index("$semanticReady = $null")
    semantic_timeout_index = script.index(
        'throw "Timed out waiting for WebView2 semantic UIA readiness '
    )
    semantic_block = script[semantic_state_index:semantic_timeout_index]

    assert "while ([DateTime]::UtcNow -lt $deadline)" in semantic_block
    direct_probe = "if ([string]$uiaRoot.Current.AutomationId -eq '330') {"
    family_probe = "Get-ProcessFamilyIds -RootProcessId $process.Id"
    assert direct_probe in semantic_block
    assert "Find-UiaSemanticViaChildren -Root $uiaRoot" not in semantic_block
    assert family_probe in semantic_block
    assert semantic_block.index(direct_probe) < semantic_block.index(family_probe)
    assert "$nextFamilyRefresh = [DateTime]::UtcNow.AddMilliseconds(1000)" in semantic_block
    assert "if ($now -ge $nextFamilyRefresh)" in semantic_block
    assert "$nextFamilyRefresh = $now.AddMilliseconds(1000)" in semantic_block
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
    # The live Windows candidate blocked while reading a foreign desktop
    # UIA window's ProcessId. The process-family match must be performed by
    # a real UIA ProcessIdProperty condition before window property reads.
    assert "[System.Windows.Automation.AutomationElement]::ProcessIdProperty" in helper
    assert "$processCondition = New-Object System.Windows.Automation.PropertyCondition(" in helper
    assert "$firstWindow = $desktop.FindFirst(" in helper
    assert "$desktop.FindAll(" not in helper
    assert "$windows = @()" in helper
    assert "if ($null -ne $firstWindow) { $windows = @($firstWindow) }" in helper
    assert "                $processCondition\n" in helper
    assert "$candidateProcessId = [int]$window.Current.ProcessId" not in helper
    assert "[System.Windows.Automation.Condition]::TrueCondition" not in helper
    assert "foreach ($candidateProcessId in @($ProcessIds | Sort-Object -Unique))" in helper
    assert "if ([DateTime]::UtcNow -ge $Deadline) { break }" in helper
    assert "if ([int]$candidateProcessId -le 0) { continue }" in helper
    assert "return [pscustomobject]@{ Root = $window; Element = $window }" in helper
    assert "return [pscustomobject]@{ Root = $window; Element = $element }" in helper
    assert "Find-BoundedUiaSemanticViaChildren -Root $window -AutomationId $AutomationId" in helper


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
    assert "[System.Windows.Automation.AutomationPattern]::LookupById(10018)" in helper
    assert "[System.Windows.Automation.LegacyIAccessiblePattern]" not in helper
    assert "$legacyObject.Current.DefaultAction" in helper
    assert "$action.Pattern.DoDefaultAction()" in helper
    assert "Kind = 'KeyboardButton'" in helper
    assert "$Element.SetFocus()" in helper
    assert "[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')" in helper
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


def test_external_uia_native_pid_is_single_read_before_bounded_worker() -> None:
    """Regression: a second provider COM read blocked outside the worker watchdog."""
    script = _audit_script_text()
    start = script.index("function Find-UiaRootWithElementForProcessFamily")
    end = script.index("$report = [ordered]@{", start)
    helper = script[start:end]
    native_start = helper.index("foreach ($nativeHandle in $nativeWindows)")
    fallback_start = helper.index("Write-ExternalUiaProgress -Stage 'SEMANTIC_NATIVE_CANDIDATES_EXHAUSTED'", native_start)
    native = helper[native_start:fallback_start]

    # Read the candidate PID once from Win32; do not bind a COM UIA root
    # on the caller runspace before starting the bounded MTA worker.
    assert native.count("[AutosportNativeWindows]::ProcessIdForHandle($nativeHandle)") == 1
    assert "$nativeProcessId = [AutosportNativeWindows]::ProcessIdForHandle($nativeHandle)" in native
    assert "$nativeRoot" not in native
    assert "if (-not ($ProcessIds -contains $nativeProcessId)) { continue }" in native
    assert "Write-ExternalUiaProgress -Stage 'SEMANTIC_NATIVE_DESCENDANT_PROBE'" in native
    assert "-NativeWindowHandle $nativeHandle -ExpectedProcessId $nativeProcessId" in native
    assert "Find-BoundedUiaSemanticViaChildren" in native

    # Verify the real MTA UIA worker still rechecks the HWND/PID pair and
    # requires a semantic AutomationId rather than treating native HWND as PASS.
    worker = script.split("function Find-UiaSemanticViaChildren {", 1)[1].split(
        "function Find-BoundedUiaSemanticViaChildren {", 1
    )[0]
    assert "$Root = [System.Windows.Automation.AutomationElement]::FromHandle($NativeWindowHandle)" in worker
    assert "[int]$Root.Current.ProcessId -ne $ExpectedProcessId" in worker
    assert "throw 'External UIA semantic worker window identity changed'" in worker
    assert "[string]$node.Current.AutomationId -ceq $AutomationId" in worker
    assert "$report.status = 'PASS'" not in native + worker



def test_native_semantic_helper_never_binds_com_root_before_mta_job() -> None:
    """A typed UIA Root parameter can enter provider COM before worker timeout."""
    script = _audit_script_text()
    start = script.index("function Find-BoundedUiaSemanticViaChildren")
    end = script.index("function Find-UiaRootWithElementForProcessFamily", start)
    helper = script[start:end]

    # Native candidates supply only HWND/PID. Explicit validation preserves
    # fail-closed behavior for the desktop-fallback genuine UIA root.
    assert "[object]$Root," in helper
    assert "[System.Windows.Automation.AutomationElement]$Root," not in helper
    assert "$Root -isnot [System.Windows.Automation.AutomationElement]" in helper
    assert "throw 'External UIA semantic root is not an AutomationElement'" in helper
    assert "$workerRoot = if ($NativeWindowHandle -ne [IntPtr]::Zero) { $null } else { $Root }" in helper
    assert "Write-ExternalUiaProgress -Stage 'SEMANTIC_WORKER_START_PROBE'" in helper
    assert "$job = Start-ThreadJob" in helper
    # Neither Wait-Job nor Job.State is a reliable bounded wait when UIA COM
    # holds ThreadJob locks; the shared completion event must own timeout.
    assert "Wait-Job -Job $job -Timeout $budgetSeconds" not in helper
    assert "$workerCompleted = [System.Threading.ManualResetEventSlim]::new($false)" in helper
    assert "$workerCompleted.Wait($budgetSeconds * 1000)" in helper
    assert "$job.State" not in helper
    assert "$workerResults = [System.Collections.Concurrent.ConcurrentQueue[object]]::new()" in helper
    assert "if (-not $workerTerminal)" in helper
    timeout_path = helper.split("if (-not $workerTerminal) {", 1)[1].split("\n    }", 1)[0]
    assert "$script:semanticWorkerTimedOut = $true" in timeout_path
    assert 'throw "External UIA semantic worker timed out' in timeout_path
    assert "last_worker_stage=$lastWorkerStage" in timeout_path
    assert "return $null" not in timeout_path
    assert "Receive-Job" not in timeout_path
    assert "Remove-Job -Job" not in timeout_path
    assert "Write-ExternalUiaProgress -Stage 'SEMANTIC_WORKER_RESULT_PROBE'" in helper
    assert "$workerResults.TryDequeue([ref]$result)" in helper
    assert "return $null" in helper
    assert "if ($result -isnot [System.Windows.Automation.AutomationElement])" in helper


def test_external_uia_windows_focus_ancestry_is_real_scoped_fallback_not_a_fake_pass() -> None:
    """Exact Windows gate stalled in desktop FindAll AND FindFirst; no bypass allowed."""
    script = _audit_script_text()
    start = script.index("function Find-UiaRootWithElementForProcessFamily")
    end = script.index("function Wait-ForUiaElement", start)
    helper = script[start:end]
    focus = helper.split("Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_FOCUS_PROBE'", 1)[1].split(
        "Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_ROOT_PROBE'", 1
    )[0]
    assert "[System.Windows.Automation.AutomationElement]::FocusedElement" in focus
    assert "$ProcessIds -contains $focusedPid" in focus
    assert "[System.Windows.Automation.TreeWalker]::RawViewWalker" in focus
    assert "$ancestorHops -lt 64" in focus
    assert "$ProcessIds -contains [int]$ancestor.Current.ProcessId" in focus
    assert "Find-BoundedUiaSemanticViaChildren -Root $focusedRoot" in focus
    assert "[string]$focusSemantic.Current.AutomationId -cne $AutomationId" in focus
    assert "$ProcessIds -contains [int]$focusSemantic.Current.ProcessId" in focus
    assert "New-BoundedUiaPair -Window $focusedRoot -Element $focusSemantic" in focus
    assert "SendKeys" not in focus
    assert "return [pscustomobject]@{ status = 'PASS' }" not in focus


def test_external_uia_native_bucket_diagnostics_have_no_identifiers_or_pass_authority() -> None:
    script = _audit_script_text()
    begin = script.index("$bucketCounts = [ordered]@{")
    end = script.index("$script:nativeSemanticCandidateBuckets = $bucketCounts", begin)
    counts = script[begin:end]
    for kind in (
        "RENDER_WIDGET_VISIBLE", "RENDER_WIDGET_HIDDEN",
        "CHROME_WIDGET_VISIBLE", "CHROME_WIDGET_HIDDEN",
        "OTHER_VISIBLE", "OTHER_HIDDEN", "UNKNOWN",
    ):
        assert kind + " = 0" in counts
    assert "DiagnosticClassBucket($candidateHandle)" in counts
    assert "$bucketCounts[$classBucket] = [int]$bucketCounts[$classBucket] + 1" in counts
    assert "$report.native_semantic_candidate_buckets = $script:nativeSemanticCandidateBuckets" in script
    assert "$report.status = 'PASS'" not in counts
    assert "Current.Name" not in counts
    assert "ProcessIdForHandle" not in counts


def test_focused_automation_id_requires_exact_launched_process_family() -> None:
    """A foreign application with the same AutomationId must never pass F2/F8/STOP focus."""
    script = _audit_script_text()
    start = script.index("function Wait-ForFocusedAutomationId {")
    stop = script.index("$report = [ordered]@{", start)
    focus = script[start:stop]

    assert "[int]$RootProcessId" in focus
    assert "if ($RootProcessId -le 0)" in focus
    assert "Get-ProcessFamilyIds -RootProcessId $RootProcessId" in focus
    assert "$nextFamilyRefresh = $now.AddMilliseconds(500)" in focus

    pid_witness = "($familyIds -contains [int]$focused.Current.ProcessId)"
    semantic_witness = "[string]$focused.Current.AutomationId -ceq $AutomationId"
    assert pid_witness in focus
    assert semantic_witness in focus
    assert focus.index(pid_witness) < focus.index(semantic_witness) < focus.index("return $focused")
    assert "($familyIds -contains [int]$focused.Current.ProcessId)" in focus

    process_scoped_call = (
        "Wait-ForFocusedAutomationId -RootProcessId $process.Id -AutomationId"
    )
    # F2, F8, emergency STOP, owner panel close, manual panel close.
    assert script.count(process_scoped_call) == 5
    assert "Wait-ForFocusedAutomationId -AutomationId" not in script
