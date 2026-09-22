param(
    [int]$RootProcessId = 0,
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [int]$TimeoutSeconds = 10,
    [int]$MinimumEvents = 1,
    [switch]$IncludeDisplayString,
    [switch]$CapabilityProbeOnly
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$recorderSource = @'
using System;
using System.Collections.Concurrent;
using System.Windows.Automation;

public sealed class AutosportUiaNotificationRecord
{
    public string ObservedAtUtc { get; set; }
    public int SenderProcessId { get; set; }
    public string SenderAutomationId { get; set; }
    public string NotificationKind { get; set; }
    public string NotificationProcessing { get; set; }
    public string DisplayString { get; set; }
    public string ActivityId { get; set; }
}

public sealed class AutosportUiaNotificationRecorder
{
    public ConcurrentQueue<AutosportUiaNotificationRecord> Records { get; }
        = new ConcurrentQueue<AutosportUiaNotificationRecord>();

    public AutomationEventHandler Handler { get; }

    public AutosportUiaNotificationRecorder()
    {
        Handler = OnEvent;
    }

    private void OnEvent(object sender, AutomationEventArgs eventArgs)
    {
        NotificationEventArgs notification = eventArgs as NotificationEventArgs;
        if (notification == null)
        {
            return;
        }

        AutomationElement element = sender as AutomationElement;
        int processId = 0;
        string automationId = string.Empty;
        if (element != null)
        {
            try { processId = element.Current.ProcessId; } catch { }
            try { automationId = element.Current.AutomationId ?? string.Empty; } catch { }
        }

        Records.Enqueue(new AutosportUiaNotificationRecord
        {
            ObservedAtUtc = DateTime.UtcNow.ToString("O"),
            SenderProcessId = processId,
            SenderAutomationId = automationId,
            NotificationKind = notification.NotificationKind.ToString(),
            NotificationProcessing = notification.NotificationProcessing.ToString(),
            DisplayString = notification.DisplayString ?? string.Empty,
            ActivityId = notification.ActivityId ?? string.Empty,
        });
    }
}
'@

if (-not ('AutosportUiaNotificationRecorder' -as [type])) {
    Add-Type -TypeDefinition $recorderSource -Language CSharp
}

function Get-Sha256Text {
    param([string]$Value)

    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($hash.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally {
        $hash.Dispose()
    }
}

function Get-ProcessFamilyIds {
    param([int]$RootId)

    $seen = New-Object 'System.Collections.Generic.HashSet[int]'
    $pending = New-Object 'System.Collections.Generic.Queue[int]'
    $pending.Enqueue($RootId)
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
            $candidate = Get-Process -Id $candidateId -ErrorAction Stop
            $candidate.Refresh()
            if ($candidate.MainWindowHandle -eq 0) { continue }
            $element = [System.Windows.Automation.AutomationElement]::FromHandle(
                $candidate.MainWindowHandle
            )
            if ($null -ne $element) { return $element }
        } catch {
            # A launcher or child process can turn over while the packaged app starts.
        }
    }

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
        # UIA discovery is retried by the bounded caller.
    }
    return $null
}

function Get-FocusIdentity {
    try {
        $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
        if ($null -eq $focused) { return $null }
        return [ordered]@{
            process_id = [int]$focused.Current.ProcessId
            automation_id = [string]$focused.Current.AutomationId
        }
    } catch {
        return $null
    }
}

$report = [ordered]@{
    schema_version = 1
    status = 'FAIL'
    source = 'external_windows_uia_notification_client'
    evidence_scope = 'machine UIA NotificationEvent evidence only; not NVDA speech or human proof'
    root_process_id = if ($RootProcessId -gt 0) { $RootProcessId } else { $null }
    process_family_ids = @()
    subscribed_process_id = $null
    notification_event_id = [int][System.Windows.Automation.AutomationElement]::NotificationEvent.Id
    notification_event_args_type = [System.Windows.Automation.NotificationEventArgs].FullName
    include_display_string = [bool]$IncludeDisplayString
    minimum_events = $MinimumEvents
    event_count = 0
    focus_before = $null
    focus_after = $null
    focus_changed = $null
    events = @()
    failures = @()
    real_money_execution = $false
    human_tested = $false
    nvda_verified = $false
    whole_product_complete = $false
}

