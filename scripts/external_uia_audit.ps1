param(
    [Parameter(Mandatory = $true)]
    [string]$Exe,
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [int]$TimeoutSeconds = 20
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

# This gate describes the semantic HTML/WebView2 surface that ships in the
# package.  It intentionally validates externally observable UIA semantics, not
# the legacy Tk widget contract that preceded the WebView2 migration.
$expected = @(
    [ordered]@{ key = 'choose_dataset'; automation_id = '101'; name = 'Вибрати та перевірити набір даних'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'run_replay'; automation_id = '102'; name = 'Запустити симуляційний повтор'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'replay_speed'; automation_id = '103'; name = 'Швидкість повтору'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; child_types = @() },
    [ordered]@{ key = 'live_mode'; automation_id = '104'; name = 'Режим живих даних'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; child_types = @() },
    [ordered]@{ key = 'live_refresh'; automation_id = '105'; name = 'Оновити поточні котирування'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'strategy'; automation_id = '106'; name = 'Стратегія повтору'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; child_types = @() },
    [ordered]@{ key = 'research_plan'; automation_id = '107'; name = 'Вибрати план дослідження'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'repair_workspace'; automation_id = '108'; name = 'Відновити робочу область'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'tickets'; automation_id = '201'; name = 'Паперові квитки і результати'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.Table'; child_types = @('ControlType.DataItem', 'ControlType.Row') },
    [ordered]@{ key = 'log'; automation_id = '202'; name = 'Журнал виконання'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; child_types = @() },
    [ordered]@{ key = 'live_quotes'; automation_id = '203'; name = 'Поточні котирування'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; child_types = @('ControlType.ListItem') },
    [ordered]@{ key = 'evaluation'; automation_id = '204'; name = 'Оцінювання та докази портфеля'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; child_types = @('ControlType.ListItem') },
    [ordered]@{ key = 'bankroll'; automation_id = '205'; name = 'Віртуальний банк'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; require_value_read_only = $true; child_types = @() },
    [ordered]@{ key = 'shell_navigation'; automation_id = '301'; name = 'Навігація екранами Автоспорт'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; child_types = @() },
    [ordered]@{ key = 'shell_state'; automation_id = '302'; name = 'Стан вибраної поверхні'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; require_value_read_only = $true; child_types = @() },
    [ordered]@{ key = 'shell_open'; automation_id = '303'; name = 'Перейти до робочої поверхні'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'shell_details'; automation_id = '304'; name = 'Контракт вибраного екрана'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; child_types = @('ControlType.ListItem') },
    [ordered]@{ key = 'owner_economic_open'; automation_id = '305'; name = 'Економічні межі власника'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'owner_economic_status'; automation_id = '306'; name = 'Стан економічних меж власника'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; require_value_read_only = $true; child_types = @() },
    [ordered]@{ key = 'owner_economic_readback'; automation_id = '307'; name = 'Точні економічні межі власника'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; child_types = @('ControlType.ListItem') },
    [ordered]@{ key = 'manual_calculation_open'; automation_id = '330'; name = 'Відкрити робочу поверхню ручних розрахунків'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'manual_calculation_operation'; automation_id = '331'; name = 'Операція ручного розрахунку'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; child_types = @() },
    [ordered]@{ key = 'manual_calculation_input'; automation_id = '332'; name = 'Вхідні значення ручного розрахунку'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; child_types = @() },
    [ordered]@{ key = 'manual_calculation_calculate'; automation_id = '333'; name = 'Обчислити ручний результат'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'manual_calculation_result'; automation_id = '334'; name = 'Результат і evidence ручного розрахунку'; required_pattern = 'Value'; require_external_focus = $false; expected_control_type = 'ControlType.Edit'; require_value_read_only = $true; allow_disabled = $true; child_types = @() },
    [ordered]@{ key = 'manual_calculation_clear'; automation_id = '335'; name = 'Очистити ручні значення'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'manual_calculation_close'; automation_id = '336'; name = 'Закрити ручні розрахунки'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() }
)

function Get-ExternalActionPattern {
    param([System.Windows.Automation.AutomationElement]$Element)

    $invokeObject = $null
    if ($Element.TryGetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern,
        [ref]$invokeObject
    ) -and $null -ne $invokeObject) {
        return [pscustomobject]@{ Kind = 'Invoke'; Pattern = $invokeObject }
    }

    # Chromium/WebView2 can expose an HTML button through the legacy-accessible
    # bridge even when UIA InvokePattern is absent. UIA_LegacyIAccessiblePatternId
    # is the stable UI Automation identifier (10018). Resolve the AutomationPattern
    # by identifier instead of naming the concrete LegacyIAccessiblePattern CLR type:
    # some PowerShell/.NET Windows runners expose the pattern object but cannot
    # resolve that concrete type name. Dynamic member access still exercises the
    # real external DefaultAction/DoDefaultAction contract.
    $legacyPattern = [System.Windows.Automation.AutomationPattern]::LookupById(10018)
    if ($null -eq $legacyPattern) { return $null }

    $legacyObject = $null
    if ($Element.TryGetCurrentPattern(
        $legacyPattern,
        [ref]$legacyObject
    ) -and $null -ne $legacyObject) {
        $defaultAction = [string]$legacyObject.Current.DefaultAction
        if (-not [string]::IsNullOrWhiteSpace($defaultAction)) {
            return [pscustomobject]@{ Kind = 'LegacyIAccessible'; Pattern = $legacyObject }
        }
    }

    # Chromium/WebView2 can expose a semantic HTML button as a focusable
    # ControlType.Button while omitting both InvokePattern and the legacy action
    # pattern from this .NET UIA client. That is still externally keyboard
    # actionable. Keep the fallback narrow: exact Button semantics, enabled,
    # keyboard-focusable, and activation through a real key event.
    try {
        if (
            [string]$Element.Current.ControlType.ProgrammaticName -eq 'ControlType.Button' -and
            $Element.Current.IsKeyboardFocusable -eq $true -and
            $Element.Current.IsEnabled -eq $true
        ) {
            return [pscustomobject]@{ Kind = 'KeyboardButton'; Pattern = $null }
        }
    } catch {
        # A disappearing fragment cannot prove keyboard actionability.
    }
    return $null
}

function Test-Pattern {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [string]$PatternName
    )
    if ([string]::IsNullOrWhiteSpace($PatternName)) { return $true }
    if ($PatternName -eq 'Action') {
        return $null -ne (Get-ExternalActionPattern -Element $Element)
    }
    if ($PatternName -eq 'Value') {
        $patternObject = $null
        return $Element.TryGetCurrentPattern(
            [System.Windows.Automation.ValuePattern]::Pattern,
            [ref]$patternObject
        )
    }
    throw "Unsupported UIA pattern: $PatternName"
}

