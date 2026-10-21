param(
    [int]$RootProcessId = 0,
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [int]$TimeoutSeconds = 10,
    [int]$MinimumEvents = 1,
    [switch]$IncludeDisplayString,
    [switch]$CapabilityProbeOnly,
    [switch]$ListenerWorker
)

$ErrorActionPreference = 'Stop'
# A reused evidence path must never retain a prior PASS when this invocation
# fails before the full listener report exists (invalid parameters, unsupported
# UIAutomationClient surface, or a child process startup failure).
# Publish a non-authoritative FAIL sentinel before touching external UIA.
$outputPath = [System.IO.Path]::GetFullPath($Output)
$outputDirectory = Split-Path -Parent $outputPath
if (-not [string]::IsNullOrWhiteSpace($outputDirectory)) {
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
}
$startupFailure = [ordered]@{
    schema_version = 1
    status = 'FAIL'
    source = 'external_windows_uia_notification_client'
    failures = @('Listener invocation was not qualified')
    # Pre-compiler failures must not inherit an old PASS or omit evidence fields.
    event_count = 0
    events = @()
    real_money_execution = $false
    human_tested = $false
    nvda_verified = $false
    whole_product_complete = $false
}
$startupFailure | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath $outputPath -Encoding utf8

# Reject false evidence claims before loading or compiling managed UIA.
if ($TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 300) {
    throw 'TimeoutSeconds must be between 1 and 300'
}
if ($MinimumEvents -lt 0 -or $MinimumEvents -gt 10000) {
    throw 'MinimumEvents must be between 0 and 10000'
}
if (-not $CapabilityProbeOnly -and $MinimumEvents -eq 0) {
    # A zero-event runtime listen must not mint a PASS-shaped UIA evidence report.
    # CapabilityProbeOnly has its own explicit CAPABILITY_READY evidence class.
    throw 'MinimumEvents must be at least 1 for runtime notification evidence'
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$recorderSource = @'
using System;
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

public sealed class AutosportUiaNotificationBuffer
{
    // CLR-synchronized methods avoid Roslyn's implicit Monitor.Exit intrinsic
    // (CS0656 on the hosted PowerShell 7 / .NET 10 Add-Type path).
    // Every access to _items/_head/_count uses the same instance monitor.
    private AutosportUiaNotificationRecord[] _items = new AutosportUiaNotificationRecord[16];
    private int _head = 0;
    private int _count = 0;

    public int Count
    {
        [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.Synchronized)]
        get { return _count; }
    }

    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.Synchronized)]
    public void Enqueue(AutosportUiaNotificationRecord item)
    {
        if (item == null) throw new ArgumentNullException(nameof(item));
        if (_count == _items.Length)
        {
            var bigger = new AutosportUiaNotificationRecord[checked(_items.Length * 2)];
            for (int i = 0; i < _count; i++)
                bigger[i] = _items[(_head + i) % _items.Length];
            _items = bigger;
            _head = 0;
        }
        _items[(_head + _count) % _items.Length] = item;
        _count++;
    }

    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.Synchronized)]
    public bool TryDequeue(out AutosportUiaNotificationRecord item)
    {
        if (_count == 0) { item = null; return false; }
        item = _items[_head];
        _items[_head] = null;
        _head = (_head + 1) % _items.Length;
        _count--;
        return true;
    }
}

