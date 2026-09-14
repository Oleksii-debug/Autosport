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

$expected = @(
    [ordered]@{ key = 'choose_dataset'; automation_id = '101'; name = 'Вибрати replay dataset'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'run_replay'; automation_id = '102'; name = 'Запустити paper replay'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'replay_speed'; automation_id = '103'; name = 'Швидкість replay'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; require_named_rows = $false },
    [ordered]@{ key = 'live_mode'; automation_id = '104'; name = 'Режим live observation'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; require_named_rows = $false },
    [ordered]@{ key = 'live_refresh'; automation_id = '105'; name = 'Оновити live snapshot'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'strategy'; automation_id = '106'; name = 'Стратегія replay'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; require_named_rows = $false },
    [ordered]@{ key = 'research_plan'; automation_id = '107'; name = 'Вибрати research plan'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'repair_workspace'; automation_id = '108'; name = 'Відновити workspace'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'tickets'; automation_id = '201'; name = 'Paper tickets і результати'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; require_named_rows = $true },
    [ordered]@{ key = 'log'; automation_id = '202'; name = 'Журнал виконання'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; require_named_rows = $false },
    [ordered]@{ key = 'live_quotes'; automation_id = '203'; name = 'Live quotes'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; require_named_rows = $true },
    [ordered]@{ key = 'evaluation'; automation_id = '204'; name = 'Evaluation і portfolio evidence'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; require_named_rows = $true },
    [ordered]@{ key = 'bankroll'; automation_id = '205'; name = 'Віртуальний банк'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; require_named_rows = $false; require_value_read_only = $true }
)

function Test-Pattern {
    param(
        [System.Windows.Automation.AutomationElement]$Element,
        [string]$PatternName
    )
    if ([string]::IsNullOrWhiteSpace($PatternName)) { return $true }
    switch ($PatternName) {
        'Invoke' { $pattern = [System.Windows.Automation.InvokePattern]::Pattern }
        'Value' { $pattern = [System.Windows.Automation.ValuePattern]::Pattern }
        default { throw "Unsupported UIA pattern: $PatternName" }
    }
    $patternObject = $null
    return $Element.TryGetCurrentPattern($pattern, [ref]$patternObject)
}

function Get-NamedListItemCount {
    param([System.Windows.Automation.AutomationElement]$Element)

    $count = 0
    $descendants = $Element.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($item in $descendants) {
        try {
            $typeName = [string]$item.Current.ControlType.ProgrammaticName
            $itemName = [string]$item.Current.Name
            if ($typeName -eq 'ControlType.ListItem' -and -not [string]::IsNullOrWhiteSpace($itemName)) {
                $count += 1
            }
        } catch {
            # A fragment can disappear while the provider refreshes; missing rows
            # are caught by the zero-count fail-closed check below.
        }
    }
    return $count
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
        } catch {
            # A bootstrap/child process can turn over while the one-file app starts.
        }
    }

    # MainWindowHandle belongs to the PyInstaller GUI child, not necessarily the
    # launcher returned by Start-Process. Fall back to the desktop UIA tree so a
    # valid child window is still externally discoverable by its real process ID.
    try {
        $desktop = [System.Windows.Automation.AutomationElement]::RootElement
        $windows = $desktop.FindAll(
            [System.Windows.Automation.TreeScope]::Children,
            [System.Windows.Automation.Condition]::TrueCondition
        )
        foreach ($window in $windows) {
            if ($ProcessIds -contains [int]$window.Current.ProcessId) {
                return $window
            }
        }
    } catch {
        # UIA can lag process creation briefly; the bounded caller retries.
    }
    return $null
}

$report = [ordered]@{
    status = 'FAIL'
    source = 'external_windows_uia_client'
    evidence_scope = 'external System.Windows.Automation client against the fresh-extracted packaged Autosport.exe; list keyboard focus is separately gated by the same fresh-extracted EXE keyboard audit; not NVDA speech or physical-human proof'
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

    $report.process_family_ids = @($lastFamilyIds | Sort-Object -Unique)
    $report.process_id = [int]$uiaRoot.Current.ProcessId
    $report.root_name = [string]$uiaRoot.Current.Name
    if ([string]::IsNullOrWhiteSpace($report.root_name)) {
        $report.failures += 'main window has no external UIA Name'
    }

    $descendants = $uiaRoot.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $report.descendant_count = $descendants.Count

    foreach ($spec in $expected) {
        $idCondition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
            [string]$spec.automation_id
        )
        $element = $uiaRoot.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $idCondition)
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
        $namedRowCount = 0
        if ([bool]$spec.require_named_rows) {
            $namedRowCount = Get-NamedListItemCount -Element $element
        }
        $record = [ordered]@{
            key = $spec.key
            automation_id = [string]$element.Current.AutomationId
            expected_name = $spec.name
            name = $currentName
            control_type = $controlType
            expected_control_type = $spec.expected_control_type
            keyboard_focusable = $focusable
            keyboard_focus_required_by_external_gate = [bool]$spec.require_external_focus
            keyboard_focus_gate_owner = if ([bool]$spec.require_external_focus) { 'external_uia_property' } else { 'fresh_extraction_keyboard_audit' }
            enabled = $enabled
            required_pattern = $spec.required_pattern
            required_pattern_available = $patternOk
            value_read_only_required = $valueReadOnlyRequired
            value_read_only = $valueReadOnly
            named_list_item_count = $namedRowCount
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
        if ([bool]$spec.require_named_rows -and $namedRowCount -lt 1) {
            $report.failures += "automation_id=$($spec.automation_id): no externally exposed named ListItem rows"
        }
        if (-not $enabled) {
            $report.failures += "automation_id=$($spec.automation_id): externally disabled at startup"
        }
        if (-not $patternOk) {
            $report.failures += "automation_id=$($spec.automation_id): missing external UIA $($spec.required_pattern) pattern"
        }
        if ($valueReadOnlyRequired -and $valueReadOnly -ne $true) {
            $report.failures += "automation_id=$($spec.automation_id): external UIA ValuePattern is writable or read-only state unavailable"
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