function Invoke-ExternalAction {
    param([System.Windows.Automation.AutomationElement]$Element)

    $action = Get-ExternalActionPattern -Element $Element
    if ($null -eq $action) {
        throw "control exposes neither InvokePattern, LegacyIAccessible default action, nor keyboard-actionable Button semantics"
    }
    if ($action.Kind -eq 'Invoke') {
        ([System.Windows.Automation.InvokePattern]$action.Pattern).Invoke()
        return
    }
    if ($action.Kind -eq 'LegacyIAccessible') {
        $action.Pattern.DoDefaultAction()
        return
    }
    if ($action.Kind -eq 'KeyboardButton') {
        $Element.SetFocus()
        Start-Sleep -Milliseconds 50
        [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
        return
    }
    throw "unsupported external action kind '$($action.Kind)'"
}

function Test-IsNamedStructuralHeaderRow {
    param([System.Windows.Automation.AutomationElement]$Element)

    # WebView2 exposes the semantic <thead><tr><th scope="col">… structure as an
    # unnamed ControlType.Row containing a named ControlType.HeaderItem. That row
    # is table structure, not ticket data, so it must not be counted as an unnamed
    # data item. Actual body rows remain subject to the nonblank-name gate.
    $descendants = $Element.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($child in $descendants) {
        try {
            $childType = [string]$child.Current.ControlType.ProgrammaticName
            if ($childType -ne 'ControlType.HeaderItem') { continue }
            $childName = [string]$child.Current.Name
            if (-not [string]::IsNullOrWhiteSpace($childName)) { return $true }
        } catch {
            # A disappearing fragment cannot prove structural-header semantics.
        }
    }
    return $false
}

function Get-SemanticChildStats {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [string[]]$AllowedTypes
    )

    $candidateCount = 0
    $namedCount = 0
    $unnamedCount = 0
    if ($null -eq $AllowedTypes -or $AllowedTypes.Count -eq 0) {
        return [pscustomobject]@{ CandidateCount = 0; NamedCount = 0; UnnamedCount = 0 }
    }
    $descendants = $Element.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($item in $descendants) {
        try {
            $typeName = [string]$item.Current.ControlType.ProgrammaticName
            if (-not ($AllowedTypes -contains $typeName)) { continue }
            $itemName = [string]$item.Current.Name
            if (
                [string]::IsNullOrWhiteSpace($itemName) -and
                $typeName -eq 'ControlType.Row' -and
                (Test-IsNamedStructuralHeaderRow -Element $item)
            ) {
                continue
            }
            $candidateCount += 1
            if ([string]::IsNullOrWhiteSpace($itemName)) {
                $unnamedCount += 1
            } else {
                $namedCount += 1
            }
        } catch {
            # A fragment can disappear during projection. A later complete
            # collection snapshot still has to satisfy the semantic checks.
        }
    }
    return [pscustomobject]@{
        CandidateCount = $candidateCount
        NamedCount = $namedCount
        UnnamedCount = $unnamedCount
    }
}

