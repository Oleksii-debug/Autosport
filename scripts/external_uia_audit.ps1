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
    [ordered]@{ key = 'choose_dataset'; automation_id = '101'; name = 'Вибрати replay dataset'; required_pattern = 'Invoke' },
    [ordered]@{ key = 'run_replay'; automation_id = '102'; name = 'Запустити paper replay'; required_pattern = 'Invoke' },
    [ordered]@{ key = 'replay_speed'; automation_id = '103'; name = 'Швидкість replay'; required_pattern = 'Value' },
    [ordered]@{ key = 'live_mode'; automation_id = '104'; name = 'Режим live observation'; required_pattern = 'Value' },
    [ordered]@{ key = 'live_refresh'; automation_id = '105'; name = 'Оновити live snapshot'; required_pattern = 'Invoke' },
    [ordered]@{ key = 'strategy'; automation_id = '106'; name = 'Стратегія replay'; required_pattern = 'Value' },
    [ordered]@{ key = 'research_plan'; automation_id = '107'; name = 'Вибрати research plan'; required_pattern = 'Invoke' },
    [ordered]@{ key = 'tickets'; automation_id = '201'; name = 'Paper tickets і результати'; required_pattern = $null },
    [ordered]@{ key = 'log'; automation_id = '202'; name = 'Журнал виконання'; required_pattern = 'Value' },
    [ordered]@{ key = 'live_quotes'; automation_id = '203'; name = 'Live quotes'; required_pattern = $null }
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

$report = [ordered]@{
    status = 'FAIL'
    source = 'external_windows_uia_client'
    evidence_scope = 'external System.Windows.Automation client against the fresh-extracted packaged Autosport.exe; not NVDA speech or physical-human proof'
    root_name = $null
    process_id = $null
    descendant_count = 0
    controls = @()
    failures = @()
    real_money_execution = $false
    human_tested = $false
    nvda_verified = $false
}

$process = $null
try {
    $exePath = (Resolve-Path -LiteralPath $Exe).Path
    $outputPath = [System.IO.Path]::GetFullPath($Output)
    $outputDirectory = Split-Path -Parent $outputPath
    if (-not [string]::IsNullOrWhiteSpace($outputDirectory)) {
        New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    }

    $process = Start-Process -FilePath $exePath -PassThru
    $report.process_id = $process.Id
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $root = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($process.HasExited) {
            throw "Autosport.exe exited before an externally inspectable main window appeared (exit=$($process.ExitCode))"
        }
        $process.Refresh()
        if ($process.MainWindowHandle -ne 0) {
            try {
                $candidate = [System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
                if ($null -ne $candidate) {
                    $root = $candidate
                    break
                }
            } catch {
                # UIA provider can lag the HWND briefly after Tk creates it.
            }
        }
        Start-Sleep -Milliseconds 250
    }
    if ($null -eq $root) {
        throw "Timed out waiting for an externally inspectable Autosport main window"
    }

    $report.root_name = $root.Current.Name
    if ([string]::IsNullOrWhiteSpace($report.root_name)) {
        $report.failures += 'main window has no external UIA Name'
    }

    $descendants = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $report.descendant_count = $descendants.Count

    foreach ($spec in $expected) {
        $idCondition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
            [string]$spec.automation_id
        )
        $element = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $idCondition)
        if ($null -eq $element) {
            $report.failures += "automation_id=$($spec.automation_id): not found by external UIA client"
            continue
        }

        $currentName = [string]$element.Current.Name
        $controlType = [string]$element.Current.ControlType.ProgrammaticName
        $focusable = [bool]$element.Current.IsKeyboardFocusable
        $enabled = [bool]$element.Current.IsEnabled
        $patternOk = Test-Pattern -Element $element -PatternName $spec.required_pattern
        $record = [ordered]@{
            key = $spec.key
            automation_id = [string]$element.Current.AutomationId
            expected_name = $spec.name
            name = $currentName
            control_type = $controlType
            keyboard_focusable = $focusable
            enabled = $enabled
            required_pattern = $spec.required_pattern
            required_pattern_available = $patternOk
        }
        $report.controls += $record

        if ($currentName -ne $spec.name) {
            $report.failures += "automation_id=$($spec.automation_id): external UIA Name mismatch expected='$($spec.name)' actual='$currentName'"
        }
        if (-not $focusable) {
            $report.failures += "automation_id=$($spec.automation_id): not externally keyboard-focusable"
        }
        if (-not $enabled) {
            $report.failures += "automation_id=$($spec.automation_id): externally disabled at startup"
        }
        if (-not $patternOk) {
            $report.failures += "automation_id=$($spec.automation_id): missing external UIA $($spec.required_pattern) pattern"
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
            if (-not $process.HasExited) {
                $null = $process.CloseMainWindow()
                if (-not $process.WaitForExit(3000)) {
                    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                }
            }
        } catch {
            try { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue } catch {}
        }
    }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Output -Encoding utf8
}

if ($report.status -ne 'PASS') {
    Write-Host ($report | ConvertTo-Json -Depth 8)
    exit 1
}
Write-Host "external_uia_audit=PASS controls=$($report.controls.Count) descendants=$($report.descendant_count)"
exit 0