public sealed class AutosportUiaNotificationRecorder
{
    public AutosportUiaNotificationBuffer Records { get; }
        = new AutosportUiaNotificationBuffer();

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

# PowerShell 7 Roslyn compilation requires explicit UIAutomation references;
# loading the assemblies with Add-Type -AssemblyName alone is not sufficient.
# Use the same bounded, fail-closed reference strategy as external_uia_audit.ps1.
$runtimeFacades = @()
if ($PSVersionTable.PSEdition -eq 'Core') {
    $runtimeFacades = @(
        (Join-Path $PSHOME 'System.Runtime.dll'),
        (Join-Path $PSHOME 'System.Threading.dll'),
        (Join-Path $PSHOME 'System.Collections.dll'),
        (Join-Path $PSHOME 'System.Collections.Concurrent.dll')
    )
    foreach ($reference in $runtimeFacades) {
        if (-not (Test-Path -LiteralPath $reference -PathType Leaf)) {
            throw 'Required .NET reference facade for UIA notification listener is unavailable'
        }
    }
}
$uiaReferences = @(
    # Pin the running CLR CoreLib identity for the synchronized event buffer.
    [System.Object].Assembly.Location,
    [System.Windows.Automation.Automation].Assembly.Location,
    [System.Windows.Automation.AutomationElement].Assembly.Location,
    [System.Windows.Automation.NotificationEventArgs].Assembly.Location,
    [System.Array].Assembly.Location
) | Select-Object -Unique
if ($runtimeFacades.Count -gt 0) {
    # -ReferencedAssemblies replaces Add-Type's default .NET reference set.
    # Retain the full trusted CLR reference set for UIA type forwarders.
    # Use the trusted platform assembly manifest for the running CLR: this
    # resolves type-forwarder closure, rather than guessing framework versions.
    # Missing/inconsistent framework metadata must FAIL CLOSED, never PASS.
    $trustedAssemblyManifest = [AppContext]::GetData('TRUSTED_PLATFORM_ASSEMBLIES')
    if (-not ($trustedAssemblyManifest -is [string]) -or
        [string]::IsNullOrWhiteSpace($trustedAssemblyManifest)) {
        throw 'Trusted .NET framework reference manifest for UIA listener is unavailable'
    }
    $trustedFrameworkReferences = @(
        $trustedAssemblyManifest -split [System.IO.Path]::PathSeparator
    )
    if ($trustedFrameworkReferences.Count -lt 20 -or
        $trustedFrameworkReferences.Count -gt 1024) {
        throw 'Trusted .NET framework reference manifest has invalid bounds'
    }
    foreach ($reference in $trustedFrameworkReferences) {
        if (-not [System.IO.Path]::IsPathFullyQualified($reference) -or
            -not (Test-Path -LiteralPath $reference -PathType Leaf)) {
            throw 'Trusted .NET framework reference manifest contains unavailable assembly'
        }
    }
    if (-not @($trustedFrameworkReferences | Where-Object {
        [System.IO.Path]::GetFileName($_) -ceq 'System.Private.CoreLib.dll'
    }).Count) {
        throw 'Trusted .NET framework reference manifest lacks System.Private.CoreLib'
    }
    $uiaReferences = @(
        $uiaReferences + $runtimeFacades + $trustedFrameworkReferences |
            Select-Object -Unique
    )
}
if (-not ('AutosportUiaNotificationRecorder' -as [type])) {
    Add-Type -TypeDefinition $recorderSource -Language CSharp -ReferencedAssemblies $uiaReferences
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

    if ($RootId -le 0) { throw 'Invalid external UIA process family root' }
    # Reuse the bounded process-family strategy in canonical external_uia_audit.ps1.
    # Read only process identity, not paths, arguments, or account data.
    $inventory = @(
        Get-CimInstance -ClassName Win32_Process -Property ProcessId, ParentProcessId -OperationTimeoutSec 5 -ErrorAction Stop
    )
    if ($inventory.Count -gt 8192) {
        throw 'External UIA process inventory exceeded its safety bound'
    }
    $childrenByParent = @{}
    foreach ($entry in $inventory) {
        $parentId = [int]$entry.ParentProcessId
        $childId = [int]$entry.ProcessId
        if ($parentId -le 0 -or $childId -le 0) { continue }
        if (-not $childrenByParent.ContainsKey($parentId)) {
            $childrenByParent[$parentId] = New-Object 'System.Collections.Generic.List[int]'
        }
        $childrenByParent[$parentId].Add($childId)
    }
    $seen = New-Object 'System.Collections.Generic.HashSet[int]'
    $pending = New-Object 'System.Collections.Generic.Queue[int]'
    $pending.Enqueue($RootId)
    while ($pending.Count -gt 0) {
        $currentId = $pending.Dequeue()
        if (-not $seen.Add($currentId)) { continue }
        if ($seen.Count -gt 512) {
            throw 'External UIA process family exceeded its safety bound'
        }
        if ($childrenByParent.ContainsKey($currentId)) {
            foreach ($childId in $childrenByParent[$currentId]) {
                if (-not $seen.Contains($childId)) { $pending.Enqueue($childId) }
            }
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


# UIA calls are foreign COM code: a loop deadline cannot interrupt FromHandle,
# FindAll, event subscribe/unsubscribe or focus inspection if they hang.
# Execute the untrusted UIA operation in an independent killable process.
# Only this supervisor can publish PASS to the requested report path.
if (-not $CapabilityProbeOnly -and -not $ListenerWorker) {
    $workerOutputPath = "$outputPath.worker.$([guid]::NewGuid().ToString('N')).json"
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo.FileName = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    $proc.StartInfo.UseShellExecute = $false
    $proc.StartInfo.CreateNoWindow = $true
    $proc.StartInfo.RedirectStandardOutput = $true
    $proc.StartInfo.RedirectStandardError = $true
    foreach ($arg in @(
        '-NoProfile', '-NonInteractive', '-File', [string]$PSCommandPath,
        '-ListenerWorker', '-RootProcessId', [string]$RootProcessId,
        '-Output', [string]$workerOutputPath,
        '-TimeoutSeconds', [string]$TimeoutSeconds,
        '-MinimumEvents', [string]$MinimumEvents
    )) {
        $proc.StartInfo.ArgumentList.Add([string]$arg)
    }
    if ($IncludeDisplayString) {
        $proc.StartInfo.ArgumentList.Add('-IncludeDisplayString')
    }
    $started = $false
    $report | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $outputPath -Encoding utf8
    # Both normal discovery and listening can each use one bounded interval.
    $budgetMs = [int]((2 * $TimeoutSeconds + 20) * 1000)
    try {
        if (-not $proc.Start()) {
            throw 'UIA child process could not start'
        }
        $started = $true
        # Drain output pipes, but never publish raw child exception details.
        $proc.BeginOutputReadLine()
        $proc.BeginErrorReadLine()
        if (-not $proc.WaitForExit($budgetMs)) {
            try {
                $proc.Kill($true)
                [void]$proc.WaitForExit(2000)
            } catch { }
            throw 'UIA watchdog expired'
        }
        if ($proc.ExitCode -ne 0 -or
            -not [System.IO.File]::Exists($workerOutputPath)) {
            throw 'UIA child did not produce passing evidence'
        }
        $candidate = Get-Content -LiteralPath $workerOutputPath -Raw -Encoding utf8 |
            ConvertFrom-Json -ErrorAction Stop
        if ([int]$candidate.schema_version -ne 1 -or
            $candidate.status -cne 'PASS' -or
            $candidate.source -cne 'external_windows_uia_notification_client' -or
            [int]$candidate.root_process_id -ne $RootProcessId -or
            [int]$candidate.minimum_events -ne $MinimumEvents -or
            [bool]$candidate.include_display_string -ne [bool]$IncludeDisplayString -or
            [int]$candidate.event_count -lt $MinimumEvents -or
            [int]$candidate.event_count -ne @($candidate.events).Count -or
            @($candidate.failures).Count -ne 0 -or
            -not (@($candidate.process_family_ids) -contains $RootProcessId) -or
            $candidate.real_money_execution -ne $false -or
            $candidate.human_tested -ne $false -or
            $candidate.nvda_verified -ne $false -or
            $candidate.whole_product_complete -ne $false) {
            throw 'UIA child report did not satisfy its exact evidence contract'
        }
        $candidate | ConvertTo-Json -Depth 8 |
            Set-Content -LiteralPath $outputPath -Encoding utf8
        Write-Host "external_uia_notification_listener=PASS events=$($candidate.event_count)"
        exit 0
    } catch {
        $report.failures += 'External UIA worker did not finish and qualify within budget'
        $report | ConvertTo-Json -Depth 8 |
            Set-Content -LiteralPath $outputPath -Encoding utf8
        Write-Host 'external_uia_notification_listener=FAIL supervisor'
        exit 1
    } finally {
        if ($started) {
            try {
                if (-not $proc.HasExited) { $proc.Kill($true) }
            } catch { }
        }
        $proc.Dispose()
        Remove-Item -LiteralPath $workerOutputPath -Force -ErrorAction SilentlyContinue
    }
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
$listenStage = 'DISCOVER'
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

    $listenStage = 'SUBSCRIBE'
    $report.focus_before = Get-FocusIdentity
    $recorder = New-Object AutosportUiaNotificationRecorder
    [System.Windows.Automation.Automation]::AddAutomationEventHandler(
        [System.Windows.Automation.AutomationElement]::NotificationEvent,
        $root,
        [System.Windows.Automation.TreeScope]::Subtree,
        $recorder.Handler
    )
    $subscribed = $true
    $listenStage = 'LISTEN'

    $eventDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $eventDeadline) {
        if ($recorder.Records.Count -ge $MinimumEvents) { break }
        Start-Sleep -Milliseconds 50
    }

    $listenStage = 'COLLECT'
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

    $listenStage = 'VERIFY'
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
    # Third-party UIA and CIM exception text can contain private user data.
    $report.failures += "External UIA notification listener failed at stage=$listenStage"
} finally {
    if ($subscribed -and $null -ne $root -and $null -ne $recorder) {
        try {
            [System.Windows.Automation.Automation]::RemoveAutomationEventHandler(
                [System.Windows.Automation.AutomationElement]::NotificationEvent,
                $root,
                $recorder.Handler
            )
        } catch {
            $report.failures += 'External UIA notification listener unsubscribe failed'
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