function Get-ProcessFamilyIds {
    param([int]$RootProcessId)

    $seen = New-Object 'System.Collections.Generic.HashSet[int]'
    $pending = New-Object 'System.Collections.Generic.Queue[int]'
    $pending.Enqueue($RootProcessId)
    while ($pending.Count -gt 0) {
        $currentId = $pending.Dequeue()
        if (-not $seen.Add($currentId)) { continue }
        $children = @(
            Get-CimInstance Win32_Process -Filter "ParentProcessId = $currentId" -ErrorAction SilentlyContinue
        )
        foreach ($child in $children) {
            $pending.Enqueue([int]$child.ProcessId)
        }
    }
    return @($seen | ForEach-Object { [int]$_ })
}

function Find-UiaRootForProcessFamily {
    param([int[]]$ProcessIds)

    foreach ($candidateId in $ProcessIds) {
        try {
            $candidateProcess = Get-Process -Id $candidateId -ErrorAction Stop
            $candidateProcess.Refresh()
            if ($candidateProcess.MainWindowHandle -ne 0) {
                $element = [System.Windows.Automation.AutomationElement]::FromHandle(
                    $candidateProcess.MainWindowHandle
                )
                if ($null -ne $element) { return $element }
            }
        } catch {}
    }
    try {
        $desktop = [System.Windows.Automation.AutomationElement]::RootElement
        $windows = $desktop.FindAll(
            [System.Windows.Automation.TreeScope]::Children,
            [System.Windows.Automation.Condition]::TrueCondition
        )
        foreach ($window in $windows) {
            if ($ProcessIds -contains [int]$window.Current.ProcessId) { return $window }
        }
    } catch {}
    return $null
}

function Find-UiaElementForProcessFamily {
    param(
        [int[]]$ProcessIds,
        [string]$AutomationId
    )

    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        $AutomationId
    )
    try {
        $desktop = [System.Windows.Automation.AutomationElement]::RootElement
        $windows = $desktop.FindAll(
            [System.Windows.Automation.TreeScope]::Children,
            [System.Windows.Automation.Condition]::TrueCondition
        )
        foreach ($window in $windows) {
            if (-not ($ProcessIds -contains [int]$window.Current.ProcessId)) { continue }
            if ([string]$window.Current.AutomationId -eq $AutomationId) { return $window }
            $element = $window.FindFirst(
                [System.Windows.Automation.TreeScope]::Descendants,
                $condition
            )
            if ($null -ne $element) { return $element }
        }
    } catch {}
    return $null
}