$outputPath = [System.IO.Path]::GetFullPath($Output)
$outputDirectory = Split-Path -Parent $outputPath
if (-not [string]::IsNullOrWhiteSpace($outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}

if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 300) {
    throw 'TimeoutSeconds must be between 1 and 300'
}
if ($MinimumEvents -lt 0 -or $MinimumEvents -gt 10000) {
    throw 'MinimumEvents must be between 0 and 10000'
}

if ($CapabilityProbeOnly) {
    $report.status = 'CAPABILITY_READY'
    $report.minimum_events = 0
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $outputPath -Encoding utf8
    Write-Host 'external_uia_notification_listener=CAPABILITY_READY'
    exit 0
}

if ($RootProcessId -le 0) {
    throw 'RootProcessId must be positive unless CapabilityProbeOnly is used'
}

$root = $null
$recorder = $null
$subscribed = $false
try {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $familyIds = @(Get-ProcessFamilyIds -RootId $RootProcessId)
        $root = Find-UiaRootForProcessFamily -ProcessIds $familyIds
        if ($null -ne $root) {
            $report.process_family_ids = @($familyIds | Sort-Object -Unique)
            $report.subscribed_process_id = [int]$root.Current.ProcessId
            break
        }
        Start-Sleep -Milliseconds 100
    }
    if ($null -eq $root) {
        throw 'Timed out waiting for an externally inspectable process-family UIA root'
    }

    $report.focus_before = Get-FocusIdentity
    $recorder = New-Object AutosportUiaNotificationRecorder
    [System.Windows.Automation.Automation]::AddAutomationEventHandler(
        [System.Windows.Automation.AutomationElement]::NotificationEvent,
        $root,
        [System.Windows.Automation.TreeScope]::Subtree,
        $recorder.Handler
    )
    $subscribed = $true

    $eventDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $eventDeadline) {
        if ($recorder.Records.Count -ge $MinimumEvents) { break }
        Start-Sleep -Milliseconds 50
    }

    $raw = $null
    while ($recorder.Records.TryDequeue([ref]$raw)) {
        $display = [string]$raw.DisplayString
        $record = [ordered]@{
            observed_at_utc = [string]$raw.ObservedAtUtc
            sender_process_id = [int]$raw.SenderProcessId
            sender_automation_id = [string]$raw.SenderAutomationId
            notification_kind = [string]$raw.NotificationKind
            notification_processing = [string]$raw.NotificationProcessing
            activity_id = [string]$raw.ActivityId
            display_string_sha256 = Get-Sha256Text -Value $display
            display_string_utf8_length = [System.Text.Encoding]::UTF8.GetByteCount($display)
        }
        if ($IncludeDisplayString) {
            $record.display_string = $display
        }
        $report.events += $record
    }

    $report.event_count = $report.events.Count
    $report.focus_after = Get-FocusIdentity
    $beforeJson = $report.focus_before | ConvertTo-Json -Compress
    $afterJson = $report.focus_after | ConvertTo-Json -Compress
    $report.focus_changed = ($beforeJson -ne $afterJson)

    if ($report.event_count -lt $MinimumEvents) {
        $report.failures += (
            "observed $($report.event_count) notification events; " +
            "minimum required is $MinimumEvents"
        )
    }
    foreach ($eventRecord in $report.events) {
        if (-not ($report.process_family_ids -contains [int]$eventRecord.sender_process_id)) {
            $report.failures += (
                "notification sender process $($eventRecord.sender_process_id) " +
                'is outside the subscribed process family'
            )
        }
        if ([string]::IsNullOrWhiteSpace([string]$eventRecord.activity_id)) {
            $report.failures += 'notification activity_id is blank'
        }
    }
    if ($report.failures.Count -eq 0) {
        $report.status = 'PASS'
    }
} catch {
    $report.failures += "$($_.Exception.GetType().Name): $($_.Exception.Message)"
} finally {
    if ($subscribed -and $null -ne $root -and $null -ne $recorder) {
        try {
            [System.Windows.Automation.Automation]::RemoveAutomationEventHandler(
                [System.Windows.Automation.AutomationElement]::NotificationEvent,
                $root,
                $recorder.Handler
            )
        } catch {
            $report.failures += "event unsubscribe failed: $($_.Exception.Message)"
            $report.status = 'FAIL'
        }
    }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $outputPath -Encoding utf8
}

if ($report.status -ne 'PASS') {
    Write-Host ($report | ConvertTo-Json -Depth 8)
    exit 1
}
Write-Host "external_uia_notification_listener=PASS events=$($report.event_count)"
exit 0
