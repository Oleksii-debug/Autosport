# Windows-only, independent hosted UIA control. NOT Autosport product acceptance.
# Creates disposable WinForms host; probes AutomationElement.FromHandle in a
# separate MTA process; caller forcibly kills any blocked UIA worker.
[CmdletBinding()]
param(
    [ValidateSet('orchestrate', 'host', 'probe')]
    [string]$Mode = 'orchestrate',
    [string]$StateDirectory = '',
    [long]$WindowHandle = 0,
    [int]$ExpectedPid = 0,
    [string]$ReportPath = '',
    [ValidateSet('FromHandle', 'FocusedElement')]
    [string]$ProbeMode = 'FromHandle'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-JsonAtomically {
    param([string]$Path, [object]$Value)
    $tempPath = "$Path.tmp"
    $data = ConvertTo-Json -InputObject $Value -Depth 5 -Compress
    [System.IO.File]::WriteAllText($tempPath, $data, [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::Move($tempPath, $Path, $true)
}

function New-Evidence {
    param([string]$Code)
    return [ordered]@{
        status = $Code
        probe_mode = $ProbeMode
        process_id_matches = $false
        control_type_present = $false
        semantic_focus_matches = $false
        host_focus_claimed = $false
        evidence_class = 'independent_hosted_windows_uia_control_only'
        autosport_packaged_uia_qualified = $false
        autosport_keyboard_qualified = $false
        autosport_emergency_stop_qualified = $false
        real_money_execution = $false
        human_tested = $false
        nvda_verified = $false
        whole_product_complete = $false
    }
}

if ($Mode -eq 'host') {
    if (-not [System.IO.Directory]::Exists($StateDirectory)) { throw 'missing control state directory' }
    Add-Type -AssemblyName System.Windows.Forms
    # Local WinForms Focused is not global desktop foreground ownership.
    # Use the native Win32 witness to avoid attributing a hosted-runner focus
    # limitation to the WebView2/UIA provider itself.
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AutosportUiaNativeForeground {
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
}
'@
    $form = [System.Windows.Forms.Form]::new()
    $form.Name = 'autosport-uia-control-form'
    $form.Text = 'Disposable UIA control'
    $form.Width = 350
    $form.Height = 160
    $form.ShowInTaskbar = $false
    # Exact native focus intent for a separate FocusedElement diagnostic.
    $focusInput = [System.Windows.Forms.TextBox]::new()
    $focusInput.Name = 'autosport-uia-control-focused-input'
    $focusInput.AccessibleName = 'Autosport UIA hosted focus anchor'
    $focusInput.TabIndex = 0
    $form.Controls.Add($focusInput)
    try {
        $form.Show()
        $form.Activate()
        $focusInput.Focus() | Out-Null
        [System.Windows.Forms.Application]::DoEvents()
        # Failure to own the real foreground is reported as CONTROL_FOCUS_NOT_OWNED.
        # It must never be promoted to a focused-UIA or packaged acceptance PASS.
        $foregroundOwned = [AutosportUiaNativeForeground]::GetForegroundWindow() -eq $form.Handle
        $focusClaimed = [bool]$focusInput.Focused -and [bool]$foregroundOwned
        $handle = $form.Handle.ToInt64()
        if ($handle -le 0) { throw 'native control HWND unavailable' }
        Write-JsonAtomically -Path (Join-Path $StateDirectory 'ready.json') -Value ([ordered]@{
            pid = $PID
            hwnd = $handle
            focus_claimed = $focusClaimed
        })
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        while (
            [DateTime]::UtcNow -lt $deadline -and
            -not (Test-Path -LiteralPath (Join-Path $StateDirectory 'stop'))
        ) {
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 30
        }
    }
    finally {
        $form.Close()
        $form.Dispose()
    }
    exit 0
}

if ($Mode -eq 'probe') {
    if ($WindowHandle -le 0 -or $ExpectedPid -le 0) { throw 'invalid native identity' }
    if (-not [System.IO.Directory]::Exists($StateDirectory)) { throw 'missing probe state directory' }
    $result = New-Evidence -Code 'CONTROL_UIA_REJECTED'
    # Fixed classifications only. Never serialize raw UIA property values,
    # foreign window titles, process IDs, or exception text.
    $focusRejectReason = 'PROVIDER_OR_PROPERTY_ERROR'
    try {
        if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -ne [System.Threading.ApartmentState]::MTA) {
            throw 'UIA control probe requires MTA'
        }
        Add-Type -AssemblyName UIAutomationClient
        Add-Type -AssemblyName UIAutomationTypes
        # Each potentially blocking COM entry is isolated in this disposable
        # MTA *process*, not in a ThreadJob sharing a poisoned UIA process.
        if ($ProbeMode -eq 'FocusedElement') {
            [System.IO.File]::WriteAllText((Join-Path $StateDirectory 'stage'), 'FOCUSED_ELEMENT_PROBE')
            $root = [System.Windows.Automation.AutomationElement]::FocusedElement
            [System.IO.File]::WriteAllText((Join-Path $StateDirectory 'stage'), 'FOCUSED_ELEMENT_RETURNED')
        } else {
            $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]::new($WindowHandle))
        }
        if ($null -eq $root) {
            $focusRejectReason = 'NO_ELEMENT'
            throw 'UIA returned no control element'
        }
        $actualPid = [int]$root.Current.ProcessId
        $typeName = [string]$root.Current.ControlType.ProgrammaticName
        if ($actualPid -ne $ExpectedPid) {
            $focusRejectReason = 'FOREIGN_PROCESS'
            throw 'UIA native process mismatch'
        }
        if ([string]::IsNullOrWhiteSpace($typeName)) {
            $focusRejectReason = 'MISSING_TYPE'
            throw 'UIA native type missing'
        }
        if ($ProbeMode -eq 'FocusedElement') {
            # Native WinForms intent + REAL UIA Name/AutomationId/role/PID.
            # A foreign focus, empty provider metadata, or property exception
            # never counts as UIA success.
            $focusId = [string]$root.Current.AutomationId
            $focusName = [string]$root.Current.Name
            if ($focusId -cne 'autosport-uia-control-focused-input') {
                $focusRejectReason = 'AUTOMATION_ID_MISMATCH'
                throw 'focused UIA automation identity mismatch'
            }
            if ($focusName -cne 'Autosport UIA hosted focus anchor') {
                $focusRejectReason = 'ACCESSIBLE_NAME_MISMATCH'
                throw 'focused UIA accessible name mismatch'
            }
            if ($typeName -cne 'ControlType.Edit') {
                $focusRejectReason = 'ROLE_MISMATCH'
                throw 'focused UIA role mismatch'
            }
            $result.status = 'CONTROL_FOCUSED_UIA_OK'
            $result.semantic_focus_matches = $true
        } else {
            $result.status = 'CONTROL_UIA_OK'
        }
        $result.process_id_matches = $true
        $result.control_type_present = $true
    }
    catch {
        # Deliberately never persist window titles, exception messages,
        # command lines, environment, user paths or other sensitive data.
        $result.status = if ($ProbeMode -eq 'FocusedElement') { 'CONTROL_FOCUSED_UIA_REJECTED' } else { 'CONTROL_UIA_REJECTED' }
        if ($ProbeMode -eq 'FocusedElement') { $result.focus_reject_reason = $focusRejectReason }
        $result.process_id_matches = $false
        $result.control_type_present = $false
    }
    Write-JsonAtomically -Path (Join-Path $StateDirectory 'probe.json') -Value $result
    exit 0
}