function Find-UiaRootWithElementForProcessFamily {
    param(
        [int[]]$ProcessIds,
        [string]$AutomationId
    )

    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        $AutomationId
    )
    try {
        $desktop = [System.Windows.Automation.AutomationElement]::RootElement
        $windows = $desktop.FindAll(
            [System.Windows.Automation.TreeScope]::Children,
            [System.Windows.Automation.Condition]::TrueCondition
        )
        foreach ($window in $windows) {
            if (-not ($ProcessIds -contains [int]$window.Current.ProcessId)) { continue }
            if ([string]$window.Current.AutomationId -eq $AutomationId) {
                return [pscustomobject]@{ Root = $window; Element = $window }
            }
            $element = $window.FindFirst(
                [System.Windows.Automation.TreeScope]::Descendants,
                $condition
            )
            if ($null -ne $element) {
                return [pscustomobject]@{ Root = $window; Element = $element }
            }
        }
    } catch {}
    return $null
}

function Wait-ForUiaElement {
    param(
        [int]$RootProcessId,
        [string]$AutomationId,
        [DateTime]$Deadline
    )

    while ([DateTime]::UtcNow -lt $Deadline) {
        $ids = @(Get-ProcessFamilyIds -RootProcessId $RootProcessId)
        $element = Find-UiaElementForProcessFamily -ProcessIds $ids -AutomationId $AutomationId
        if ($null -ne $element) { return $element }
        Start-Sleep -Milliseconds 100
    }
    return $null
}

$report = [ordered]@{
    status = 'FAIL'
    source = 'external_windows_uia_client'
    evidence_scope = 'external System.Windows.Automation client against the fresh-extracted packaged Autosport.exe; semantic HTML collections may be legitimately empty at startup; not NVDA speech or physical-human proof'
    launcher_process_id = $null
    process_id = $null
    process_family_ids = @()
    root_name = $null
    descendant_count = 0
    controls = @()
    failures = @()
    real_money_execution = $false
    human_tested = $false
    nvda_verified = $false
}

