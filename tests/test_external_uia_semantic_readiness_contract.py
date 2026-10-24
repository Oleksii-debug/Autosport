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


def test_external_uia_semantic_readiness_has_a_fresh_bounded_provider_budget() -> None:
    script = _audit_script_text()

    witness_guard_index = script.index(
        'throw "Packaged WebView2 runtime witness unavailable before semantic UIA probe"'
    )
    semantic_deadline = "$semanticDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)"
    semantic_state_index = script.index("$semanticReady = $null")
    semantic_timeout_index = script.index(
        'throw "Timed out waiting for WebView2 semantic UIA readiness '
    )
    semantic_block = script[semantic_state_index:semantic_timeout_index]

    assert script.count(semantic_deadline) == 1
    assert witness_guard_index < script.index(semantic_deadline) < semantic_state_index
    assert "while ([DateTime]::UtcNow -lt $semanticDeadline)" in semantic_block
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
    assert "-Deadline $semanticDeadline" in semantic_block
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


def test_family_host_foreground_fallback_is_exact_family_top_level_and_non_chromium() -> None:
    """The launcher may have no visible HWND; never activate renderer/foreign HWNDs."""
    source = _audit_script_text()
    native = source.split("public static class AutosportNativeWindows", 1)[1].split(
        "function Get-ProcessFamilyIds", 1
    )[0]
    helper = native.split(
        "public static bool TryClaimForegroundForVisibleFamilyHost(", 1
    )[1].split("public static IntPtr[] ForProcessFamily(", 1)[0]
    assert "Array.IndexOf(familyIds, launcherId) < 0" in helper
    assert "EnumWindows(callback, IntPtr.Zero)" in helper
    assert "owner == (uint)launcherId" in helper
    assert "Array.IndexOf(familyIds, (int)owner) < 0" in helper
    assert "!IsWindowVisible(window)" in helper
    assert 'DiagnosticClassBucket(window) != "OTHER_VISIBLE"' in helper
    assert "if (count >= windows.Length) return false" in helper
    assert "ProcessIdForHandle(windows[index])" in helper
    assert "Array.IndexOf(familyIds, owner) < 0" in helper
    assert 'DiagnosticClassBucket(windows[index]) != "OTHER_VISIBLE"' in helper
    assert "TryClaimForegroundForProcess(windows[index], owner)" in helper
    assert "GetWindow(" not in helper
    assert "AutomationElement" not in helper


def test_family_host_activation_only_on_observed_child_top_level_and_never_passes_uia() -> None:
    source = _audit_script_text()
    activation = source.split(
        "$report.native_foreground_activation_status = 'NOT_CLAIMED'", 1
    )[1].split("$semanticReady = $null", 1)[0]
    assert "native_foreground_top_level_class -eq 'FAMILY_CHILD_VISIBLE_ONLY'" in activation
    assert "[AutosportNativeWindows]::TryClaimForegroundForVisibleFamilyHost(" in activation
    assert "[int]$process.Id, [int[]]$foregroundFamilyIds" in activation
    assert "if (" in activation
    assert "$report.status = 'PASS'" not in activation
    assert "AutomationElement" not in activation
    assert source.index("$semanticReady = $null") < source.index(
        "Find-UiaRootWithElementForProcessFamily -ProcessIds $lastFamilyIds"
    )
    assert "if ($null -eq $semanticReady -or $null -eq $uiaRoot)" in source
    assert "if ($report.status -ne 'PASS')" in source


def test_renderer_accessibility_activation_is_bounded_and_owned_not_uia_proof() -> None:
    """The actual WebView2 render HWND gets one finite Win32 request, not fake UIA."""
    source = _audit_script_text()
    native = source.split("public static class AutosportNativeWindows", 1)[1].split(
        "function Get-ProcessFamilyIds", 1
    )[0]
    request = native.split("public static string RequestRendererAccessibility(", 1)[1].split(
        "// Privacy-bounded OS-only classification", 1
    )[0]
    assert "private const uint WM_GETOBJECT = 0x003D;" in native
    assert "private const int OBJID_CLIENT = -4;" in native
    assert "private const uint SMTO_ABORTIFHUNG = 0x0002;" in native
    assert "private static extern IntPtr SendMessageTimeout(" in native
    assert "Array.IndexOf(familyIds, owner) < 0" in request
    assert 'DiagnosticClassBucket(window) != "RENDER_WIDGET_VISIBLE"' in request
    assert "SMTO_ABORTIFHUNG, 500, out ignoredReply" in request
    assert "ProcessIdForHandle(window) != owner" in request
    assert 'return "STALE";' in request
    assert 'return dispatched == IntPtr.Zero ? "NO_REPLY" : "DELIVERED";' in request
    assert "AutomationElement" not in request
    assert "SetForegroundWindow" not in request


def test_renderer_accessibility_request_precedes_real_uia_and_cannot_pass_audit() -> None:
    source = _audit_script_text()
    start = source.index("function Find-UiaRootWithElementForProcessFamily")
    end = source.index("function Wait-ForUiaElement", start)
    helper = source[start:end]
    request = "[AutosportNativeWindows]::RequestRendererAccessibility("
    assert helper.count(request) == 1
    assert "$script:nativeRendererAccessibilityRequestAttempted = $false" in source
    assert "-not $script:nativeRendererAccessibilityRequestAttempted" in helper
    assert helper.index("$script:nativeRendererAccessibilityRequestAttempted = $true") < helper.index(request)
    assert "if ($classBucket -eq 'RENDER_WIDGET_VISIBLE'" in helper
    assert helper.index(request) < helper.index("SEMANTIC_NATIVE_HANDLES_READY")
    assert helper.index(request) < helper.index("SEMANTIC_NATIVE_UIA_PROBE")
    assert helper.index(request) < helper.index("SEMANTIC_DESKTOP_POINT_PROBE")
    assert "if ($script:nativeRendererAccessibilityMessageStatus -eq 'DELIVERED')" not in source
    assert "$report.status = 'PASS'" not in helper
    assert "AutomationElement]::FromPoint(" in helper
    assert "$report.native_renderer_accessibility_message_status = $script:nativeRendererAccessibilityMessageStatus" in source
    assert "if ($null -eq $semanticReady -or $null -eq $uiaRoot)" in source