$result = New-Evidence -Code 'CONTROL_INFRA_UNAVAILABLE'
$work = Join-Path ([System.IO.Path]::GetTempPath()) ('Autosport-UIA-Control-' + [guid]::NewGuid().ToString('N'))
[System.IO.Directory]::CreateDirectory($work) | Out-Null
$hostProcess = $null
$probeProcess = $null
try {
    if (-not $IsWindows) { throw 'Windows is required' }
    $pwsh = (Get-Process -Id $PID).Path
    if (-not [System.IO.File]::Exists($pwsh)) { throw 'PowerShell executable unavailable' }
    if (-not [System.IO.File]::Exists($PSCommandPath)) { throw 'on-disk script required' }
    $quotedScript = '"' + $PSCommandPath + '"'
    $quotedWork = '"' + $work + '"'

    $hostProcess = Start-Process -FilePath $pwsh -PassThru -ArgumentList @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-Sta', '-File', $quotedScript,
        '-Mode', 'host', '-StateDirectory', $quotedWork
    )
    $readyPath = Join-Path $work 'ready.json'
    $until = [DateTime]::UtcNow.AddSeconds(7)
    while (-not (Test-Path -LiteralPath $readyPath) -and [DateTime]::UtcNow -lt $until) {
        if ($hostProcess.HasExited) { break }
        Start-Sleep -Milliseconds 50
    }
    if (-not (Test-Path -LiteralPath $readyPath)) {
        $result.status = 'CONTROL_HOST_UNAVAILABLE'
    }
    else {
        $native = Get-Content -LiteralPath $readyPath -Raw | ConvertFrom-Json
        $controlPid = [int]$native.pid
        $controlHandle = [long]$native.hwnd
        if ($controlPid -le 0 -or $controlHandle -le 0 -or $controlPid -ne $hostProcess.Id) {
            throw 'native host identity mismatch'
        }
        $result.host_focus_claimed = ($native.focus_claimed -eq $true)
        if ($ProbeMode -eq 'FocusedElement' -and $native.focus_claimed -ne $true) {
            # A focus not actually claimed in native WinForms is not a
            # property of the Windows UIA provider: classify separately.
            $result.status = 'CONTROL_FOCUS_NOT_OWNED'
        } else {
        $probeProcess = Start-Process -FilePath $pwsh -PassThru -ArgumentList @(
            '-NoLogo', '-NoProfile', '-NonInteractive', '-Mta', '-File', $quotedScript,
            '-Mode', 'probe', '-StateDirectory', $quotedWork,
            '-WindowHandle', $controlHandle, '-ExpectedPid', $controlPid,
            '-ProbeMode', $ProbeMode
        )
        if (-not $probeProcess.WaitForExit(10000)) {
            $result.status = 'CONTROL_UIA_TIMEOUT'
            if ($ProbeMode -eq 'FocusedElement') {
                $stagePath = Join-Path $work 'stage'
                $stage = 'NO_STAGE'
                if (Test-Path -LiteralPath $stagePath) {
                    $rawStage = [System.IO.File]::ReadAllText($stagePath)
                    $stage = if (@('FOCUSED_ELEMENT_PROBE','FOCUSED_ELEMENT_RETURNED') -ccontains $rawStage) { $rawStage } else { 'INVALID' }
                }
                $result.last_allowlisted_stage = $stage
            }
        }
        else {
            $probePath = Join-Path $work 'probe.json'
            if (-not (Test-Path -LiteralPath $probePath)) {
                $result.status = 'CONTROL_PROBE_FAILED'
            }
            else {
                $probe = Get-Content -LiteralPath $probePath -Raw | ConvertFrom-Json
                $allowedProbeStatuses = if ($ProbeMode -eq 'FocusedElement') {
                    @('CONTROL_FOCUSED_UIA_OK', 'CONTROL_FOCUSED_UIA_REJECTED')
                } else { @('CONTROL_UIA_OK', 'CONTROL_UIA_REJECTED') }
                $allowedFocusRejectReasons = @(
                    'PROVIDER_OR_PROPERTY_ERROR', 'NO_ELEMENT', 'FOREIGN_PROCESS',
                    'MISSING_TYPE', 'AUTOMATION_ID_MISMATCH',
                    'ACCESSIBLE_NAME_MISMATCH', 'ROLE_MISMATCH'
                )
                if (
                    ($ProbeMode -cne 'FocusedElement' -or
                     $probe.status -cne 'CONTROL_FOCUSED_UIA_REJECTED' -or
                     $allowedFocusRejectReasons -ccontains [string]$probe.focus_reject_reason) -and
                    ($probe.status -ceq 'CONTROL_FOCUSED_UIA_REJECTED' -or
                     -not ($probe.PSObject.Properties.Name -ccontains 'focus_reject_reason')) -and
                    $probe.evidence_class -ceq 'independent_hosted_windows_uia_control_only' -and
                    $probe.autosport_packaged_uia_qualified -eq $false -and
                    $probe.real_money_execution -eq $false -and
                    $probe.nvda_verified -eq $false -and
                    $probe.human_tested -eq $false -and
                    $probe.whole_product_complete -eq $false -and
                    $probe.probe_mode -ceq $ProbeMode -and
                    $allowedProbeStatuses -ccontains [string]$probe.status -and
                    (
                        $probe.status -cnotin @('CONTROL_UIA_OK', 'CONTROL_FOCUSED_UIA_OK') -or
                        (
                            $probe.process_id_matches -eq $true -and
                            $probe.control_type_present -eq $true -and
                            ($ProbeMode -cne 'FocusedElement' -or $probe.semantic_focus_matches -eq $true)
                        )
                    )
                ) {
                    $result.status = [string]$probe.status
                    $result.process_id_matches = ($probe.process_id_matches -eq $true)
                    $result.control_type_present = ($probe.control_type_present -eq $true)
                    $result.semantic_focus_matches = ($ProbeMode -eq 'FocusedElement' -and $probe.semantic_focus_matches -eq $true)
                    if ($ProbeMode -eq 'FocusedElement' -and
                        $probe.status -ceq 'CONTROL_FOCUSED_UIA_REJECTED') {
                        $result.focus_reject_reason = [string]$probe.focus_reject_reason
                    }
                }
                else {
                    $result.status = 'CONTROL_PROBE_UNTRUSTED'
                }
            }
        }
        }
    }
}
catch {
    $result.status = 'CONTROL_INFRA_UNAVAILABLE'
}
finally {
    # Stalled COM calls cannot be cancelled reliably inside their own process.
    # Never synchronously join the blocked worker: terminate disposable child.
    if ($null -ne $probeProcess) {
        try { if (-not $probeProcess.HasExited) { $probeProcess.Kill($true) } } catch {}
        try { $probeProcess.Dispose() } catch {}
    }
    try { [System.IO.File]::WriteAllText((Join-Path $work 'stop'), '') } catch {}
    if ($null -ne $hostProcess) {
        try { if (-not $hostProcess.WaitForExit(2000)) { $hostProcess.Kill($true) } } catch {}
        try { $hostProcess.Dispose() } catch {}
    }
    try { [System.IO.Directory]::Delete($work, $true) } catch {}
}
if (-not [string]::IsNullOrWhiteSpace($ReportPath)) {
    Write-JsonAtomically -Path ([System.IO.Path]::GetFullPath($ReportPath)) -Value $result
}
ConvertTo-Json -InputObject $result -Compress
$expectedPass = if ($ProbeMode -eq 'FocusedElement') { 'CONTROL_FOCUSED_UIA_OK' } else { 'CONTROL_UIA_OK' }
if ($result.status -cne $expectedPass) { exit 1 }