$process = $null
$lastFamilyIds = @()
$uiaRoot = $null
try {
    $exePath = (Resolve-Path -LiteralPath $Exe).Path
    $outputPath = [System.IO.Path]::GetFullPath($Output)
    $outputDirectory = Split-Path -Parent $outputPath
    if (-not [string]::IsNullOrWhiteSpace($outputDirectory)) {
        New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    }

    $process = Start-Process -FilePath $exePath -PassThru
    $report.launcher_process_id = $process.Id
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $lastFamilyIds = @(Get-ProcessFamilyIds -RootProcessId $process.Id)
        $uiaRoot = Find-UiaRootForProcessFamily -ProcessIds $lastFamilyIds
        if ($null -ne $uiaRoot) { break }
        Start-Sleep -Milliseconds 250
    }
    if ($null -eq $uiaRoot) {
        throw "Timed out waiting for an externally inspectable Autosport main window across packaged process family"
    }

    # Native host visibility is not semantic readiness. The already discovered
    # top-level window is the cheapest and strongest same-window probe, so poll its
    # descendants directly. WebView2 can finish attaching under another process-
    # family top-level after the native host first appears; retain that bounded
    # rediscovery path, but rate-limit the expensive CIM + desktop enumeration.
    $semanticReady = $null
    $semanticCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        '330'
    )
    $nextFamilyRefresh = [DateTime]::UtcNow.AddMilliseconds(1000)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            if ([string]$uiaRoot.Current.AutomationId -eq '330') {
                $semanticReady = $uiaRoot
            } else {
                $semanticReady = $uiaRoot.FindFirst(
                    [System.Windows.Automation.TreeScope]::Descendants,
                    $semanticCondition
                )
            }
        } catch {
            $semanticReady = $null
        }
        if ($null -ne $semanticReady) { break }

        $now = [DateTime]::UtcNow
        if ($now -ge $nextFamilyRefresh) {
            $lastFamilyIds = @(Get-ProcessFamilyIds -RootProcessId $process.Id)
            $semanticSurface = Find-UiaRootWithElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '330'
            if ($null -ne $semanticSurface) {
                $uiaRoot = $semanticSurface.Root
                $semanticReady = $semanticSurface.Element
                break
            }
            $nextFamilyRefresh = $now.AddMilliseconds(1000)
        }
        Start-Sleep -Milliseconds 100
    }
    if ($null -eq $semanticReady -or $null -eq $uiaRoot) {
        throw "Timed out waiting for WebView2 semantic UIA readiness (automation_id=330) across packaged process family"
    }

    $report.process_family_ids = @($lastFamilyIds | Sort-Object -Unique)
    $report.process_id = [int]$uiaRoot.Current.ProcessId
    $report.root_name = [string]$uiaRoot.Current.Name
    if ([string]::IsNullOrWhiteSpace($report.root_name)) {
        $report.failures += 'main window has no external UIA Name'
    }

    # Owner/manual details are intentionally hidden in the semantic document at
    # startup. Exercise the real external activation contract before auditing the
    # controls inside those panels; inspecting hidden descendants would be a false
    # negative and skipping activation would fail to prove keyboard-operable flow.
    $ownerOpen = Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '305'
    if ($null -eq $ownerOpen) {
        $report.failures += 'automation_id=305: cannot open owner economic panel for external UIA audit'
    } else {
        try {
            Invoke-ExternalAction -Element $ownerOpen
            $ownerDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(5, $TimeoutSeconds))
            $ownerProbe = Wait-ForUiaElement -RootProcessId $process.Id -AutomationId '306' -Deadline $ownerDeadline
            if ($null -eq $ownerProbe) {
                $report.failures += 'automation_id=306: owner economic panel did not become externally inspectable'
            }
        } catch {
            $report.failures += "owner economic panel open failed: $($_.Exception.Message)"
        }
    }

    $manualOpen = $semanticReady
    try {
        Invoke-ExternalAction -Element $manualOpen
        $manualDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(5, $TimeoutSeconds))
        $manualProbe = Wait-ForUiaElement -RootProcessId $process.Id -AutomationId '331' -Deadline $manualDeadline
        if ($null -eq $manualProbe) {
            $report.failures += 'automation_id=331: manual calculation panel did not become externally inspectable'
        }
    } catch {
        $report.failures += "manual calculation panel open failed: $($_.Exception.Message)"
    }

    $lastFamilyIds = @(Get-ProcessFamilyIds -RootProcessId $process.Id)
    $report.process_family_ids = @($lastFamilyIds | Sort-Object -Unique)
    $descendants = $uiaRoot.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $report.descendant_count = $descendants.Count

    foreach ($spec in $expected) {
        $element = Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId ([string]$spec.automation_id)
        if ($null -eq $element) {
            $report.failures += "automation_id=$($spec.automation_id): not found by external UIA client"
            continue
        }

        $currentName = [string]$element.Current.Name
        $controlType = [string]$element.Current.ControlType.ProgrammaticName
        $focusable = [bool]$element.Current.IsKeyboardFocusable
        $enabled = [bool]$element.Current.IsEnabled
        $patternOk = Test-Pattern -Element $element -PatternName $spec.required_pattern

        $valueReadOnlyRequired = [bool]$spec.require_value_read_only
        $valueReadOnly = $null
        if ($valueReadOnlyRequired) {
            $valuePatternObject = $null
            try {
                $valuePatternAvailable = $element.TryGetCurrentPattern(
                    [System.Windows.Automation.ValuePattern]::Pattern,
                    [ref]$valuePatternObject
                )
                if ($valuePatternAvailable -and $null -ne $valuePatternObject) {
                    $valuePattern = [System.Windows.Automation.ValuePattern]$valuePatternObject
                    $valueReadOnly = [bool]$valuePattern.Current.IsReadOnly
                }
            } catch {
                $valueReadOnly = $null
            }
        }

        $childStats = Get-SemanticChildStats -Element $element -AllowedTypes @($spec.child_types)
        $record = [ordered]@{
            key = $spec.key
            automation_id = [string]$element.Current.AutomationId
            expected_name = $spec.name
            name = $currentName
            control_type = $controlType
            expected_control_type = $spec.expected_control_type
            keyboard_focusable = $focusable
            keyboard_focus_required_by_external_gate = [bool]$spec.require_external_focus
            enabled = $enabled
            required_pattern = $spec.required_pattern
            required_pattern_available = $patternOk
            value_read_only_required = $valueReadOnlyRequired
            value_read_only = $valueReadOnly
            semantic_child_count = $childStats.CandidateCount
            named_semantic_child_count = $childStats.NamedCount
            unnamed_semantic_child_count = $childStats.UnnamedCount
        }
        $report.controls += $record

        if ($currentName -ne $spec.name) {
            $report.failures += "automation_id=$($spec.automation_id): external UIA Name mismatch expected='$($spec.name)' actual='$currentName'"
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$spec.expected_control_type) -and $controlType -ne [string]$spec.expected_control_type) {
            $report.failures += "automation_id=$($spec.automation_id): external UIA ControlType mismatch expected='$($spec.expected_control_type)' actual='$controlType'"
        }
        if ([bool]$spec.require_external_focus -and -not $focusable) {
            $report.failures += "automation_id=$($spec.automation_id): not externally keyboard-focusable"
        }
        if (-not $enabled -and -not [bool]$spec.allow_disabled) {
            $report.failures += "automation_id=$($spec.automation_id): externally disabled at audit time"
        }
        if (-not $patternOk) {
            $report.failures += "automation_id=$($spec.automation_id): missing external UIA $($spec.required_pattern) pattern"
        }
        if ($valueReadOnlyRequired -and $valueReadOnly -ne $true) {
            $report.failures += "automation_id=$($spec.automation_id): external UIA ValuePattern is writable or read-only state unavailable"
        }
        if ($childStats.UnnamedCount -gt 0) {
            $report.failures += "automation_id=$($spec.automation_id): exposes $($childStats.UnnamedCount) unnamed semantic collection items"
        }
    }

    if ($report.controls.Count -ne $expected.Count) {
        $report.failures += "external UIA found $($report.controls.Count) of $($expected.Count) critical controls"
    }
    if ($report.failures.Count -eq 0) {
        $report.status = 'PASS'
    }
} catch {
    $report.failures += "$($_.Exception.GetType().Name): $($_.Exception.Message)"
} finally {
    if ($null -ne $process) {
        try {
            $cleanupIds = @(Get-ProcessFamilyIds -RootProcessId $process.Id | Sort-Object -Unique -Descending)
        } catch {
            $cleanupIds = @($lastFamilyIds | Sort-Object -Unique -Descending)
        }
        foreach ($cleanupId in $cleanupIds) {
            try {
                $candidate = Get-Process -Id $cleanupId -ErrorAction Stop
                if (-not $candidate.HasExited) {
                    if ($candidate.MainWindowHandle -ne 0) {
                        $null = $candidate.CloseMainWindow()
                        $null = $candidate.WaitForExit(2000)
                    }
                    if (-not $candidate.HasExited) {
                        Stop-Process -Id $cleanupId -Force -ErrorAction SilentlyContinue
                    }
                }
            } catch {}
        }
    }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Output -Encoding utf8
}

if ($report.status -ne 'PASS') {
    Write-Host ($report | ConvertTo-Json -Depth 8)
    exit 1
}
Write-Host "external_uia_audit=PASS controls=$($report.controls.Count) descendants=$($report.descendant_count) process=$($report.process_id)"
exit 0