# Plan 6 §7А: genuine packaged WebView2 provider timed out even from a
# separate pwsh -Mta process; minimal WebView2 control focuses its first
# semantic button after loaded.  Exercise the actual extracted product callback
# with fake window events, not a replacement function or synthetic UIA PASS.
def _product_first_focus_callback(
    window,
    *,
    canonical_bridge=True,
    renderer_observed=True,
    trusted_document_observed=True,
    trusted_document_violation=False,
    runtime_identity_violation=False,
    expected_real_url="http://127.0.0.1:8999/index.html",
):
    import json

    shell = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "autosport"
        / "windows_webview_shell.py"
    ).read_text(encoding="utf-8")
    start = shell.index("    def seed_initial_accessibility_focus() -> None:")
    end = shell.index("    close_teardown_lock = threading.Lock()", start)
    callback_source = shell[start:end]
    factory = (
        "def factory(window, *, canonical_bridge, renderer_observed, "
        "trusted_document_observed, trusted_document_violation, "
        "runtime_identity_violation, expected_real_url):\\n"
        "    expected_original_url = 'canonical-index.html'\\n"
        "    initial_accessibility_focus_attempted = False\\n"
    )
    # Python strings above contain literal escaped newline sequences, so
    # normalize them to source newlines before compiling the isolated callback.
    factory = factory.replace("\\n", "\n")
    factory += callback_source + "    return seed_initial_accessibility_focus\\n".replace("\\n", "\n")

    def require_document(candidate, *, expected_original_url):
        if candidate.original_url != expected_original_url:
            raise ValueError("foreign document")
        return candidate.real_url

    namespace = {
        "json": json,
        "_require_packaged_launch_document": require_document,
    }
    exec(compile(factory, "<product-loaded-callback>", "exec"), namespace)
    return namespace["factory"](
        window,
        canonical_bridge=canonical_bridge,
        renderer_observed=renderer_observed,
        trusted_document_observed=trusted_document_observed,
        trusted_document_violation=trusted_document_violation,
        runtime_identity_violation=runtime_identity_violation,
        expected_real_url=expected_real_url,
    )


class _FocusSeedWindow:
    original_url = "canonical-index.html"
    real_url = "http://127.0.0.1:8999/index.html"

    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    def evaluate_js(self, script):
        self.calls.append(script)
        if self.fail:
            raise RuntimeError("WebView2 not ready")
        return True


def test_webview2_focus_seed_is_loaded_only_after_trusted_before_load_hook():
    shell = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "autosport"
        / "windows_webview_shell.py"
    ).read_text(encoding="utf-8")
    before = "window.events.before_load += bind_trusted_document"
    loaded = "window.events.loaded += seed_initial_accessibility_focus"
    assert shell.count(before) == 1
    assert shell.count(loaded) == 1
    assert shell.index(before) < shell.index(loaded)
    assert "window.events.loaded += seed_initial_accessibility_focus" in shell.split(
        "if canonical_bridge:", 1
    )[-1]
    assert "window.events.closing += close_trusted_window_safely" in shell


def test_webview2_focus_seed_uses_only_trusted_first_load_and_never_steals_focus():
    window = _FocusSeedWindow()
    callback = _product_first_focus_callback(window)
    callback()
    callback()
    assert len(window.calls) == 1
    script = window.calls[0]
    assert 'window.location.href !== "http://127.0.0.1:8999/index.html"' in script
    assert "document.activeElement !== document.body" in script
    assert "document.activeElement !== document.documentElement" in script
    assert "getElementById('303')" in script
    assert "target.focus({preventScroll:true})" in script
    assert "target.disabled || target.hidden" in script
    assert "getElementById('330')" not in script


def test_webview2_focus_seed_rejects_untrusted_or_not_ready_document():
    for settings in (
        {"canonical_bridge": False},
        {"renderer_observed": False},
        {"trusted_document_observed": False},
        {"trusted_document_violation": True},
        {"runtime_identity_violation": True},
        {"expected_real_url": None},
    ):
        window = _FocusSeedWindow()
        callback = _product_first_focus_callback(window, **settings)
        callback()
        assert window.calls == [], settings

    foreign = _FocusSeedWindow()
    foreign.original_url = "foreign.html"
    _product_first_focus_callback(foreign)()
    assert foreign.calls == []

    drifted = _FocusSeedWindow()
    drifted.real_url = "http://127.0.0.1:8999/foreign.html"
    _product_first_focus_callback(drifted)()
    assert drifted.calls == []


def test_webview2_focus_seed_failure_is_bounded_and_restart_is_fresh():
    broken = _FocusSeedWindow(fail=True)
    callback = _product_first_focus_callback(broken)
    callback()
    callback()
    assert len(broken.calls) == 1  # No unbounded retry after a JS failure.
    restarted = _FocusSeedWindow()
    _product_first_focus_callback(restarted)()
    assert len(restarted.calls) == 1
    assert "target.focus(" in restarted.calls[0]
    # Focus seeding alone is never a semantic, keyboard, STOP or NVDA PASS.
    audit = _audit_script_text()
    assert "AutomationId '330'" in audit or "-AutomationId '330'" in audit
    assert "$report.status = 'PASS'" in audit
    assert "nvda_verified = $false" in audit
