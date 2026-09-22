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
    [ordered]@{ key = 'choose_dataset'; automation_id = '101'; name = 'Вибрати набір даних для повтору'; help_text = 'Відкриває вибір папки набору даних для повтору і перевіряє її у фоновому процесі лише для читання. Гаряча клавіша Control+O.'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'run_replay'; automation_id = '102'; name = 'Запустити паперовий повтор'; help_text = 'Запускає причинний паперовий повтор для вибраного набору даних і канонічної стратегії. Гаряча клавіша Control+R.'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'replay_speed'; automation_id = '103'; name = 'Швидкість повтору'; help_text = 'Вибір подієвого, 1×, 10×, 100× або 1000× режиму повтору.'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; require_named_rows = $false },
    [ordered]@{ key = 'live_mode'; automation_id = '104'; name = 'Режим живого спостереження'; help_text = 'Публічний перегляд без ключа або автентифікований ключ API із середовища.'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; require_named_rows = $false },
    [ordered]@{ key = 'live_refresh'; automation_id = '105'; name = 'Оновити поточний знімок'; help_text = 'Запускає один знімок настільного тенісу лише для читання у фоновому процесі. Гаряча клавіша Control+L.'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'strategy'; automation_id = '106'; name = 'Стратегія повтору'; help_text = 'Канонічна стратегія для вибору. Для типізованого дослідницького повтору потрібен план дослідження.'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; require_named_rows = $false },
    [ordered]@{ key = 'research_plan'; automation_id = '107'; name = 'Вибрати план дослідження'; help_text = 'Вибирає та перевіряє типізований причинний JSON-план дослідження для research-replay-v1.'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'repair_workspace'; automation_id = '108'; name = 'Відновити робочу область'; help_text = 'Запускає закрите при помилці відновлення робочої області для вибраної канонічної стратегії. Гаряча клавіша Control+Shift+R.'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'tickets'; automation_id = '201'; name = 'Паперові квитки і результати'; help_text = 'Список віртуальних квитків та їх поточних результатів. F6 переводить сюди фокус.'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; require_named_rows = $true },
    [ordered]@{ key = 'log'; automation_id = '202'; name = 'Журнал виконання'; help_text = 'Текстовий журнал повтору, спостереження, розрахунку результатів та оцінювання.'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; require_named_rows = $false },
    [ordered]@{ key = 'live_quotes'; automation_id = '203'; name = 'Поточні котирування'; help_text = 'Поточні котирування лише для читання з останнього знімка. F7 переводить сюди фокус.'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; require_named_rows = $true },
    [ordered]@{ key = 'evaluation'; automation_id = '204'; name = 'Оцінювання та докази портфеля'; help_text = 'Підсумок останнього завершеного паперового повтору: банк, ROI, результати квитків і явно позначені сценарії портфеля. F8 переводить сюди фокус.'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; require_named_rows = $true },
    [ordered]@{ key = 'bankroll'; automation_id = '205'; name = 'Віртуальний банк'; help_text = 'Поле лише для читання з поточним віртуальним банком, зарезервованою паперовою ставкою, канонічною стратегією та робочою областю. Доступне переходом Tab.'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; require_named_rows = $false; require_value_read_only = $true },
    [ordered]@{ key = 'shell_navigation'; automation_id = '301'; name = 'Навігація екранами Автоспорт'; help_text = 'Виберіть один із канонічних екранів. F2 переводить фокус сюди; Control+Alt+Left/Right рухає між екранами.'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; require_named_rows = $false },
    [ordered]@{ key = 'shell_state'; automation_id = '302'; name = 'Стан вибраної поверхні'; help_text = 'Лише для читання: активна, інформаційна або видима, але вимкнена можливість.'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; require_named_rows = $false; require_value_read_only = $true },
    [ordered]@{ key = 'shell_open'; automation_id = '303'; name = 'Перейти до робочої поверхні'; help_text = 'Переводить фокус до вже реалізованого робочого контролу. Для неактивних можливостей кнопка вимкнена.'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'shell_details'; automation_id = '304'; name = 'Контракт вибраного екрана'; help_text = 'Опис лише для читання: задача, клавіатура, стани, збереження та межі доменної істини вибраного екрана.'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; require_named_rows = $true },
    [ordered]@{ key = 'owner_economic_open'; automation_id = '305'; name = 'Економічні межі власника'; help_text = 'Відкриває лише читання поточного контракту або форму одноразового початкового створення.'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'owner_economic_status'; automation_id = '306'; name = 'Стан економічних меж власника'; help_text = 'Лише для читання: відсутній, дійсний або пошкоджений контракт економічної цілі.'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; require_named_rows = $false; require_value_read_only = $true },
    [ordered]@{ key = 'owner_economic_readback'; automation_id = '307'; name = 'Точні економічні межі власника'; help_text = 'Список лише для читання з ідентичністю, межами, автоматизацією, аварійною зупинкою та списками заборон.'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.List'; require_named_rows = $true },
    [ordered]@{ key = 'manual_calculation_open'; automation_id = '330'; name = 'Відкрити робочу поверхню ручних розрахунків'; help_text = 'Українська клавіатурна поверхня для ручних паперових/дослідницьких розрахунків без запису на диск і без реального виконання.'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'manual_calculation_operation'; automation_id = '331'; name = 'Операція ручного розрахунку'; help_text = 'Вибір канонічної ручної операції; формула залишається в сервісі.'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; require_named_rows = $false },
    [ordered]@{ key = 'manual_calculation_input'; automation_id = '332'; name = 'Вхідні значення ручного розрахунку'; help_text = 'Лише ручний текстовий ввід; помилкові та дубльовані значення відхиляються.'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; require_named_rows = $false },
    [ordered]@{ key = 'manual_calculation_calculate'; automation_id = '333'; name = 'Обчислити ручний результат'; help_text = 'Запускає лише канонічний ManualCalculationService; реальні ставки не створюються.'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'manual_calculation_result'; automation_id = '334'; name = 'Результат і evidence ручного розрахунку'; help_text = 'Лише для читання: канонічний результат, припущення, попередження та evidence hash.'; required_pattern = 'Value'; require_external_focus = $false; expected_control_type = 'ControlType.Edit'; require_named_rows = $false; require_value_read_only = $true; allow_disabled = $true },
    [ordered]@{ key = 'manual_calculation_clear'; automation_id = '335'; name = 'Очистити ручні значення'; help_text = 'Очищає локальні поля без запису на диск.'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false },
    [ordered]@{ key = 'manual_calculation_close'; automation_id = '336'; name = 'Закрити ручні розрахунки'; help_text = 'Закриває робочу поверхню без запису результату.'; required_pattern = 'Invoke'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; require_named_rows = $false }
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
    } catch {
        # The UIA tree may change while a dialog opens; bounded callers retry.
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

    $manualOpen = Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '330'
    if ($null -eq $manualOpen) {
        $report.failures += 'automation_id=330: cannot open manual calculation workbench for external UIA audit'
    } else {
        try {
            $invokeObject = $null
            if (-not $manualOpen.TryGetCurrentPattern(
                [System.Windows.Automation.InvokePattern]::Pattern,
                [ref]$invokeObject
            )) {
                throw 'manual calculation open control has no InvokePattern'
            }
            ([System.Windows.Automation.InvokePattern]$invokeObject).Invoke()
            $dialogDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(5, $TimeoutSeconds))
            $dialogProbe = $null
            while ([DateTime]::UtcNow -lt $dialogDeadline) {
                $lastFamilyIds = @(Get-ProcessFamilyIds -RootProcessId $process.Id)
                $dialogProbe = Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '331'
                if ($null -ne $dialogProbe) { break }
                Start-Sleep -Milliseconds 100
            }
            if ($null -eq $dialogProbe) {
                $report.failures += 'automation_id=331: manual calculation dialog did not become externally inspectable'
            }
        } catch {
            $report.failures += "manual calculation dialog open failed: $($_.Exception.Message)"
        }
    }

    foreach ($spec in $expected) {
        $element = Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId ([string]$spec.automation_id)
        if ($null -eq $element) {
            $report.failures += "automation_id=$($spec.automation_id): not found by external UIA client"
            continue
        }

        $currentName = [string]$element.Current.Name
        $currentHelpText = [string]$element.Current.HelpText
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
            expected_help_text = $spec.help_text
            help_text = $currentHelpText
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
        if ($currentHelpText -ne [string]$spec.help_text) {
            $report.failures += "automation_id=$($spec.automation_id): external UIA HelpText mismatch expected='$($spec.help_text)' actual='$currentHelpText'"
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
        if (-not $enabled -and -not [bool]$spec.allow_disabled) {
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
