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
    [string]$ReportPath = ''
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
    $form = [System.Windows.Forms.Form]::new()
    $form.Name = 'autosport-uia-control-form'
    $form.Text = 'Disposable UIA control'
    $form.Width = 350
    $form.Height = 160
    $form.ShowInTaskbar = $false
    try {
        $form.Show()
        $handle = $form.Handle.ToInt64()
        if ($handle -le 0) { throw 'native control HWND unavailable' }
        Write-JsonAtomically -Path (Join-Path $StateDirectory 'ready.json') -Value ([ordered]@{
            pid = $PID
            hwnd = $handle
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
    try {
        if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -ne [System.Threading.ApartmentState]::MTA) {
            throw 'UIA control probe requires MTA'
        }
        Add-Type -AssemblyName UIAutomationClient
        Add-Type -AssemblyName UIAutomationTypes
        # Genuine external UIA provider call. No in-process retry on timeout.
        $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]::new($WindowHandle))
        if ($null -eq $root) { throw 'UIA returned no native control element' }
        $actualPid = [int]$root.Current.ProcessId
        $typeName = [string]$root.Current.ControlType.ProgrammaticName
        if ($actualPid -ne $ExpectedPid -or [string]::IsNullOrWhiteSpace($typeName)) {
            throw 'UIA native identity/type mismatch'
        }
        $result.status = 'CONTROL_UIA_OK'
        $result.process_id_matches = $true
        $result.control_type_present = $true
    }
    catch {
        # Deliberately never persist window titles, exception messages,
        # command lines, environment, user paths or other sensitive data.
        $result.status = 'CONTROL_UIA_REJECTED'
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
        $probeProcess = Start-Process -FilePath $pwsh -PassThru -ArgumentList @(
            '-NoLogo', '-NoProfile', '-NonInteractive', '-Mta', '-File', $quotedScript,
            '-Mode', 'probe', '-StateDirectory', $quotedWork,
            '-WindowHandle', $controlHandle, '-ExpectedPid', $controlPid
        )
        if (-not $probeProcess.WaitForExit(10000)) {
            $result.status = 'CONTROL_UIA_TIMEOUT'
        }
        else {
            $probePath = Join-Path $work 'probe.json'
            if (-not (Test-Path -LiteralPath $probePath)) {
                $result.status = 'CONTROL_PROBE_FAILED'
            }
            else {
                $probe = Get-Content -LiteralPath $probePath -Raw | ConvertFrom-Json
                if (
                    $probe.evidence_class -ceq 'independent_hosted_windows_uia_control_only' -and
                    $probe.autosport_packaged_uia_qualified -eq $false -and
                    $probe.real_money_execution -eq $false -and
                    $probe.nvda_verified -eq $false -and
                    @('CONTROL_UIA_OK', 'CONTROL_UIA_REJECTED') -ccontains [string]$probe.status -and
                    (
                        $probe.status -cne 'CONTROL_UIA_OK' -or
                        (
                            $probe.process_id_matches -eq $true -and
                            $probe.control_type_present -eq $true
                        )
                    )
                ) {
                    $result.status = [string]$probe.status
                }
                else {
                    $result.status = 'CONTROL_PROBE_UNTRUSTED'
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
if ($result.status -cne 'CONTROL_UIA_OK') { exit 1 }
