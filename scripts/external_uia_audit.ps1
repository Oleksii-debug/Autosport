param(
    [Parameter(Mandatory = $true)]
    [string]$Exe,
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [string]$WorkingDirectory = '',
    [string]$Workspace = '',
    [int]$TimeoutSeconds = 20,
    [string]$ProgressOutput = ''
)

$ErrorActionPreference = 'Stop'
# Start-Job launches a clean PowerShell process.  Hosted Windows run
# 40171001725 completed the real audit but could not publish its final report
# because that worker did not auto-load ConvertTo-Json.  Load the built-in
# utility module before any UIA work so PASS and FAIL evidence always retain
# the same fail-closed JSON publication path.
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
# This file is a bounded diagnostic of the real external audit, NOT audit PASS.
# Never persist UIA names, command lines, paths, environment or user data.
function Write-ExternalUiaProgress {
    param(
        [ValidateSet('START','UIA_ASSEMBLIES_READY','WINDOW_LAUNCHED','NATIVE_WINDOW_OBSERVED','NATIVE_ROOT_NAME_PROBE','SEMANTIC_LOCAL_PROBE','SEMANTIC_LOCAL_ROOT_ID_PROBE','SEMANTIC_LOCAL_DESCENDANT_PROBE','SEMANTIC_FAMILY_PROBE','SEMANTIC_FAMILY_IDS_READY','SEMANTIC_NATIVE_HANDLES_PROBE','SEMANTIC_NATIVE_HANDLES_READY','SEMANTIC_NATIVE_CANDIDATES_EXHAUSTED','SEMANTIC_NATIVE_UIA_PROBE','SEMANTIC_NATIVE_DESCENDANT_PROBE','SEMANTIC_WORKER_START_PROBE','SEMANTIC_WORKER_WAIT_PROBE','SEMANTIC_WORKER_TIMEOUT_PROBE','SEMANTIC_WORKER_RESULT_PROBE','SEMANTIC_DESKTOP_ROOT_PROBE','SEMANTIC_DESKTOP_ROOT_READY','SEMANTIC_DESKTOP_WORKER_START_PROBE','SEMANTIC_DESKTOP_WORKER_WAIT_PROBE','SEMANTIC_DESKTOP_WORKER_TIMEOUT_PROBE','SEMANTIC_DESKTOP_WORKER_RESULT_PROBE','SEMANTIC_DESKTOP_FOCUS_PROBE','SEMANTIC_DESKTOP_FOCUS_READY','SEMANTIC_DESKTOP_FOCUS_PARENT_PROBE','SEMANTIC_DESKTOP_FOCUS_CHILD_PROBE','SEMANTIC_DESKTOP_POINT_PROBE','SEMANTIC_DESKTOP_POINT_READY','SEMANTIC_DESKTOP_POINT_ID_PROBE','SEMANTIC_DESKTOP_POINT_PARENT_PROBE','SEMANTIC_DESKTOP_POINT_CHILD_PROBE','SEMANTIC_DESKTOP_RENDERER_PROBE','SEMANTIC_DESKTOP_RENDERER_READY','SEMANTIC_DESKTOP_ENUM_PROBE','SEMANTIC_DESKTOP_WINDOWS_READY','SEMANTIC_DESKTOP_COUNT_PROBE','SEMANTIC_DESKTOP_ITEM_PROBE','SEMANTIC_WINDOW_PROPERTY_PROBE','SEMANTIC_DESCENDANT_PROBE','SEMANTIC_PRIMARY_DESCENDANT_PROBE','SEMANTIC_CHILD_DESCENDANT_PROBE','SEMANTIC_UIA_OBSERVED','RUNTIME_WITNESS_OBSERVED','DUPLICATE_START_PROCESS_PROBE','DUPLICATE_START_PROCESS_READY','DUPLICATE_PROCESS_FAMILY_PROBE','DUPLICATE_PROCESS_FAMILY_READY','DUPLICATE_UIA_ROOT_PROBE','AUDIT_COMPLETE')]
        [string]$Stage
    )
    if ([string]::IsNullOrWhiteSpace($ProgressOutput)) { return }
    $target = [System.IO.Path]::GetFullPath($ProgressOutput)
    $directory = [System.IO.Path]::GetDirectoryName($target)
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    $temporary = "$target.$PID.tmp"
    try {
        [System.IO.File]::WriteAllText($temporary, $Stage, [System.Text.Encoding]::ASCII)
        [System.IO.File]::Move($temporary, $target, $true)
    } finally {
        if ([System.IO.File]::Exists($temporary)) {
            [System.IO.File]::Delete($temporary)
        }
    }
}
Write-ExternalUiaProgress -Stage 'START'
# A timed-out UIA COM worker poisons this audit process for further provider
# probes. Never retry another HWND or query the stalled provider in cleanup.
$script:semanticWorkerTimedOut = $false
$script:nativeRendererAccessibilityMessageStatus = 'NOT_ATTEMPTED'
$script:nativeRendererAccessibilityRequestAttempted = $false
$script:nativeHostMessageStatus = 'NOT_ATTEMPTED'
$script:nativeHostFocusStatus = 'NOT_ATTEMPTED'
$script:isolatedHostHandleStatus = 'NOT_ATTEMPTED'
$script:isolatedRendererHandleStatus = 'NOT_ATTEMPTED'
$script:failureDiagnosticHostHandle = [IntPtr]::Zero
$script:failureDiagnosticRendererHandle = [IntPtr]::Zero
# Only fixed audit-owned stage codes are retained. Never persist a UIA provider
# exception message, arbitrary COM property, user path, or window title.
$script:lastDesktopWorkerStage = 'NOT_RUN'
# Fixed per-route diagnostics; never expose provider text, HWNDs, PIDs or paths.
$script:lastDesktopWorkerRouteStages = [ordered]@{
    ROOT = 'NOT_RUN'
    POINT = 'NOT_RUN'
    FOCUS = 'NOT_RUN'
    RENDERER = 'NOT_RUN'
}
# Audit-owned fixed failure classification only; never serialize provider text.
$script:semanticFailureClass = 'NOT_OBSERVED'
# Fixed OS-only class-bucket counts reveal whether a provider-backed render
# HWND was even available. These are diagnostic counts, never UIA proof.
$script:nativeSemanticCandidateBuckets = [ordered]@{
    RENDER_WIDGET_VISIBLE = 0
    RENDER_WIDGET_HIDDEN = 0
    CHROME_WIDGET_VISIBLE = 0
    CHROME_WIDGET_HIDDEN = 0
    OTHER_VISIBLE = 0
    OTHER_HIDDEN = 0
    UNKNOWN = 0
}
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName WindowsBase

# PowerShell 7 Add-Type replaces default .NET references with explicit ones.
# The live-region probe resolves runtime Monitor Enter/Exit once via delegates:
# both C# lock lowering and direct Monitor calls require compiler references
# that the Windows PowerShell 7 Roslyn runtime has not consistently resolved.
# The same paired try/finally critical sections remain mandatory; missing runtime
# monitor operations fail closed instead of skipping the UIA gate.
# Do not weaken the UIA gate by ignoring compilation failure.
$runtimeFacades = @()
if ($PSVersionTable.PSEdition -eq 'Core') {
    $runtimeFacades = @(
        (Join-Path $PSHOME 'System.Runtime.dll'),
        (Join-Path $PSHOME 'System.Threading.dll'),
        (Join-Path $PSHOME 'System.Collections.dll')
    )
    foreach ($reference in $runtimeFacades) {
        if (-not (Test-Path -LiteralPath $reference -PathType Leaf)) {
            throw 'Required .NET reference facade for external UIA audit is unavailable'
        }
    }
}
$uiaReferences = @(
    [System.Threading.Monitor].Assembly.Location,
    [System.Windows.Automation.Automation].Assembly.Location,
    [System.Windows.Automation.AutomationElementIdentifiers].Assembly.Location
) | Select-Object -Unique
if ($runtimeFacades.Count -gt 0) {
    $uiaReferences = @($uiaReferences + $runtimeFacades | Select-Object -Unique)
}
Add-Type -ReferencedAssemblies $uiaReferences -TypeDefinition @'
using System;
using System.Windows.Automation;
using System.Runtime.InteropServices;

// OS HWND discovery is candidate selection, not semantic UIA acceptance.
public static class AutosportNativeWindows
{
    private delegate bool EnumWindowsCallback(IntPtr window, IntPtr state);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool EnumWindows(EnumWindowsCallback callback, IntPtr state);
    // GetWindow reads one HWND relationship at a time; unlike recursive
    // EnumChildWindows this cannot enter a provider-wide enumeration callback.
    private const uint GW_HWNDNEXT = 2;
    private const uint GW_CHILD = 5;
    [DllImport("user32.dll")]
    private static extern IntPtr GetWindow(IntPtr window, uint command);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(IntPtr window, System.Text.StringBuilder className, int maxCount);
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [StructLayout(LayoutKind.Sequential)]
    private struct NativeRect
    {
        public int Left, Top, Right, Bottom;
    }
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetWindowRect(IntPtr window, out NativeRect rectangle);
    // A Win32-only candidate point, never a UIA witness. Do not serialize
    // screen coordinates or inspect a foreign-process UIA subtree.
    public static int[] VisibleScreenCenterForHandle(IntPtr window)
    {
        if (window == IntPtr.Zero || !IsWindowVisible(window)) return Array.Empty<int>();
        NativeRect rectangle;
        if (!GetWindowRect(window, out rectangle) ||
            rectangle.Right <= rectangle.Left || rectangle.Bottom <= rectangle.Top)
            return Array.Empty<int>();
        long x = (long)rectangle.Left + ((long)rectangle.Right - rectangle.Left) / 2;
        long y = (long)rectangle.Top + ((long)rectangle.Bottom - rectangle.Top) / 2;
        return new[] { (int)x, (int)y };
    }
    public static int ProcessIdForHandle(IntPtr window)
    {
        uint processId;
        return GetWindowThreadProcessId(window, out processId) == 0 || processId > (uint)int.MaxValue
            ? 0 : (int)processId;
    }
    // The independent WinForms UIA control explicitly activates its window
    // before reading UIA. Make the same bounded OS-only attempt for the real
    // packaged host; foreground ownership is NEVER semantic accessibility.
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();
    public static bool TryClaimForegroundForProcess(IntPtr window, int expectedProcessId)
    {
        if (window == IntPtr.Zero || expectedProcessId <= 0 || !IsWindowVisible(window))
            return false;
        uint ownerPid;
        if (GetWindowThreadProcessId(window, out ownerPid) == 0 ||
            ownerPid != (uint)expectedProcessId)
            return false;
        if (GetForegroundWindow() == window) return true;
        if (!SetForegroundWindow(window)) return false;
        return GetForegroundWindow() == window;
    }
    // Diagnostic only: the real packaged host may accept native Win32 messages
    // while its UIA provider blocks. Query only in-family visible top-level
    // non-Chromium hosts; no HWND, PID, coordinates or user text are persisted.
    private const uint GA_ROOT = 2;
    private const uint WM_NULL = 0;
    [DllImport("user32.dll")]
    private static extern IntPtr GetAncestor(IntPtr window, uint flags);
    [StructLayout(LayoutKind.Sequential)]
    private struct GuiThreadInfo
    {
        public uint cbSize, flags;
        public IntPtr hwndActive, hwndFocus, hwndCapture, hwndMenuOwner, hwndMoveSize, hwndCaret;
        public NativeRect rcCaret;
    }
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetGUIThreadInfo(uint threadId, ref GuiThreadInfo info);
    public static bool IsVisibleFamilyHost(IntPtr window, int[] familyIds)
    {
        return window != IntPtr.Zero && familyIds != null &&
            GetAncestor(window, GA_ROOT) == window &&
            DiagnosticClassBucket(window) == "OTHER_VISIBLE" &&
            Array.IndexOf(familyIds, ProcessIdForHandle(window)) >= 0;
    }
    // Both outputs use fixed vocabulary and are OS-only forensic hints.
    // WM_NULL is bounded; a reply cannot satisfy AutomationId or focus gates.
    public static string[] ProbeVisibleFamilyHost(IntPtr window, int[] familyIds)
    {
        if (!IsVisibleFamilyHost(window, familyIds))
            return new[] { "NOT_ATTEMPTED", "NOT_ATTEMPTED" };
        var owner = ProcessIdForHandle(window);
        IntPtr ignoredReply;
        var dispatched = SendMessageTimeout(
            window, WM_NULL, IntPtr.Zero, IntPtr.Zero,
            SMTO_ABORTIFHUNG, 250, out ignoredReply);
        if (ProcessIdForHandle(window) != owner ||
            !IsVisibleFamilyHost(window, familyIds))
            return new[] { "STALE", "STALE" };
        var messageStatus = dispatched == IntPtr.Zero ? "NO_REPLY" : "RESPONSIVE";
        if (GetForegroundWindow() != window)
            return new[] { messageStatus, "NOT_FOREGROUND" };
        uint currentPid;
        var threadId = GetWindowThreadProcessId(window, out currentPid);
        if (threadId == 0 || currentPid != (uint)owner)
            return new[] { messageStatus, "STALE" };
        var info = new GuiThreadInfo();
        info.cbSize = (uint)Marshal.SizeOf(typeof(GuiThreadInfo));
        if (!GetGUIThreadInfo(threadId, ref info))
            return new[] { messageStatus, "GUI_INFO_UNAVAILABLE" };
        if (info.hwndFocus == IntPtr.Zero)
            return new[] { messageStatus, "FOCUS_NOT_SET" };
        var focusPid = ProcessIdForHandle(info.hwndFocus);
        return new[] { messageStatus,
            Array.IndexOf(familyIds, focusPid) >= 0
                ? "FOCUS_IN_FAMILY" : "FOCUS_FOREIGN" };
    }
    // WebView2 can defer renderer accessibility until a client requests it.
    // Request the standard Win32 OBJID_CLIENT object once on a genuine, visible
    // render widget, before entering a UIA COM provider known to stall on CI.
    // SendMessageTimeout is bounded; the OS reply is NEVER a UIA/ARIA witness.
    private const uint WM_GETOBJECT = 0x003D;
    private const int OBJID_CLIENT = -4;
    private const uint SMTO_ABORTIFHUNG = 0x0002;
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr SendMessageTimeout(
        IntPtr window, uint message, IntPtr wParam, IntPtr lParam,
        uint flags, uint timeoutMs, out IntPtr reply);
    public static string RequestRendererAccessibility(IntPtr window, int[] familyIds)
    {
        if (window == IntPtr.Zero || familyIds == null || familyIds.Length == 0)
            return "NOT_ATTEMPTED";
        var owner = ProcessIdForHandle(window);
        if (owner <= 0 || Array.IndexOf(familyIds, owner) < 0 ||
            DiagnosticClassBucket(window) != "RENDER_WIDGET_VISIBLE")
            return "NOT_ATTEMPTED";
        IntPtr ignoredReply;
        var dispatched = SendMessageTimeout(
            window, WM_GETOBJECT, IntPtr.Zero, new IntPtr(OBJID_CLIENT),
            SMTO_ABORTIFHUNG, 500, out ignoredReply);
        // HWND ownership/class may change while the OS message is in flight.
        // Never retain or expose the returned object token or HWND/PID.
        if (ProcessIdForHandle(window) != owner ||
            DiagnosticClassBucket(window) != "RENDER_WIDGET_VISIBLE")
            return "STALE";
        return dispatched == IntPtr.Zero ? "NO_REPLY" : "DELIVERED";
    }
    // Privacy-bounded OS-only classification BEFORE any UIA COM provider call.
    // Never persist HWND, PID, raw class/title, executable path or user data.
    // These fixed buckets are forensic hints, NEVER accessibility PASS.
    public static string DiagnosticClassBucket(IntPtr window)
    {
        if (window == IntPtr.Zero) return "UNKNOWN";
        var className = new System.Text.StringBuilder(128);
        var length = GetClassName(window, className, className.Capacity);
        if (length <= 0) return "UNKNOWN";
        var name = className.ToString();
        var kind = name.Equals("Chrome_RenderWidgetHostHWND", StringComparison.Ordinal)
            ? "RENDER_WIDGET"
            : name.StartsWith("Chrome_WidgetWin_", StringComparison.Ordinal)
                ? "CHROME_WIDGET" : "OTHER";
        return kind + (IsWindowVisible(window) ? "_VISIBLE" : "_HIDDEN");
    }
    // OS-only top-level HWND census: never enumerate UIA/COM, window titles,
    // command lines or foreign-process descendants. Counts are fixed categories.
    public static int[] TopLevelProcessOwnershipCounts(int launcherId, int[] familyIds)
    {
        if (launcherId <= 0 || familyIds == null ||
            Array.IndexOf(familyIds, launcherId) < 0)
            throw new ArgumentException("Invalid launched process family");
        var counts = new int[4]; // launcher visible/hidden, child visible/hidden
        var matched = 0;
        EnumWindowsCallback callback = (window, state) =>
        {
            uint owner;
            if (GetWindowThreadProcessId(window, out owner) == 0 ||
                owner > (uint)int.MaxValue ||
                Array.IndexOf(familyIds, (int)owner) < 0)
                return true;
            if (++matched > 4096) return false;
            var offset = owner == (uint)launcherId ? 0 : 2;
            counts[offset + (IsWindowVisible(window) ? 0 : 1)]++;
            return true;
        };
        if (!EnumWindows(callback, IntPtr.Zero))
            throw new InvalidOperationException("Top-level HWND census failed or exceeded bound");
        return counts;
    }
    // SetForegroundWindow accepts top-level windows; do not retry child HWNDs
    // from ForProcessFamily, even if those child HWNDs share the launcher PID.
    public static IntPtr[] VisibleTopLevelWindowsForProcess(int launcherId)
    {
        if (launcherId <= 0) return Array.Empty<IntPtr>();
        var windows = new IntPtr[256];
        var count = 0;
        EnumWindowsCallback callback = (window, state) =>
        {
            uint owner;
            if (GetWindowThreadProcessId(window, out owner) == 0 ||
                owner != (uint)launcherId || !IsWindowVisible(window))
                return true;
            if (count >= windows.Length) return false;
            windows[count++] = window;
            return true;
        };
        if (!EnumWindows(callback, IntPtr.Zero))
            throw new InvalidOperationException("Top-level launcher HWND limit exceeded");
        var result = new IntPtr[count];
        Array.Copy(windows, result, count);
        return result;
    }
    // A packaged WebView2 host can be owned by a launched family process,
    // while the launcher has no visible top-level HWND. Activate only a
    // *top-level* non-Chromium host of that exact family, never a renderer
    // widget or a descendant HWND. Native foreground is NOT UIA evidence.
    public static bool TryClaimForegroundForVisibleFamilyHost(int launcherId, int[] familyIds)
    {
        if (launcherId <= 0 || familyIds == null ||
            Array.IndexOf(familyIds, launcherId) < 0)
            return false;
        var windows = new IntPtr[256];
        var count = 0;
        EnumWindowsCallback callback = (window, state) =>
        {
            uint owner;
            if (GetWindowThreadProcessId(window, out owner) == 0 ||
                owner > (uint)int.MaxValue || owner == (uint)launcherId ||
                Array.IndexOf(familyIds, (int)owner) < 0 || !IsWindowVisible(window))
                return true;
            // Exclude Chrome_WidgetWin_ and Chrome_RenderWidgetHostHWND,
            // including any renderer-owned top-level window.
            if (DiagnosticClassBucket(window) != "OTHER_VISIBLE") return true;
            if (count >= windows.Length) return false;
            windows[count++] = window;
            return true;
        };
        if (!EnumWindows(callback, IntPtr.Zero)) return false;
        for (var index = 0; index < count; index++)
        {
            // PID/visibility/class can change after enumeration. Recheck at
            // use; a stale or foreign HWND must never be activated.
            var owner = ProcessIdForHandle(windows[index]);
            if (owner <= 0 || owner == launcherId ||
                Array.IndexOf(familyIds, owner) < 0 ||
                DiagnosticClassBucket(windows[index]) != "OTHER_VISIBLE")
                continue;
            if (TryClaimForegroundForProcess(windows[index], owner))
                return true;
        }
        return false;
    }
    public static IntPtr[] ForProcessFamily(int[] allowedIds)
    {
        if (allowedIds == null || allowedIds.Length == 0) return Array.Empty<IntPtr>();
        // A fixed bound avoids PowerShell Add-Type's forwarded generic List<>
        // compilation failure while preserving the same fail-closed HWND cap.
        var windows = new IntPtr[4096];
        var count = 0;
        EnumWindowsCallback callback = (window, state) =>
        {
            uint processId;
            if (GetWindowThreadProcessId(window, out processId) != 0 &&
                Array.IndexOf(allowedIds, (int)processId) >= 0)
            {
                if (count >= windows.Length) return false;
                windows[count++] = window;
            }
            return true;
        };
        if (!EnumWindows(callback, IntPtr.Zero))
            throw new InvalidOperationException("Native HWND enumeration failed or exceeded limit");
        // The actual WebView2 accessibility provider can live beneath a native
        // parent HWND. Enumerate bounded OS child HWNDs before attempting any
        // potentially blocking UIA subtree calls on the top-level host.
        // An HWND remains only a candidate; later UIA ProcessId/AutomationId,
        // keyboard, live-region and durable STOP witnesses are still required.
        var rootCount = count;
        var scheduled = new IntPtr[4096];
        var visited = new IntPtr[4096];
        Array.Copy(windows, scheduled, rootCount);
        Array.Copy(windows, visited, rootCount);
        var scheduledCount = rootCount;
        var visitedCount = rootCount;
        var probes = 0;
        for (var cursor = 0; cursor < scheduledCount; cursor++)
        {
            // No implicit deep enumeration. Bound the total child/sibling probes
            // and the breadth-first work queue, including foreign-process HWNDs.
            // Fail closed on churn/cycles/cap exhaustion; never truncate to PASS.
            var child = GetWindow(scheduled[cursor], GW_CHILD);
            while (child != IntPtr.Zero)
            {
                if (++probes > visited.Length)
                    throw new InvalidOperationException("Child HWND probe cap exceeded");
                if (Array.IndexOf(visited, child, 0, visitedCount) < 0)
                {
                    if (visitedCount >= visited.Length || scheduledCount >= scheduled.Length)
                        throw new InvalidOperationException("Child HWND enumeration limit exceeded");
                    visited[visitedCount++] = child;
                    scheduled[scheduledCount++] = child;
                    uint processId;
                    if (GetWindowThreadProcessId(child, out processId) != 0 &&
                        Array.IndexOf(allowedIds, (int)processId) >= 0)
                    {
                        if (count >= windows.Length)
                            throw new InvalidOperationException("Native HWND result cap exceeded");
                        windows[count++] = child;
                    }
                }
                child = GetWindow(child, GW_HWNDNEXT);
            }
        }
        var result = new IntPtr[count];
        Array.Copy(windows, result, count);
        Array.Reverse(result); // descendants first, native top-level hosts last
        // An unrelated or hidden native HWND can hang the first real UIA COM
        // provider and make the external semantic gate fail before examining
        // the WebView2 provider. OS-only priority never substitutes for UIA:
        // preserve all discovered candidates, the cap and their stable order
        // within each visibility/class bucket. The MTA still requires a real
        // PID + AutomationId and all keyboard/STOP/live-region evidence.
        var priority = new int[count];
        for (var i = 0; i < count; i++)
        {
            var className = new System.Text.StringBuilder(128);
            var classLength = GetClassName(result[i], className, className.Capacity);
            var name = classLength > 0 ? className.ToString() : "";
            // Windows Candidate 38776077488: a visible Chrome_WidgetWin_
            // blocked real UIA FromHandle. Try the native render-content HWND
            // before the wrapper when available; never prune candidates.
            // Genuine UIA PID/AutomationId, F2/F8, live-region and STOP still gate.
            var renderHost = name.Equals("Chrome_RenderWidgetHostHWND", StringComparison.Ordinal);
            var chromeWidget = name.StartsWith("Chrome_WidgetWin_", StringComparison.Ordinal);
            var visible = IsWindowVisible(result[i]);
            priority[i] = renderHost ? 0 : chromeWidget ? (visible ? 1 : 2)
                : (visible ? 3 : 4);
        }
        var ordered = new IntPtr[count];
        var write = 0;
        for (var bucket = 0; bucket < 5; bucket++)
        {
            for (var i = 0; i < count; i++)
            {
                if (priority[i] == bucket) ordered[write++] = result[i];
            }
        }
        if (write != count)
            throw new InvalidOperationException("Native HWND candidate order incomplete");
        return ordered;
    }
}

public sealed class AutosportExternalLiveRegionProbe : IDisposable
{
    private readonly AutomationElement _element;
    private readonly AutomationEventHandler _handler;
    private readonly object _sync = new object();
    // PowerShell 7 may compile C# against a reduced reference set: resolving
    // Monitor by static C# name fails with CS1069 even when the UIA assembly is
    // valid. Resolve only the runtime's own Monitor Enter/Exit once, fail closed
    // if either method is missing, then retain the same try/finally critical
    // sections and concurrency guarantees as direct Monitor calls.
    private static readonly Action<object> EnterSync = ResolveMonitor("Enter");
    private static readonly Action<object> ExitSync = ResolveMonitor("Exit");

    private static Action<object> ResolveMonitor(string name)
    {
        var monitor = typeof(object).Assembly.GetType(
            "System.Threading.Monitor", throwOnError: true
        );
        var method = monitor.GetMethod(name, new[] { typeof(object) });
        if (method == null) throw new InvalidOperationException("Monitor operation unavailable");
        return (Action<object>)Delegate.CreateDelegate(typeof(Action<object>), method);
    }

    private int _count;
    private string _lastAutomationId = "";
    private string _lastName = "";

    public AutosportExternalLiveRegionProbe(AutomationElement element)
    {
        _element = element ?? throw new ArgumentNullException(nameof(element));
        _handler = OnEvent;
        Automation.AddAutomationEventHandler(
            AutomationElementIdentifiers.LiveRegionChangedEvent,
            _element,
            TreeScope.Element,
            _handler
        );
    }

    private void OnEvent(object sender, AutomationEventArgs eventArgs)
    {
        if (eventArgs == null ||
            eventArgs.EventId != AutomationElementIdentifiers.LiveRegionChangedEvent)
        {
            return;
        }
        try
        {
            var element = sender as AutomationElement;
            if (element == null)
            {
                return;
            }
            var automationId = element.Current.AutomationId ?? "";
            var name = element.Current.Name ?? "";
            EnterSync(_sync);
            try
            {
                _lastAutomationId = automationId;
                _lastName = name;
                _count++;
            }
            finally
            {
                ExitSync(_sync);
            }
        }
        catch (ElementNotAvailableException)
        {
        }
    }

    public int Count
    {
        get
        {
            EnterSync(_sync);
            try { return _count; }
            finally { ExitSync(_sync); }
        }
    }
    public string LastAutomationId
    {
        get
        {
            EnterSync(_sync);
            try { return _lastAutomationId; }
            finally { ExitSync(_sync); }
        }
    }
    public string LastName
    {
        get
        {
            EnterSync(_sync);
            try { return _lastName; }
            finally { ExitSync(_sync); }
        }
    }

    public void Dispose()
    {
        Automation.RemoveAutomationEventHandler(
            AutomationElementIdentifiers.LiveRegionChangedEvent,
            _element,
            _handler
        );
    }
}
'@
Write-ExternalUiaProgress -Stage 'UIA_ASSEMBLIES_READY'

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
    [ordered]@{ key = 'owner_economic_close'; automation_id = '329'; name = 'Закрити економічні межі'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'product_source'; automation_id = 'product-source-select'; name = 'Джерело даних для тривалої симуляційної роботи'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; child_types = @() },
    [ordered]@{ key = 'product_source_save'; automation_id = 'product-source-save'; name = 'Зберегти джерело даних'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'product_runtime_start'; automation_id = 'product-runtime-start'; name = 'Запустити симуляційну роботу'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.Button'; allow_disabled = $true; child_types = @() },
    [ordered]@{ key = 'product_runtime_stop'; automation_id = 'product-runtime-stop'; name = 'Зупинити симуляційну роботу'; required_pattern = $null; require_external_focus = $false; expected_control_type = 'ControlType.Button'; allow_disabled = $true; child_types = @() },
    [ordered]@{ key = 'product_runtime_status'; automation_id = 'product-runtime-status'; name = 'Стан тривалої симуляційної роботи'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; require_value_read_only = $true; child_types = @() },
    [ordered]@{ key = 'emergency_stop'; automation_id = 'emergency-stop-action'; name = 'Активувати аварійний STOP'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'manual_calculation_open'; automation_id = '330'; name = 'Відкрити робочу поверхню ручних розрахунків'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'manual_calculation_operation'; automation_id = '331'; name = 'Операція ручного розрахунку'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.ComboBox'; child_types = @() },
    [ordered]@{ key = 'manual_calculation_input'; automation_id = '332'; name = 'Вхідні значення ручного розрахунку'; required_pattern = 'Value'; require_external_focus = $true; expected_control_type = 'ControlType.Edit'; child_types = @() },
    [ordered]@{ key = 'manual_calculation_calculate'; automation_id = '333'; name = 'Обчислити ручний результат'; required_pattern = 'Action'; require_external_focus = $true; expected_control_type = 'ControlType.Button'; child_types = @() },
    [ordered]@{ key = 'manual_calculation_result'; automation_id = '334'; name = 'Результат і докази ручного розрахунку'; required_pattern = 'Value'; require_external_focus = $false; expected_control_type = 'ControlType.Edit'; require_value_read_only = $true; allow_disabled = $true; child_types = @() },
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
    if ($null -ne $legacyPattern) {
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

    if ($RootProcessId -le 0) {
        throw 'Invalid external UIA root process identifier'
    }
    # Previously this loop issued one CIM query per child, which could block
    # before semantic UIA enumeration. Take a single bounded identity snapshot.
    # Do not query or persist command lines, paths or user data.
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
    $pending.Enqueue($RootProcessId)
    while ($pending.Count -gt 0) {
        $currentId = $pending.Dequeue()
        if (-not $seen.Add($currentId)) { continue }
        if ($seen.Count -gt 512) {
            throw 'External UIA process family exceeded its safety bound'
        }
        if ($childrenByParent.ContainsKey($currentId)) {
            foreach ($childId in $childrenByParent[$currentId]) {
                if (-not $seen.Contains($childId)) {
                    $pending.Enqueue($childId)
                }
            }
        }
    }
    return @($seen | ForEach-Object { [int]$_ })
}

# The duplicate-instance process census is a separate OS/CIM provider call.
# A CIM connection may outlive OperationTimeoutSec, so the real census runs
# in an independently bounded ThreadJob. A timeout or missing family fails
# closed: it can never count as the required native duplicate-dialog witness.
function Get-BoundedDuplicateProcessFamilyIds {
    param([int]$RootProcessId)

    if ($RootProcessId -le 0) {
        throw 'Invalid duplicate-instance process identifier'
    }
    $results = [System.Collections.Concurrent.ConcurrentQueue[object]]::new()
    $completed = [System.Threading.ManualResetEventSlim]::new($false)
    $familyFinder = ${function:Get-ProcessFamilyIds}
    $worker = Start-ThreadJob -ScriptBlock {
        param([int]$ProcessIdValue, [scriptblock]$FindFamily, [object]$Results, [object]$Completion)
        try {
            [int[]]$ids = @(& $FindFamily -RootProcessId $ProcessIdValue)
            $Results.Enqueue([object]$ids)
        } catch {
            # Do not publish CIM/provider errors or partial process inventories.
        } finally {
            $Completion.Set()
        }
    } -ArgumentList @($RootProcessId, $familyFinder, $results, $completed) -ThrottleLimit 16
    if (-not $completed.Wait(6000)) {
        # Stop-Job can block on a stuck provider. The enclosing audit process
        # is independently killed by the real Windows gate watchdog.
        throw 'Second packaged launch bounded process-family probe timed out'
    }
    [object]$observedFamily = $null
    if (-not $results.TryDequeue([ref]$observedFamily)) {
        $completed.Dispose()
        throw 'Second packaged launch process-family probe returned no trusted inventory'
    }
    $completed.Dispose()
    [int[]]$familyIds = [int[]]$observedFamily
    if ($familyIds.Count -eq 0 -or -not ($familyIds -contains $RootProcessId)) {
        throw 'Second packaged launch process-family identity is incomplete'
    }
    return $familyIds
}

# Traverse one UIA children level per provider call. A single recursive Descendants
# FindFirst on the packaged native host blocked the real Windows CI provider for
# >120 seconds (run 38216324196); it must not be used for semantic readiness.
# The *actual* external UIA element is still mandatory for acceptance.
# Real UIA providers can block on FindAll(Children) while building a full
# collection. Walk one raw child/sibling at a time under the existing isolated
# ThreadJob/outer-process watchdog. A missing/blocked node never means PASS.
function Find-UiaSemanticViaChildren {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$AutomationId,
        [DateTime]$Deadline,
        [IntPtr]$NativeWindowHandle = [IntPtr]::Zero,
        [int]$ExpectedProcessId = 0,
        [object]$ResultQueue = $null,
        [object]$CompletionSignal = $null,
        [object]$ProgressQueue = $null
    )
    try {
    if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('WORKER_ENTER') }
    # UI Automation elements must be created in the MTA worker that queries
    # their descendants. Passing the parent-runspace COM element across the
    # ThreadJob boundary can deadlock or invalidate its COM apartment.
    if ($NativeWindowHandle -ne [IntPtr]::Zero) {
        if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('MTA_APARTMENT_CHECK') }
        if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -ne [System.Threading.ApartmentState]::MTA) {
            throw 'External UIA semantic worker requires MTA'
        }
        if ($ExpectedProcessId -le 0) {
            throw 'External UIA semantic worker requires a verified process'
        }
        if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('FROM_HANDLE_ENTER') }
        $Root = [System.Windows.Automation.AutomationElement]::FromHandle($NativeWindowHandle)
        if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('FROM_HANDLE_RETURNED') }
        if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('NATIVE_PID_READ_ENTER') }
        if ($null -eq $Root -or [int]$Root.Current.ProcessId -ne $ExpectedProcessId) {
            throw 'External UIA semantic worker window identity changed'
        }
        if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('NATIVE_PID_VALIDATED') }
    }
    if ($null -eq $Root) { return $null }
    $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
    $pending = [System.Collections.Generic.Queue[System.Windows.Automation.AutomationElement]]::new()
    $pending.Enqueue($Root)
    $visited = 0
    while ($pending.Count -gt 0 -and [DateTime]::UtcNow -lt $Deadline) {
        if ($visited -ge 4096) {
            throw 'External UIA semantic child traversal exceeded its safety bound'
        }
        $node = $pending.Dequeue()
        $visited += 1
        try {
            if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('NODE_ID_READ_ENTER') }
            if ([string]$node.Current.AutomationId -ceq $AutomationId) {
                if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('NODE_ID_MATCH') }
                # Return both native elements from the same bounded MTA apartment.
                if ($NativeWindowHandle -ne [IntPtr]::Zero) {
                    if ($null -ne $ResultQueue) {
                        $ResultQueue.Enqueue([object]([pscustomobject]@{ Root = $Root; Element = $node }))
                        return $null
                    }
                    return [pscustomobject]@{ Root = $Root; Element = $node }
                }
                if ($null -ne $ResultQueue) {
                    $ResultQueue.Enqueue([object]$node)
                    return $null
                }
                return $node
            }
            # A single provider GetFirstChild/GetNextSibling call may itself
            # block, so only the bounded worker may execute this traversal.
            if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('FIRST_CHILD_ENTER') }
            $child = $walker.GetFirstChild($node)
            if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('FIRST_CHILD_RETURNED') }
            $siblingProbes = 0
            while ($null -ne $child -and [DateTime]::UtcNow -lt $Deadline) {
                if ($siblingProbes -ge 4096) {
                    throw 'External UIA semantic sibling traversal exceeded its safety bound'
                }
                if ($pending.Count -ge 4096) {
                    throw 'External UIA semantic child traversal exceeded its queue bound'
                }
                $siblingProbes += 1
                $pending.Enqueue($child)
                if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('NEXT_SIBLING_ENTER') }
                $child = $walker.GetNextSibling($child)
                if ($null -ne $ProgressQueue) { $ProgressQueue.Enqueue('NEXT_SIBLING_RETURNED') }
            }
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            # A stale provider subtree cannot confer semantic readiness.
            continue
        }
    }
    return $null
    } finally {
        # A thread-safe completion signal is independent of the ThreadJob
        # State/Wait-Job locks that may be held by a blocked COM provider.
        if ($null -ne $CompletionSignal) {
            $CompletionSignal.Set()
        }
    }
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

# Windows UIA providers can block forever inside FindAll(Children), even with a
# source-level deadline. Execute each candidate's *real* UIA search in a bounded
# in-process thread job. ThreadJob signals a shared completion event with real AutomationElement references:
# no mocked elements or status markers can satisfy the external audit.
# A timed-out provider thread is intentionally not Stop-Job'd (Stop-Job can
# itself block on the same COM call); the enclosing isolated audit process has
# an independent 120-second watchdog and disposes these bounded workers.
function Find-BoundedUiaSemanticViaChildren {
    param(
        [object]$Root,
        [string]$AutomationId,
        [DateTime]$Deadline,
        [IntPtr]$NativeWindowHandle = [IntPtr]::Zero,
        [int]$ExpectedProcessId = 0
    )
    # The native path intentionally omits Root. Keep its PowerShell binder free
    # of a provider-backed UIA type conversion before the bounded worker starts.
    # Desktop fallback still requires a genuine AutomationElement.
    if ($null -ne $Root -and $Root -isnot [System.Windows.Automation.AutomationElement]) {
        throw 'External UIA semantic root is not an AutomationElement'
    }
    if ($null -eq $Root -and $NativeWindowHandle -eq [IntPtr]::Zero) { return $null }
    $remainingSeconds = [int][Math]::Ceiling(($Deadline - [DateTime]::UtcNow).TotalSeconds)
    if ($remainingSeconds -le 0) { return $null }
    $budgetSeconds = [int][Math]::Min(4, $remainingSeconds)
    # The native path passes only primitive HWND/PID values across runspaces;
    # its UIA object is reacquired in the same MTA worker that performs the
    # traversal. Desktop fallback retains the previous bounded path.
    $workerRoot = if ($NativeWindowHandle -ne [IntPtr]::Zero) { $null } else { $Root }
    # Snapshot OS-only class/visibility BEFORE starting the potentially blocking
    # MTA provider worker. A timed-out worker must not trigger new HWND/UIA calls.
    $nativeCandidateBucket = if ($NativeWindowHandle -ne [IntPtr]::Zero) {
        [AutosportNativeWindows]::DiagnosticClassBucket($NativeWindowHandle)
    } else {
        'NO_NATIVE_HANDLE'
    }
    Write-ExternalUiaProgress -Stage 'SEMANTIC_WORKER_START_PROBE'
    # Pass only primitive HWND/PID plus an in-process concurrent result queue
    # and completion event. Neither Job.State nor Receive-Job can form a
    # bounded wait when the UIA COM provider holds a ThreadJob internal lock.
    $workerResults = [System.Collections.Concurrent.ConcurrentQueue[object]]::new()
    $workerCompleted = [System.Threading.ManualResetEventSlim]::new($false)
    # Fixed stage codes only: never queue window names, paths, or user data.
    $workerProgress = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
    $job = Start-ThreadJob -ScriptBlock ${function:Find-UiaSemanticViaChildren} -ErrorAction Stop -ArgumentList @(
        $workerRoot, $AutomationId, $Deadline, $NativeWindowHandle, $ExpectedProcessId,
        $workerResults, $workerCompleted, $workerProgress
    ) -ThrottleLimit 16
    Write-ExternalUiaProgress -Stage 'SEMANTIC_WORKER_WAIT_PROBE'
    # The kernel completion event never queries the ThreadJob state or
    # synchronously cancels a possibly blocked COM provider. A timed-out worker
    # yields NO semantic evidence and remains owned by the outer isolated
    # 120-second process watchdog.
    $workerTerminal = $workerCompleted.Wait($budgetSeconds * 1000)
    if (-not $workerTerminal) {
        # Never reenter a stalled UIA provider or inspect ThreadJob state.
        # The independent concurrent queue contains only fixed stage tokens.
        $lastWorkerStage = 'NOT_ENTERED'
        $workerStage = $null
        while ($workerProgress.TryDequeue([ref]$workerStage)) {
            $lastWorkerStage = [string]$workerStage
        }
        Write-ExternalUiaProgress -Stage 'SEMANTIC_WORKER_TIMEOUT_PROBE'
        # This is a proven provider stall, not a missing semantic node. Retrying
        # another HWND or entering the desktop fallback can reenter the same
        # blocked COM provider and exhaust the independent 120s watchdog.
        # Keep the hung ThreadJob owned by the isolated audit process; do not
        # synchronously Stop-Job/Remove-Job or treat the timeout as PASS.
        $script:semanticWorkerTimedOut = $true
        $script:semanticFailureClass = 'NATIVE_WORKER_TIMEOUT'
        throw "External UIA semantic worker timed out without a real AutomationId witness; last_worker_stage=$lastWorkerStage; native_class_bucket=$nativeCandidateBucket"
    }
    Write-ExternalUiaProgress -Stage 'SEMANTIC_WORKER_RESULT_PROBE'
    $result = $null
    $hasResult = $workerResults.TryDequeue([ref]$result)
    $workerCompleted.Dispose()
    if (-not $hasResult) { return $null }
    if ($NativeWindowHandle -ne [IntPtr]::Zero) {
        if (
            $result -isnot [pscustomobject] -or
            $result.Root -isnot [System.Windows.Automation.AutomationElement] -or
            $result.Element -isnot [System.Windows.Automation.AutomationElement]
        ) {
            throw 'External UIA native worker returned invalid root/element evidence'
        }
        return $result
    }
    if ($result -isnot [System.Windows.Automation.AutomationElement]) {
        throw 'External UIA semantic worker returned an invalid element'
    }
    # The worker itself still reads the real AutomationId and native PID.
    # Keyboard, live-region, STOP and runtime-witness gates remain mandatory.
    return $result
}

# Differential only: the minimal packaged WebView2 control returned an exact UIA
# AutomationId from a separate pwsh -Mta process while the production
# Start-ThreadJob hung at FocusedElement. Probe the REAL launched product in a
# separately killable MTA process, but never treat this result as audit PASS.
# Only fixed codes cross the boundary; no names, HWNDs, PIDs or provider text.
function Get-IsolatedMtaFocusDiagnostic {
    param([int[]]$ProcessIds)

    if ($null -eq $ProcessIds -or $ProcessIds.Count -eq 0) { return 'NO_PROCESS_FAMILY' }
    $pwshCommand = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($null -eq $pwshCommand) { return 'UNAVAILABLE' }
    $probe = @'
$ErrorActionPreference = 'Stop'
try {
    if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -ne [System.Threading.ApartmentState]::MTA) {
        Write-Output 'APARTMENT_ERROR'; exit 1
    }
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $allowed = [int[]]@($env:AUTOSPORT_UIA_DIAGNOSTIC_PIDS.Split(',') | ForEach-Object {
        [int]::Parse($_, [System.Globalization.CultureInfo]::InvariantCulture)
    })
    $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($null -eq $focused) { Write-Output 'NO_FOCUS'; exit 0 }
    $actualPid = [int]$focused.Current.ProcessId
    if ($allowed -notcontains $actualPid) { Write-Output 'FOREIGN_FOCUS'; exit 0 }
    if ([string]$focused.Current.AutomationId -ceq '330') {
        Write-Output 'EXACT_ID_330'; exit 0
    }
    Write-Output 'IN_FAMILY_OTHER_ID'; exit 0
} catch {
    Write-Output 'PROVIDER_ERROR'; exit 1
}
'@
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = [string]$pwshCommand.Source
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-Mta',
        '-EncodedCommand', [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probe))
    )) {
        [void]$start.ArgumentList.Add($argument)
    }
    $start.Environment['AUTOSPORT_UIA_DIAGNOSTIC_PIDS'] = (@($ProcessIds | Sort-Object -Unique) -join ',')
    $child = [System.Diagnostics.Process]::new()
    $child.StartInfo = $start
    $started = $false
    try {
        $started = $child.Start()
        if (-not $started) { return 'LAUNCH_ERROR' }
        if (-not $child.WaitForExit(6000)) {
            try { $child.Kill($true) } catch {}
            return 'TIMEOUT'
        }
        $code = [string]$child.StandardOutput.ReadToEnd().Trim()
        # A nonzero child exit is never an exact semantic witness.
        if ($child.ExitCode -ne 0) {
            if ($code -cin @('APARTMENT_ERROR', 'PROVIDER_ERROR')) { return $code }
            return 'INVALID_RESULT'
        }
        if ($code -cin @('NO_FOCUS', 'FOREIGN_FOCUS', 'EXACT_ID_330', 'IN_FAMILY_OTHER_ID')) {
            return $code
        }
        return 'INVALID_RESULT'
    } catch {
        return 'LAUNCH_ERROR'
    } finally {
        if ($started) {
            try {
                if (-not $child.HasExited) { $child.Kill($true) }
            } catch {}
        }
        $child.Dispose()
    }
}

# Failure-only stack capture in a bounded independent runspace. Never expose raw
# sampler JSON/stderr, command lines, local variables or arbitrary filenames.
function Get-DuplicatePythonStackDiagnostics {
    param(
        [int[]]$ProcessIds, [int]$RootProcessId,
        [string]$CaptureScript = '', [string]$SourceRoot = ''
    )
    $python = $env:AUTOSPORT_UIA_STACK_PYTHON
    $sampler = $env:AUTOSPORT_UIA_STACK_SAMPLER
    if ([string]::IsNullOrWhiteSpace($CaptureScript)) {
        $CaptureScript = Join-Path $PSScriptRoot 'capture_duplicate_python_diagnostic.py'
    }
    if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
        $SourceRoot = Join-Path $PSScriptRoot '../src/autosport'
    }
    if (
        [string]::IsNullOrWhiteSpace($python) -or
        [string]::IsNullOrWhiteSpace($sampler) -or
        -not (Test-Path -LiteralPath $CaptureScript -PathType Leaf) -or
        -not (Test-Path -LiteralPath $SourceRoot -PathType Container)
    ) {
        return [pscustomobject]@{ status = 'NOT_CONFIGURED'; samples = @() }
    }
    $samples = [System.Collections.Generic.List[object]]::new()
    foreach ($targetId in @($ProcessIds | Where-Object { $_ -ne $RootProcessId } | Sort-Object -Unique | Select-Object -First 2)) {
        $results = [System.Collections.Concurrent.ConcurrentQueue[object]]::new()
        $completion = [System.Threading.ManualResetEventSlim]::new($false)
        $arguments = [object[]]::new(7)
        $arguments[0] = $python
        $arguments[1] = $CaptureScript
        $arguments[2] = $sampler
        $arguments[3] = $SourceRoot
        $arguments[4] = [int]$targetId
        $arguments[5] = $results
        $arguments[6] = $completion
        $job = Start-ThreadJob -ScriptBlock {
            param($Python, $Script, $Sampler, $SourceRoot, [int]$TargetId, $Results, $Completion)
            $capture = $null
            try {
                Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
                $start = [System.Diagnostics.ProcessStartInfo]::new()
                $start.FileName = $Python
                $start.UseShellExecute = $false
                $start.CreateNoWindow = $true
                $start.RedirectStandardOutput = $true
                $start.RedirectStandardError = $true
                foreach ($argument in @($Script, '--sampler', $Sampler, '--pid', [string]$TargetId, '--source-root', $SourceRoot)) {
                    [void]$start.ArgumentList.Add($argument)
                }
                $capture = [System.Diagnostics.Process]::new()
                $capture.StartInfo = $start
                if (-not $capture.Start()) { throw 'Sampler launch unavailable' }
                $outputTask = $capture.StandardOutput.ReadToEndAsync()
                $errorTask = $capture.StandardError.ReadToEndAsync()
                if (-not $capture.WaitForExit(6000)) {
                    try { $capture.Kill($true) } catch {}
                    $Results.Enqueue([object]([pscustomobject]@{ status = 'CAPTURE_TIMEOUT' }))
                    return
                }
                $raw = [string]$outputTask.GetAwaiter().GetResult()
                [void]$errorTask.GetAwaiter().GetResult()
                if ($capture.ExitCode -ne 0 -or $raw.Length -gt 16384) {
                    throw 'Sampler capture unavailable'
                }
                $value = ConvertFrom-Json -InputObject $raw -ErrorAction Stop
                if (
                    $value.evidence_class -cne 'FAILURE_ONLY_PYTHON_STACK_LOCATIONS_NOT_ACCEPTANCE' -or
                    $value.status -cnotin @('CAPTURED','SAMPLER_ERROR','SAMPLER_TIMEOUT','INVALID_PAYLOAD','PAYLOAD_LIMIT','UNAVAILABLE') -or
                    $value.human_tested -ne $false -or
                    $value.nvda_verified -ne $false -or
                    $value.real_money_execution -ne $false
                ) { throw 'Sampler schema unavailable' }
                $Results.Enqueue([object]$value)
            } catch {
                $Results.Enqueue([object]([pscustomobject]@{ status = 'CAPTURE_ERROR' }))
            } finally {
                if ($null -ne $capture) {
                    try { if (-not $capture.HasExited) { $capture.Kill($true) } } catch {}
                    $capture.Dispose()
                }
                $Completion.Set()
            }
        } -ArgumentList $arguments -ThrottleLimit 16
        if (-not $completion.Wait(8000)) {
            [void]$samples.Add([pscustomobject]@{ status = 'WORKER_TIMEOUT' })
            # Do not inspect ThreadJob.State or synchronously stop a stuck worker.
            continue
        }
        [object]$sample = $null
        if ($results.TryDequeue([ref]$sample)) {
            [void]$samples.Add($sample)
        } else {
            [void]$samples.Add([pscustomobject]@{ status = 'EMPTY_CAPTURE' })
        }
        $completion.Dispose()
    }
    return [pscustomobject]@{ status = 'FAILURE_ONLY'; samples = @($samples.ToArray()) }
}

# Diagnostic only: isolate FromHandle in a separately killable MTA process.
# An exact ID here is not a UIA witness for the packaged audit and cannot
# replace the real keyboard, STOP, LiveRegion or runtime gates.
function Get-IsolatedMtaHandleDiagnostic {
    param([IntPtr]$Window, [int[]]$ProcessIds)

    if ($Window -eq [IntPtr]::Zero) { return 'NOT_ATTEMPTED' }
    if ($null -eq $ProcessIds -or $ProcessIds.Count -eq 0) { return 'NO_PROCESS_FAMILY' }
    if ($ProcessIds -notcontains [AutosportNativeWindows]::ProcessIdForHandle($Window)) {
        return 'STALE'
    }
    $pwshCommand = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($null -eq $pwshCommand) { return 'UNAVAILABLE' }
    $probe = @'
$ErrorActionPreference = 'Stop'
try {
    if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -ne [System.Threading.ApartmentState]::MTA) {
        Write-Output 'APARTMENT_ERROR'; exit 1
    }
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $allowed = [int[]]@($env:AUTOSPORT_UIA_DIAGNOSTIC_PIDS.Split(',') | ForEach-Object {
        [int]::Parse($_, [System.Globalization.CultureInfo]::InvariantCulture)
    })
    $handleValue = [long]::Parse(
        $env:AUTOSPORT_UIA_DIAGNOSTIC_HANDLE,
        [System.Globalization.CultureInfo]::InvariantCulture
    )
    $element = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]::new($handleValue))
    if ($null -eq $element) { Write-Output 'NO_ELEMENT'; exit 0 }
    if ($allowed -notcontains [int]$element.Current.ProcessId) {
        Write-Output 'FOREIGN_ELEMENT'; exit 0
    }
    if ([string]$element.Current.AutomationId -ceq '330') {
        Write-Output 'EXACT_ID_330'; exit 0
    }
    Write-Output 'IN_FAMILY_OTHER_ID'; exit 0
} catch {
    Write-Output 'PROVIDER_ERROR'; exit 1
}
'@
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = [string]$pwshCommand.Source
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-Mta',
        '-EncodedCommand', [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probe))
    )) { [void]$start.ArgumentList.Add($argument) }
    $start.Environment['AUTOSPORT_UIA_DIAGNOSTIC_PIDS'] = (@($ProcessIds | Sort-Object -Unique) -join ',')
    $start.Environment['AUTOSPORT_UIA_DIAGNOSTIC_HANDLE'] = [string]$Window.ToInt64()
    $child = [System.Diagnostics.Process]::new()
    $child.StartInfo = $start
    $started = $false
    try {
        $started = $child.Start()
        if (-not $started) { return 'LAUNCH_ERROR' }
        if (-not $child.WaitForExit(2500)) {
            try { $child.Kill($true) } catch {}
            return 'TIMEOUT'
        }
        $code = [string]$child.StandardOutput.ReadToEnd().Trim()
        if ($child.ExitCode -ne 0) {
            if ($code -cin @('APARTMENT_ERROR', 'PROVIDER_ERROR')) { return $code }
            return 'INVALID_RESULT'
        }
        if ($code -cin @('NO_ELEMENT', 'FOREIGN_ELEMENT', 'EXACT_ID_330', 'IN_FAMILY_OTHER_ID')) {
            return $code
        }
        return 'INVALID_RESULT'
    } catch {
        return 'LAUNCH_ERROR'
    } finally {
        if ($started) {
            try { if (-not $child.HasExited) { $child.Kill($true) } } catch {}
        }
        $child.Dispose()
    }
}


# The duplicate-instance dialog is mandatory real UIA evidence, but its provider
# blocked an in-process ThreadJob on Windows Candidate 40172891373.  Probe and
# dismiss that exact native dialog in a separately killable MTA PowerShell
# process.  Only fixed audit-owned codes cross the boundary; process existence,
# HWND discovery, timeout, or any provider exception can never produce PASS.
function Invoke-IsolatedDuplicateDialogProbe {
    param([int[]]$ProcessIds, [IntPtr[]]$WindowHandles = @())

    if ($null -eq $ProcessIds -or $ProcessIds.Count -eq 0) {
        return 'NO_PROCESS_FAMILY'
    }
    $pwshCommand = Get-Command pwsh -ErrorAction SilentlyContinue
    if ($null -eq $pwshCommand) { return 'UNAVAILABLE' }
    $probe = @'
$ErrorActionPreference = 'Stop'
try {
    if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -ne [System.Threading.ApartmentState]::MTA) {
        Write-Output 'APARTMENT_ERROR'; exit 1
    }
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -AssemblyName System.Windows.Forms
    $allowed = [int[]]@($env:AUTOSPORT_UIA_DUPLICATE_PIDS.Split(',') | ForEach-Object {
        [int]::Parse($_, [System.Globalization.CultureInfo]::InvariantCulture)
    })
    if ($allowed.Count -eq 0) {
        Write-Output 'NO_PROCESS_FAMILY'; exit 1
    }
    $automationIdCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        '330'
    )
    # The duplicate process family and its top-level HWNDs were already captured
    # by the parent through bounded OS/CIM census. Reacquire those real windows
    # directly and retain Process.MainWindowHandle as a racing-window fallback.
    # A desktop-wide RootElement.FindAll call blocked beyond this probe's
    # six-second fail-closed budget on hosted Windows even though an HWND existed.
    $nativeHandles = [System.Collections.Generic.HashSet[long]]::new()
    if (-not [string]::IsNullOrWhiteSpace($env:AUTOSPORT_UIA_DUPLICATE_HANDLES)) {
        foreach ($rawHandle in $env:AUTOSPORT_UIA_DUPLICATE_HANDLES.Split(',')) {
            [void]$nativeHandles.Add([long]::Parse(
                $rawHandle, [System.Globalization.CultureInfo]::InvariantCulture
            ))
        }
    }
    foreach ($candidateId in $allowed) {
        try {
            $candidateProcess = Get-Process -Id $candidateId -ErrorAction Stop
            $candidateProcess.Refresh()
            $handle = [IntPtr]$candidateProcess.MainWindowHandle
            if ($handle -ne [IntPtr]::Zero) {
                [void]$nativeHandles.Add($handle.ToInt64())
            }
        } catch {
            # A racing/stale family member is not dialog evidence. The caller
            # retries until the existing outer duplicate deadline expires.
        }
    }
    $windows = New-Object 'System.Collections.Generic.List[object]'
    foreach ($handleValue in $nativeHandles) {
        try {
            $window = [System.Windows.Automation.AutomationElement]::FromHandle(
                [IntPtr]::new($handleValue)
            )
            if (
                $null -ne $window -and
                $allowed -contains [int]$window.Current.ProcessId
            ) {
                [void]$windows.Add($window)
            }
        } catch {}
    }
    $dialog = $null
    foreach ($window in $windows) {
        if ($allowed -notcontains [int]$window.Current.ProcessId) { continue }
        if ([string]$window.Current.AutomationId -ceq '330') {
            Write-Output 'SECOND_SEMANTIC_SURFACE'; exit 1
        }
        $semanticDescendant = $window.FindFirst(
            [System.Windows.Automation.TreeScope]::Descendants,
            $automationIdCondition
        )
        if ($null -ne $semanticDescendant) {
            Write-Output 'SECOND_SEMANTIC_SURFACE'; exit 1
        }
        if (
            [string]$window.Current.Name -ceq
            'Автоспорт — помилка запуску'
        ) {
            if ($null -ne $dialog) {
                Write-Output 'AMBIGUOUS_DIALOG'; exit 1
            }
            $dialog = $window
        }
    }
    if ($null -eq $dialog) {
        Write-Output 'NO_DIALOG'; exit 0
    }
    $names = @()
    $descendants = $dialog.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($item in $descendants) {
        $name = [string]$item.Current.Name
        if (-not [string]::IsNullOrWhiteSpace($name)) { $names += $name }
    }
    $copy = $names -join ' '
    if (
        $copy -notmatch 'уже відкритий' -or
        $copy -notmatch 'Economic і live state не змінено'
    ) {
        Write-Output 'COPY_MISMATCH'; exit 1
    }
    $windowPattern = $null
    if (
        $dialog.TryGetCurrentPattern(
            [System.Windows.Automation.WindowPattern]::Pattern,
            [ref]$windowPattern
        ) -and $null -ne $windowPattern
    ) {
        ([System.Windows.Automation.WindowPattern]$windowPattern).Close()
    } else {
        $dialog.SetFocus()
        [System.Windows.Forms.SendKeys]::SendWait('%{F4}')
    }
    Write-Output 'DIALOG_CLOSED'; exit 0
} catch {
    Write-Output 'PROVIDER_ERROR'; exit 1
}
'@
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = [string]$pwshCommand.Source
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-Mta',
        '-EncodedCommand',
        [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probe))
    )) {
        [void]$start.ArgumentList.Add($argument)
    }
    $start.Environment['AUTOSPORT_UIA_DUPLICATE_PIDS'] = (
        @($ProcessIds | Sort-Object -Unique) -join ','
    )
    $start.Environment['AUTOSPORT_UIA_DUPLICATE_HANDLES'] = (
        @($WindowHandles | ForEach-Object { $_.ToInt64() } | Sort-Object -Unique) -join ','
    )
    $child = [System.Diagnostics.Process]::new()
    $child.StartInfo = $start
    $started = $false
    try {
        $started = $child.Start()
        if (-not $started) { return 'LAUNCH_ERROR' }
        if (-not $child.WaitForExit(6000)) {
            try { $child.Kill($true) } catch {}
            return 'TIMEOUT'
        }
        $code = [string]$child.StandardOutput.ReadToEnd().Trim()
        $known = @(
            'DIALOG_CLOSED', 'NO_DIALOG', 'SECOND_SEMANTIC_SURFACE',
            'AMBIGUOUS_DIALOG', 'COPY_MISMATCH', 'APARTMENT_ERROR',
            'NO_PROCESS_FAMILY', 'PROVIDER_ERROR'
        )
        if ($known -cnotcontains $code) { return 'INVALID_RESULT' }
        if ($child.ExitCode -ne 0 -and $code -cin @('DIALOG_CLOSED', 'NO_DIALOG')) {
            return 'INVALID_RESULT'
        }
        if ($child.ExitCode -eq 0 -and $code -cnotin @('DIALOG_CLOSED', 'NO_DIALOG')) {
            return 'INVALID_RESULT'
        }
        return $code
    } catch {
        return 'LAUNCH_ERROR'
    } finally {
        if ($started) {
            try { if (-not $child.HasExited) { $child.Kill($true) } } catch {}
        }
        $child.Dispose()
    }
}

function Find-UiaRootWithElementForProcessFamily {
    param(
        [int[]]$ProcessIds,
        [string]$AutomationId,
        [int]$RootProcessId,
        [DateTime]$Deadline
    )

    if ($null -eq $ProcessIds -or $ProcessIds.Count -eq 0) { return $null }
    # Enumerate candidate top-level HWNDs via the OS process family rather than
    # blocking on the UIA desktop collection Count (38239442361).
    # A window handle NEVER constitutes semantic readiness without the real
    # provider exposing the expected AutomationId and process identity.
    Write-ExternalUiaProgress -Stage 'SEMANTIC_NATIVE_HANDLES_PROBE'
    $nativeWindows = [AutosportNativeWindows]::ForProcessFamily($ProcessIds)
    # Gather only seven fixed OS metadata buckets; never persist HWND, raw
    # class names, PIDs, titles, paths or anything returned by the UIA provider.
    # The latest runner timed out in desktop FindFirst: this distinguishes an
    # absent native render candidate from an actual native-provider failure.
    $bucketCounts = [ordered]@{
        RENDER_WIDGET_VISIBLE = 0
        RENDER_WIDGET_HIDDEN = 0
        CHROME_WIDGET_VISIBLE = 0
        CHROME_WIDGET_HIDDEN = 0
        OTHER_VISIBLE = 0
        OTHER_HIDDEN = 0
        UNKNOWN = 0
    }
    # A visible render HWND is only an OS-owned location candidate. The
    # subsequent FromPoint call must return a genuine in-family UIA element.
    [int[]]$pointCoordinates = @()
    [IntPtr]$nativeHostHandle = [IntPtr]::Zero
    [IntPtr]$nativeRendererHandle = [IntPtr]::Zero
    foreach ($candidateHandle in $nativeWindows) {
        if (
            $nativeHostHandle -eq [IntPtr]::Zero -and
            [AutosportNativeWindows]::IsVisibleFamilyHost($candidateHandle, [int[]]$ProcessIds)
        ) { $nativeHostHandle = $candidateHandle }
        $classBucket = [string][AutosportNativeWindows]::DiagnosticClassBucket($candidateHandle)
        if (-not $bucketCounts.Contains($classBucket)) { $classBucket = 'UNKNOWN' }
        $bucketCounts[$classBucket] = [int]$bucketCounts[$classBucket] + 1
        if ($classBucket -eq 'RENDER_WIDGET_VISIBLE' -and -not $script:nativeRendererAccessibilityRequestAttempted) {
            # A stale/foreign HWND must not permit another request. The entire
            # candidate set gets at most ONE finite OS message attempt.
            $script:nativeRendererAccessibilityRequestAttempted = $true
            # Bounded WM_GETOBJECT requests the actual renderer's accessibility
            # provider; DELIVERED is diagnostic only, never semantic PASS.
            $script:nativeRendererAccessibilityMessageStatus = [AutosportNativeWindows]::RequestRendererAccessibility(
                $candidateHandle, [int[]]$ProcessIds
            )
        }
        if ($classBucket -eq 'RENDER_WIDGET_VISIBLE' -and $nativeRendererHandle -eq [IntPtr]::Zero) {
            $nativeRendererHandle = $candidateHandle
        }
        if ($classBucket -eq 'RENDER_WIDGET_VISIBLE' -and $pointCoordinates.Count -eq 0) {
            $pointCoordinates = [AutosportNativeWindows]::VisibleScreenCenterForHandle($candidateHandle)
        }
    }
    $script:nativeSemanticCandidateBuckets = $bucketCounts
    # Only bounded OS preparation belongs before acceptance. The separately
    # killable FromHandle differentials are optional and can still perturb the
    # live provider after their client times out. Cache primitive HWNDs only;
    # run those diagnostics after a proven gate failure, never before the real
    # semantic/duplicate/keyboard/STOP/LiveRegion sequence.
    $script:failureDiagnosticHostHandle = $nativeHostHandle
    $script:failureDiagnosticRendererHandle = $nativeRendererHandle
    if ($nativeHostHandle -ne [IntPtr]::Zero) {
        $hostProbe = [AutosportNativeWindows]::ProbeVisibleFamilyHost(
            $nativeHostHandle, [int[]]$ProcessIds
        )
        $script:nativeHostMessageStatus = [string]$hostProbe[0]
        $script:nativeHostFocusStatus = [string]$hostProbe[1]
    }
    Write-ExternalUiaProgress -Stage 'SEMANTIC_NATIVE_HANDLES_READY'
    foreach ($nativeHandle in $nativeWindows) {
        if ([DateTime]::UtcNow -ge $Deadline) { break }
        try {
            Write-ExternalUiaProgress -Stage 'SEMANTIC_NATIVE_UIA_PROBE'
            # Candidate PID comes only from Win32, without UIA COM on the
            # calling runspace. Real PID/AutomationId UIA proof remains inside
            # the bounded MTA worker; a missing/hung provider stays FAILURE.
            $nativeProcessId = [AutosportNativeWindows]::ProcessIdForHandle($nativeHandle)
            if (-not ($ProcessIds -contains $nativeProcessId)) { continue }
            # Windows Candidate 38839881777: the real Chrome_WidgetWin_
            # UIA provider blocked inside AutomationElement.FromHandle. A
            # native wrapper HWND is never semantic accessibility evidence.
            # Route this class to the already bounded, genuine desktop MTA
            # ProcessId/AutomationId search below, not to the blocked native
            # FromHandle worker. No real witness still means FAIL.
            $nativeBucket = [AutosportNativeWindows]::DiagnosticClassBucket($nativeHandle)
            if ($nativeBucket -in @('CHROME_WIDGET_VISIBLE', 'CHROME_WIDGET_HIDDEN')) {
                continue
            }
            # Windows Candidate 38850620735 stalled at FROM_HANDLE_ENTER
            # on OTHER_VISIBLE. Only render-content HWNDs remain direct native
            # UIA candidates; unrelated/unknown wrappers defer to the bounded
            # real desktop MTA provider. Missing semantic evidence means FAIL.
            if ($nativeBucket -in @('OTHER_VISIBLE', 'OTHER_HIDDEN', 'UNKNOWN')) {
                continue
            }
            Write-ExternalUiaProgress -Stage 'SEMANTIC_NATIVE_DESCENDANT_PROBE'
            $nativeSemantic = Find-BoundedUiaSemanticViaChildren -AutomationId $AutomationId -Deadline $Deadline -NativeWindowHandle $nativeHandle -ExpectedProcessId $nativeProcessId
            if ($null -ne $nativeSemantic) {
                return $nativeSemantic
            }
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            # A stale or reused HWND is not a valid accessibility witness.
            continue
        }
    }
    # A native HWND is only a candidate. Preserve an independent diagnostic
    # boundary before entering any fallback UIA COM call. No stage is PASS.
    Write-ExternalUiaProgress -Stage 'SEMANTIC_NATIVE_CANDIDATES_EXHAUSTED'
    # Real desktop UIA is still mandatory if a native HWND cannot provide the
    # semantic root. The previous direct caller-runspace PropertyCondition
    # blocked >120s. Search the REAL scoped UIA provider in a bounded MTA job.
    $remainingSeconds = [int][Math]::Ceiling(($Deadline - [DateTime]::UtcNow).TotalSeconds)
    if ($remainingSeconds -le 0) {
        # A native candidate consumed the remaining launch deadline. This is
        # missing UIA proof, not an unknown COM exception or an audit PASS.
        $script:semanticFailureClass = 'DESKTOP_BUDGET_EXHAUSTED'
        return $null
    }
    $budgetSeconds = [int][Math]::Min(4, $remainingSeconds)
    # Run independent genuine UIA routes in separate bounded MTA workers.
    # A blocked FocusedElement must not prevent FromPoint or process-scoped
    # RootElement from producing their own real AutomationId/PID witness.
    # No native HWND, OS focus or diagnostic marker can satisfy acceptance.
    $workerResults = [System.Collections.Concurrent.ConcurrentQueue[object]]::new()
    $workerStates = @()
    $pointX = [int]::MinValue
    $pointY = [int]::MinValue
    if ($pointCoordinates.Count -eq 2) {
        $pointX = [int]$pointCoordinates[0]
        $pointY = [int]$pointCoordinates[1]
    }
    # The exact-head Windows gate observed a real in-family renderer FromHandle
    # result while host/desktop routes stalled. Try the renderer subtree as a
    # fourth independent genuine UIA route; native identity alone never passes.
    $rendererProcessId = 0
    if ($nativeRendererHandle -ne [IntPtr]::Zero) {
        $rendererProcessId = [AutosportNativeWindows]::ProcessIdForHandle($nativeRendererHandle)
    }
    $rendererCandidateValid = (
        $nativeRendererHandle -ne [IntPtr]::Zero -and
        $rendererProcessId -gt 0 -and
        ($ProcessIds -contains $rendererProcessId)
    )
    $processIdsCsv = (@($ProcessIds | Sort-Object -Unique) -join ',')
    $childSearchScript = ${function:Find-UiaSemanticViaChildren}
    Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_WORKER_START_PROBE'
    foreach ($workerMode in @('ROOT', 'POINT', 'FOCUS', 'RENDERER')) {
    if ($workerMode -eq 'RENDERER' -and -not $rendererCandidateValid) { continue }
    $workerCompleted = [System.Threading.ManualResetEventSlim]::new($false)
    $workerProgress = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
    $desktopJob = Start-ThreadJob -ScriptBlock {
        param(
            [string]$Mode, [string]$ProcessIdsCsv, [string]$AutomationId, [int]$RootProcessId,
            [int]$PointX, [int]$PointY, [IntPtr]$RendererHandle,
            [int]$RendererProcessId, [DateTime]$Deadline,
            [object]$ResultQueue, [object]$CompletionSignal,
            [object]$ProgressQueue, [scriptblock]$ChildSearchScript
        )
        function Write-ExternalUiaProgress {
            param([string]$Stage)
            $ProgressQueue.Enqueue($Stage)
        }
        function Find-BoundedUiaSemanticViaChildren {
            param(
                $Root, [string]$AutomationId, [DateTime]$Deadline,
                [IntPtr]$NativeWindowHandle = [IntPtr]::Zero,
                [int]$ExpectedProcessId = 0
            )
            # The real RawView walker runs only inside this bounded MTA worker.
            & $ChildSearchScript -Root $Root -AutomationId $AutomationId -Deadline $Deadline -NativeWindowHandle $NativeWindowHandle -ExpectedProcessId $ExpectedProcessId
        }
        function New-BoundedUiaPair {
            param($Window, $Element)
            if ([object]::ReferenceEquals($Window, $Element)) {
                return [pscustomobject]@{ Root = $window; Element = $window }
            }
            return [pscustomobject]@{ Root = $window; Element = $element }
        }
        try {
            if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -ne [System.Threading.ApartmentState]::MTA) {
                throw 'External desktop UIA semantic worker requires MTA'
            }
            $ProcessIds = [int[]]@($ProcessIdsCsv.Split(',') | ForEach-Object {
                [int]::Parse($_, [System.Globalization.CultureInfo]::InvariantCulture)
            })
        # Exact-head Windows Candidate 40049683170 blocks in FromPoint even
        # after native foreground CLAIMED and WM_GETOBJECT DELIVERED. Give the
        # independent genuine FocusedElement/RawView path a chance first.
        # A prior focus attempt blocked before renderer initialization; this
        # attempt follows the verified bounded renderer accessibility request.
        # Never infer success from OS focus or HWND; require genuine in-family ID.
        if ($Mode -eq 'RENDERER') {
        Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_RENDERER_PROBE'
        if (
            $RendererHandle -ne [IntPtr]::Zero -and
            $RendererProcessId -gt 0 -and
            ($ProcessIds -contains $RendererProcessId)
        ) {
            $rendererPair = Find-BoundedUiaSemanticViaChildren -Root $null -AutomationId $AutomationId -Deadline $Deadline -NativeWindowHandle $RendererHandle -ExpectedProcessId $RendererProcessId
            Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_RENDERER_READY'
            if ($null -ne $rendererPair) {
                if (
                    $rendererPair -isnot [pscustomobject] -or
                    $rendererPair.Root -isnot [System.Windows.Automation.AutomationElement] -or
                    $rendererPair.Element -isnot [System.Windows.Automation.AutomationElement] -or
                    [string]$rendererPair.Element.Current.AutomationId -cne $AutomationId -or
                    -not ($ProcessIds -contains [int]$rendererPair.Element.Current.ProcessId) -or
                    [int]$rendererPair.Root.Current.ProcessId -ne $RendererProcessId
                ) {
                    throw 'External UIA renderer candidate failed genuine semantic identity'
                }
                $ResultQueue.Enqueue([object]$rendererPair)
                return
            }
        }
        }
        if ($Mode -eq 'FOCUS') {
        Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_FOCUS_PROBE'
        $focusedElement = [System.Windows.Automation.AutomationElement]::FocusedElement
        Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_FOCUS_READY'
        if ($null -ne $focusedElement) {
            $focusedPid = [int]$focusedElement.Current.ProcessId
            if ($ProcessIds -contains $focusedPid) {
                $ancestorWalker = [System.Windows.Automation.TreeWalker]::RawViewWalker
                $focusedRoot = $focusedElement
                $ancestorHops = 0
                while ($ancestorHops -lt 64 -and [DateTime]::UtcNow -lt $Deadline) {
                    $ancestorHops += 1
                    Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_FOCUS_PARENT_PROBE'
                    $ancestor = $ancestorWalker.GetParent($focusedRoot)
                    if ($null -eq $ancestor) { break }
                    if (-not ($ProcessIds -contains [int]$ancestor.Current.ProcessId)) { break }
                    $focusedRoot = $ancestor
                }
                if ($ancestorHops -ge 64) {
                    throw 'External UIA focused ancestor traversal exceeded its bound'
                }
                Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_FOCUS_CHILD_PROBE'
                $focusSemantic = Find-BoundedUiaSemanticViaChildren -Root $focusedRoot -AutomationId $AutomationId -Deadline $Deadline
                if ($null -ne $focusSemantic) {
                    # The child walker must have read the genuine AutomationId.
                    # Reverify exact ID and OS-observed process-family ownership
                    # before returning the same UIA root/element pair.
                    if (
                        [string]$focusSemantic.Current.AutomationId -cne $AutomationId -or
                        -not ($ProcessIds -contains [int]$focusSemantic.Current.ProcessId)
                    ) {
                        throw 'External UIA focused candidate failed semantic identity'
                    }
                    $ResultQueue.Enqueue([object](New-BoundedUiaPair -Window $focusedRoot -Element $focusSemantic))
                    return
                }
            }
        }
        }
        # An independent MTA worker runs each alternative, so a stalled
        # FocusedElement cannot starve the process-scoped or point route.
        # After an unsuccessful real focused-element probe, try the independent
        # real UIA render-center path. This may still block in the provider;
        # the isolated worker timeout must remain FAIL, never a synthetic ID.
        if ($Mode -eq 'POINT') {
        if ($PointX -ne [int]::MinValue -and $PointY -ne [int]::MinValue) {
            Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_POINT_PROBE'
            $pointElement = [System.Windows.Automation.AutomationElement]::FromPoint(
                [System.Windows.Point]::new([double]$PointX, [double]$PointY)
            )
            Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_POINT_READY'
            if ($null -ne $pointElement) {
                $pointPid = [int]$pointElement.Current.ProcessId
                if ($ProcessIds -contains $pointPid) {
                    $pointWalker = [System.Windows.Automation.TreeWalker]::RawViewWalker
                    $pointRoot = $pointElement
                    $pointHops = 0
                    while ($pointHops -lt 64 -and [DateTime]::UtcNow -lt $Deadline) {
                        $pointHops += 1
                        Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_POINT_ID_PROBE'
                        if ([string]$pointRoot.Current.AutomationId -ceq $AutomationId) {
                            $ResultQueue.Enqueue([object](New-BoundedUiaPair -Window $pointRoot -Element $pointRoot))
                            return
                        }
                        Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_POINT_PARENT_PROBE'
                        $pointParent = $pointWalker.GetParent($pointRoot)
                        if ($null -eq $pointParent) { break }
                        if (-not ($ProcessIds -contains [int]$pointParent.Current.ProcessId)) { break }
                        $pointRoot = $pointParent
                    }
                    if ($pointHops -ge 64) {
                        throw 'External UIA point ancestor traversal exceeded its bound'
                    }
                    if ([DateTime]::UtcNow -lt $Deadline) {
                        Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_POINT_CHILD_PROBE'
                        $pointSemantic = Find-BoundedUiaSemanticViaChildren -Root $pointRoot -AutomationId $AutomationId -Deadline $Deadline
                        if ($null -ne $pointSemantic) {
                            if (
                                [string]$pointSemantic.Current.AutomationId -cne $AutomationId -or
                                -not ($ProcessIds -contains [int]$pointSemantic.Current.ProcessId)
                            ) {
                                throw 'External UIA point candidate failed semantic identity'
                            }
                            $ResultQueue.Enqueue([object](New-BoundedUiaPair -Window $pointRoot -Element $pointSemantic))
                            return
                        }
                    }
                }
            }
        }
        }
        if ($Mode -eq 'ROOT') {
        Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_ROOT_PROBE'
        $desktop = [System.Windows.Automation.AutomationElement]::RootElement
        Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_ROOT_READY'
        # The unfiltered desktop child enumeration succeeded on Windows CI, but
        # reading ProcessId on a foreign top-level UIA provider then blocked the
        # independent watchdog (38222339505, SEMANTIC_WINDOW_PROPERTY_PROBE).
        # Ask the real UIA provider for exact OS-process-family windows first.
        # Never enumerate foreign window properties or treat a process match as
        # semantic acceptance: a real automation_id=330 remains mandatory.
        foreach ($candidateProcessId in @($ProcessIds | Sort-Object -Unique)) {
            if ([DateTime]::UtcNow -ge $Deadline) { break }
            if ([int]$candidateProcessId -le 0) { continue }
            $processCondition = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
                [int]$candidateProcessId
            )
            Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_ENUM_PROBE'
            # Candidate 38861632532 stalled at desktop FindAll collection.
            # FindFirst fetches only one process-scoped window in the same bounded
            # MTA worker. A missing/blocked real semantic witness remains FAIL.
            $firstWindow = $desktop.FindFirst(
                [System.Windows.Automation.TreeScope]::Children,
                $processCondition
            )
            Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_WINDOWS_READY'
            $windows = @()
            if ($null -ne $firstWindow) { $windows = @($firstWindow) }
            # PowerShell's implicit AutomationElementCollection enumerator can
            # enter a synchronous UIA provider before the loop body. Use indexed
            # access so the independent watchdog can distinguish collection
            # count, item retrieval and actual window property/child traversal.
            # No UIA result is inferred from a successful enumeration.
            Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_COUNT_PROBE'
            $windowCount = $windows.Count
            for ($windowIndex = 0; $windowIndex -lt $windowCount; $windowIndex++) {
                if ([DateTime]::UtcNow -ge $Deadline) { break }
                Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_ITEM_PROBE'
                $window = $windows[$windowIndex]
                Write-ExternalUiaProgress -Stage 'SEMANTIC_WINDOW_PROPERTY_PROBE'
                if ([string]$window.Current.AutomationId -eq $AutomationId) {
                    $ResultQueue.Enqueue([object](New-BoundedUiaPair -Window $window -Element $window))
                    return
                }
                Write-ExternalUiaProgress -Stage 'SEMANTIC_DESCENDANT_PROBE'
                if ([int]$candidateProcessId -eq $RootProcessId) {
                    Write-ExternalUiaProgress -Stage 'SEMANTIC_PRIMARY_DESCENDANT_PROBE'
                } else {
                    Write-ExternalUiaProgress -Stage 'SEMANTIC_CHILD_DESCENDANT_PROBE'
                }
                $element = Find-BoundedUiaSemanticViaChildren -Root $window -AutomationId $AutomationId -Deadline $Deadline
                if ($null -ne $element) {
                    $ResultQueue.Enqueue([object](New-BoundedUiaPair -Window $window -Element $element))
                    return
                }
            }
        }
        }
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            # Stale UIA provider data cannot confer semantic readiness.
        } finally {
            $CompletionSignal.Set()
        }
    } -ArgumentList @(
        $workerMode, $processIdsCsv, $AutomationId, $RootProcessId, $pointX, $pointY,
        $nativeRendererHandle, $rendererProcessId, $Deadline,
        $workerResults, $workerCompleted, $workerProgress, $childSearchScript
    ) -ThrottleLimit 16
    $workerStates += [pscustomobject]@{
        Mode = $workerMode
        Completion = $workerCompleted
        Progress = $workerProgress
        Job = $desktopJob
    }
    }
    Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_WORKER_WAIT_PROBE'
    $workerDeadline = [DateTime]::UtcNow.AddSeconds($budgetSeconds)
    if ($Deadline -lt $workerDeadline) { $workerDeadline = $Deadline }
    $result = $null
    # Never wait synchronously on one possibly blocked UIA worker. Poll only
    # thread-safe result queues and completion events; the outer process is
    # the final watchdog for hung COM providers.
    while ([DateTime]::UtcNow -lt $workerDeadline) {
        if ($workerResults.TryDequeue([ref]$result)) { break }
        $allCompleted = $true
        foreach ($state in $workerStates) {
            if (-not $state.Completion.Wait(0)) {
                $allCompleted = $false
                break
            }
        }
        if ($allCompleted) { break }
        Start-Sleep -Milliseconds 25
    }
    if ($null -eq $result) {
        [void]$workerResults.TryDequeue([ref]$result)
    }
    if ($null -ne $result) {
        Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_WORKER_RESULT_PROBE'
        if (
            $result -isnot [pscustomobject] -or
            $result.Root -isnot [System.Windows.Automation.AutomationElement] -or
            $result.Element -isnot [System.Windows.Automation.AutomationElement]
        ) {
            throw 'External desktop UIA semantic worker returned invalid evidence'
        }
        return $result
    }
    $unfinished = @($workerStates | Where-Object { -not $_.Completion.Wait(0) })
    if ($unfinished.Count -gt 0) {
        # Snapshot all independently launched routes before any result is lost.
        # The old single-stage report selected the first unfinished route,
        # concealing whether POINT and FOCUS also stalled on real UIA calls.
        $allowedDesktopWorkerStages = @(
            'NOT_ENTERED',
            'SEMANTIC_DESKTOP_RENDERER_PROBE',
            'SEMANTIC_DESKTOP_RENDERER_READY',
            'SEMANTIC_DESKTOP_POINT_PROBE',
            'SEMANTIC_DESKTOP_POINT_READY',
            'SEMANTIC_DESKTOP_POINT_ID_PROBE',
            'SEMANTIC_DESKTOP_POINT_PARENT_PROBE',
            'SEMANTIC_DESKTOP_POINT_CHILD_PROBE',
            'SEMANTIC_DESKTOP_FOCUS_PROBE',
            'SEMANTIC_DESKTOP_FOCUS_READY',
            'SEMANTIC_DESKTOP_FOCUS_PARENT_PROBE',
            'SEMANTIC_DESKTOP_FOCUS_CHILD_PROBE',
            'SEMANTIC_DESKTOP_ROOT_PROBE',
            'SEMANTIC_DESKTOP_ROOT_READY',
            'SEMANTIC_DESKTOP_ENUM_PROBE',
            'SEMANTIC_DESKTOP_WINDOWS_READY',
            'SEMANTIC_DESKTOP_COUNT_PROBE',
            'SEMANTIC_DESKTOP_ITEM_PROBE',
            'SEMANTIC_WINDOW_PROPERTY_PROBE',
            'SEMANTIC_DESCENDANT_PROBE',
            'SEMANTIC_PRIMARY_DESCENDANT_PROBE',
            'SEMANTIC_CHILD_DESCENDANT_PROBE'
        )
        $lastWorkerStage = 'NOT_ENTERED'
        foreach ($state in $workerStates) {
            $routeStage = 'NOT_ENTERED'
            $workerStage = $null
            while ($state.Progress.TryDequeue([ref]$workerStage)) {
                $routeStage = [string]$workerStage
            }
            if ($state.Completion.Wait(0)) {
                $script:lastDesktopWorkerRouteStages[$state.Mode] = 'COMPLETED'
            } else {
                $safeStage = if ($allowedDesktopWorkerStages -ccontains $routeStage) {
                    $routeStage
                } else {
                    'UNKNOWN_FIXED_STAGE'
                }
                $script:lastDesktopWorkerRouteStages[$state.Mode] = $safeStage
                if ($lastWorkerStage -eq 'NOT_ENTERED') { $lastWorkerStage = $safeStage }
            }
        }
        Write-ExternalUiaProgress -Stage 'SEMANTIC_DESKTOP_WORKER_TIMEOUT_PROBE'
        $script:lastDesktopWorkerStage = if ($allowedDesktopWorkerStages -ccontains $lastWorkerStage) {
            $lastWorkerStage
        } else {
            'UNKNOWN_FIXED_STAGE'
        }
        $script:semanticWorkerTimedOut = $true
        $script:semanticFailureClass = 'DESKTOP_WORKER_TIMEOUT'
        # Do not Stop-Job, Remove-Job or reenter a stalled provider. The outer
        # Windows audit process owns termination. A timeout without an actual
        # semantic witness is always FAIL.
        throw "External desktop UIA worker timed out without a real semantic witness; last_desktop_stage=$lastWorkerStage"
    }
    foreach ($state in $workerStates) { $state.Completion.Dispose() }
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

function Wait-ForFocusedAutomationId {
    param(
        [int]$RootProcessId,
        [string]$AutomationId,
        [DateTime]$Deadline
    )

    if ($RootProcessId -le 0) {
        throw 'Focused UIA witness requires a valid launched Autosport process'
    }
    $familyIds = @()
    $nextFamilyRefresh = [DateTime]::MinValue
    while ([DateTime]::UtcNow -lt $Deadline) {
        $now = [DateTime]::UtcNow
        if ($now -ge $nextFamilyRefresh) {
            $familyIds = @(Get-ProcessFamilyIds -RootProcessId $RootProcessId)
            $nextFamilyRefresh = $now.AddMilliseconds(500)
        }
        try {
            $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
            if (
                $null -ne $focused -and
                ($familyIds -contains [int]$focused.Current.ProcessId) -and
                [string]$focused.Current.AutomationId -ceq $AutomationId
            ) {
                return $focused
            }
        } catch {
            # An unavailable/foreign provider cannot satisfy keyboard focus evidence.
        }
        Start-Sleep -Milliseconds 50
    }
    return $null
}

$report = [ordered]@{
    status = 'FAIL'
    source = 'external_windows_uia_client'
    evidence_scope = 'external System.Windows.Automation client against the fresh-extracted packaged Autosport.exe; semantic HTML collections may be legitimately empty at startup; not NVDA speech or physical-human proof'
    launch_working_directory = $null
    launcher_process_id = $null
    launcher_exit_state = 'NOT_OBSERVED'
    launcher_exit_code = $null
    initial_window_observed = $false
    native_foreground_activation_status = 'NOT_RUN'
    native_foreground_top_level_counts = $null
    native_foreground_top_level_class = 'NOT_OBSERVED'
    observed_window_class = 'NOT_OBSERVED'
    last_observed_process_family_count = 0
    native_semantic_candidate_buckets = $null
    native_renderer_accessibility_message_status = 'NOT_ATTEMPTED'
    native_host_message_status = 'NOT_ATTEMPTED'
    native_host_focus_status = 'NOT_ATTEMPTED'
    isolated_host_handle_status = 'NOT_ATTEMPTED'
    isolated_renderer_handle_status = 'NOT_ATTEMPTED'
    isolated_mta_focus_diagnostic_status = 'NOT_RUN'
    semantic_desktop_worker_timeout = $false
    semantic_desktop_worker_last_stage = 'NOT_RUN'
    semantic_desktop_worker_route_stages = $null
    process_id = $null
    process_family_ids = @()
    root_name = $null
    descendant_count = 0
    duplicate_launch_status = 'NOT_RUN'
    duplicate_start_diagnostic = 'NOT_RUN'
    duplicate_start_worker_stage = 'NOT_RUN'
    duplicate_dialog_probe_diagnostic = 'NOT_RUN'
    duplicate_launch_exit_code = $null
    duplicate_launch_dialog_title = $null
    duplicate_python_stacks = $null
    duplicate_process_state = 'NOT_OBSERVED'
    duplicate_process_exit_code = $null
    duplicate_process_family_count = 0
    duplicate_visible_window_count = 0
    automation_error_line = 0
    automation_error_category = 0
    normal_close_status = 'NOT_RUN'
    normal_close_exit_code = $null
    runtime_witness_status = 'NOT_REQUESTED'
    runtime_witness_path = $null
    runtime_browser_version = $null
    keyboard_shortcuts_status = 'NOT_RUN'
    f2_focus_automation_id = $null
    f8_focus_automation_id = $null
    emergency_stop_activation_status = 'NOT_RUN'
    emergency_stop_status_text = $null
    emergency_stop_journal_path = $null
    live_region_event_status = 'NOT_RUN'
    live_region_event_count = 0
    live_region_event_automation_id = $null
    live_region_event_text = $null
    controls = @()
    failures = @()
    real_money_execution = $false
    human_tested = $false
    nvda_verified = $false
}

$process = $null
$duplicateProcess = $null
$liveRegionProbe = $null
$lastFamilyIds = @()
$uiaRoot = $null
try {
    $exePath = (Resolve-Path -LiteralPath $Exe).Path
    $outputPath = [System.IO.Path]::GetFullPath($Output)
    $outputDirectory = Split-Path -Parent $outputPath
    if (-not [string]::IsNullOrWhiteSpace($outputDirectory)) {
        New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    }

    if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $launchWorkingDirectory = (Resolve-Path -LiteralPath $PWD.Path).Path
    } else {
        $launchWorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).Path
    }
    if (-not (Test-Path -LiteralPath $launchWorkingDirectory -PathType Container)) {
        throw "External UIA launch working directory is not an existing directory"
    }
    $report.launch_working_directory = $launchWorkingDirectory

    $process = Start-Process -FilePath $exePath -WorkingDirectory $launchWorkingDirectory -PassThru
    $report.launcher_process_id = $process.Id
    Write-ExternalUiaProgress -Stage 'WINDOW_LAUNCHED'
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
    $report.initial_window_observed = $true
    Write-ExternalUiaProgress -Stage 'NATIVE_WINDOW_OBSERVED'

    # Record the exact last bounded diagnostic before a potentially blocking
    # native UIA provider property read. Native emergency/startup dialogs must
    # fail closed here, not be misinterpreted as missing semantic WebView DOM.
    # UIA window names never enter the progress file or the error text.
    Write-ExternalUiaProgress -Stage 'NATIVE_ROOT_NAME_PROBE'
    $initialNativeName = ''
    try { $initialNativeName = [string]$uiaRoot.Current.Name } catch {}
    if ([string]::Equals($initialNativeName, 'Автоспорт — резервне аварійне керування', [System.StringComparison]::Ordinal)) {
        $report.observed_window_class = 'NATIVE_EMERGENCY_STOP'
        throw 'Packaged native emergency window is not semantic WebView2 readiness'
    }
    if ([string]::Equals($initialNativeName, 'Автоспорт — помилка запуску', [System.StringComparison]::Ordinal)) {
        $report.observed_window_class = 'NATIVE_STARTUP_ERROR'
        throw 'Packaged native startup-error window is not semantic WebView2 readiness'
    }
    if ([string]::Equals($initialNativeName, 'Автоспорт', [System.StringComparison]::Ordinal)) {
        $report.observed_window_class = 'PRODUCT_MAIN_WINDOW'
    }

    # Native host visibility is not semantic readiness. The local native HWND
    # AutomationId read is retained, but optional subtree enumeration on this
    # unscoped native window blocked fresh-extraction Windows UIA for 120 seconds
    # even after the child-BFS repair (run 38227085863). Do not perform that
    # redundant pre-family traversal. Family-scoped real UIA search below remains
    # mandatory; no runtime witness or native HWND can substitute for it.
    # The native WinForms HWND can appear before CoreWebView2 finishes attaching.
    # At that instant a synchronous deep UIA provider probe can block past the
    # startup deadline. Wait for the product's per-launch native WebView2 witness
    # first; this is readiness ordering, never a substitute for semantic UIA proof.
    if (-not [string]::IsNullOrWhiteSpace($Workspace)) {
        if (-not [System.IO.Path]::IsPathRooted($Workspace)) {
            throw "External UIA workspace witness root is not absolute"
        }
        $preSemanticWitness = Join-Path (
            [System.IO.Path]::GetFullPath($Workspace)
        ) 'webview2-runtime-witness.json'
        while (
            -not (Test-Path -LiteralPath $preSemanticWitness -PathType Leaf) -and
            [DateTime]::UtcNow -lt $deadline
        ) {
            Start-Sleep -Milliseconds 100
        }
        if (-not (Test-Path -LiteralPath $preSemanticWitness -PathType Leaf)) {
            throw "Packaged WebView2 runtime witness unavailable before semantic UIA probe"
        }
    }
    # Startup and semantic provider readiness are two independent bounded
    # phases. Windows Candidate 40032504012 published the genuine runtime
    # witness near the end of the launch budget, leaving zero time for the
    # mandatory desktop UIA probe and producing DESKTOP_BUDGET_EXHAUSTED.
    # Start a fresh finite semantic budget only after the product-owned witness
    # exists. The workflow's independent 120-second process watchdog remains
    # authoritative, and every AutomationId/keyboard/STOP/LiveRegion gate below
    # is still required; this does not turn a native HWND or witness into PASS.
    $semanticDeadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    # WinForms UIA calibration #40017780628 succeeded only after native host
    # activation. The packaged WebView2 client may otherwise be backgrounded
    # before its first accessibility request. Claim only the exact launched
    # process's visible main HWND, without querying the UIA COM provider.
    # A denied foreground claim remains diagnostic; no HWND or focus state
    # substitutes for the mandatory automation_id=330 semantic witness.
    $report.native_foreground_activation_status = 'NOT_CLAIMED'
    # A native HWND may belong to the launcher or a WebView2 child process.
    # Count only OS-enumerated top-level HWNDs, in four fixed categories.
    # This cannot establish UIA/keyboard/STOP/LiveRegion acceptance.
    try {
        $foregroundFamilyIds = @(Get-ProcessFamilyIds -RootProcessId $process.Id)
        $topLevelCounts = [AutosportNativeWindows]::TopLevelProcessOwnershipCounts(
            [int]$process.Id, [int[]]$foregroundFamilyIds
        )
        $report.native_foreground_top_level_counts = [ordered]@{
            LAUNCHER_VISIBLE = [int]$topLevelCounts[0]
            LAUNCHER_HIDDEN = [int]$topLevelCounts[1]
            FAMILY_CHILD_VISIBLE = [int]$topLevelCounts[2]
            FAMILY_CHILD_HIDDEN = [int]$topLevelCounts[3]
        }
        if ($topLevelCounts[0] -gt 0) {
            $report.native_foreground_top_level_class = 'LAUNCHER_VISIBLE'
        } elseif ($topLevelCounts[2] -gt 0) {
            $report.native_foreground_top_level_class = 'FAMILY_CHILD_VISIBLE_ONLY'
        } else {
            $report.native_foreground_top_level_class = 'NO_VISIBLE_FAMILY_TOP_LEVEL'
        }
    } catch {
        # A failed OS census is UNKNOWN, not UIA success or provider evidence.
        $report.native_foreground_top_level_class = 'UNAVAILABLE'
    }
    try {
        $process.Refresh()
        if ([AutosportNativeWindows]::TryClaimForegroundForProcess(
            $process.MainWindowHandle, [int]$process.Id
        )) {
            $report.native_foreground_activation_status = 'CLAIMED'
        }
        if ($report.native_foreground_activation_status -ne 'CLAIMED') {
            # MainWindowHandle can be zero/stale. Only actual visible TOP-LEVEL
            # HWNDs of the exact launcher may be foreground candidates; never
            # retry renderer/child HWNDs or use native focus as semantic evidence.
            $exactLauncherWindows = [AutosportNativeWindows]::VisibleTopLevelWindowsForProcess(
                [int]$process.Id
            )
            foreach ($candidateHandle in $exactLauncherWindows) {
                if ([AutosportNativeWindows]::TryClaimForegroundForProcess(
                    $candidateHandle, [int]$process.Id
                )) {
                    $report.native_foreground_activation_status = 'CLAIMED'
                    break
                }
            }
        }
        if (
            $report.native_foreground_activation_status -ne 'CLAIMED' -and
            $report.native_foreground_top_level_class -eq 'FAMILY_CHILD_VISIBLE_ONLY' -and
            [AutosportNativeWindows]::TryClaimForegroundForVisibleFamilyHost(
                [int]$process.Id, [int[]]$foregroundFamilyIds
            )
        ) {
            # Only OS foreground was claimed; AutomationId=330 and all other
            # external accessibility witnesses below remain mandatory.
            $report.native_foreground_activation_status = 'CLAIMED'
        }
    } catch {
        # Foreground restrictions are not semantic evidence; retain fail-closed UIA.
    }
    # Optional MTA FocusedElement diagnostics must not precede the mandatory
    # semantic probe: a timed-out external COM request can poison the WebView2
    # provider before the real AutomationId/keyboard/STOP/LiveRegion gate.
    # Run this differential only after semantic readiness has already FAILED.
    $semanticReady = $null
    $nextFamilyRefresh = [DateTime]::UtcNow.AddMilliseconds(1000)
    while ([DateTime]::UtcNow -lt $semanticDeadline) {
        Write-ExternalUiaProgress -Stage 'SEMANTIC_LOCAL_PROBE'
        try {
            Write-ExternalUiaProgress -Stage 'SEMANTIC_LOCAL_ROOT_ID_PROBE'
            if ([string]$uiaRoot.Current.AutomationId -eq '330') {
                $semanticReady = $uiaRoot
            }
            # The observed native root is not necessarily the actual WebView2
            # provider. The external process-family search below must produce
            # the real semantic automation_id=330 instead of traversing this
            # potentially blocking native subtree.
        } catch {
            $semanticReady = $null
        }
        if ($null -ne $semanticReady) { break }

        $now = [DateTime]::UtcNow
        if ($now -ge $nextFamilyRefresh) {
            Write-ExternalUiaProgress -Stage 'SEMANTIC_FAMILY_PROBE'
            $lastFamilyIds = @(Get-ProcessFamilyIds -RootProcessId $process.Id)
            Write-ExternalUiaProgress -Stage 'SEMANTIC_FAMILY_IDS_READY'
            $semanticSurface = Find-UiaRootWithElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '330' -RootProcessId $process.Id -Deadline $semanticDeadline
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
        # Failure-only, killable MTA differential. A returned ID is diagnostic,
        # NEVER acceptance, and cannot affect an otherwise passing UIA gate.
        # Use the latest bounded family snapshot, not a stale startup PID set.
        $report.isolated_mta_focus_diagnostic_status = Get-IsolatedMtaFocusDiagnostic -ProcessIds ([int[]]$lastFamilyIds)
        if ($script:semanticFailureClass -eq 'NOT_OBSERVED') {
            $script:semanticFailureClass = 'SEMANTIC_NOT_OBSERVED'
        }
        throw "Timed out waiting for WebView2 semantic UIA readiness (automation_id=330) across packaged process family"
    }

    Write-ExternalUiaProgress -Stage 'SEMANTIC_UIA_OBSERVED'

    # Bind the externally observed packaged UIA session to the product-owned
    # witness emitted by the actual native CoreWebView2 Environment before bridge
    # injection. A stale witness cannot survive canonical startup because the shell
    # invalidates it before creating the window and fails closed if republishing fails.
    if (-not [string]::IsNullOrWhiteSpace($Workspace)) {
        $workspacePath = [System.IO.Path]::GetFullPath($Workspace)
        if (-not [System.IO.Path]::IsPathRooted($workspacePath)) {
            throw "External UIA workspace witness root is not absolute"
        }
        $runtimeWitnessPath = Join-Path $workspacePath 'webview2-runtime-witness.json'
        $report.runtime_witness_path = $runtimeWitnessPath
        # WebView2 can expose the loaded DOM to UI Automation shortly before
        # pywebview's blocking before_load callback publishes the native runtime
        # witness. Use only the existing launch deadline: never fabricate a file,
        # accept a missing witness, or bypass its strict content validation.
        while (
            -not (Test-Path -LiteralPath $runtimeWitnessPath -PathType Leaf) -and
            [DateTime]::UtcNow -lt $deadline
        ) {
            Start-Sleep -Milliseconds 100
        }
        if (-not (Test-Path -LiteralPath $runtimeWitnessPath -PathType Leaf)) {
            throw "Packaged WebView2 session did not publish the actual runtime witness"
        }
        try {
            $runtimeWitness = Get-Content -LiteralPath $runtimeWitnessPath -Raw | ConvertFrom-Json
        } catch {
            throw "Packaged WebView2 runtime witness is not valid JSON"
        }
        $browserVersion = [string]$runtimeWitness.browser_version_string
        if (
            [int]$runtimeWitness.schema_version -ne 1 -or
            [string]$runtimeWitness.renderer -ne 'edgechromium' -or
            [string]$runtimeWitness.observation_source -ne 'native_core_webview2_environment' -or
            [string]::IsNullOrWhiteSpace($browserVersion) -or
            $browserVersion -ne $browserVersion.Trim() -or
            $browserVersion.Length -gt 256 -or
            $browserVersion -match '[\x00-\x1F\x7F]' -or
            $runtimeWitness.real_money_execution -ne $false -or
            $runtimeWitness.human_tested -ne $false -or
            $runtimeWitness.nvda_verified -ne $false -or
            $runtimeWitness.whole_product_complete -ne $false
        ) {
            throw "Packaged WebView2 runtime witness violated the bounded runtime/truth contract"
        }
        $report.runtime_browser_version = $browserVersion
        $report.runtime_witness_status = 'PASS'
        Write-ExternalUiaProgress -Stage 'RUNTIME_WITNESS_OBSERVED'
    }

    # While the real packaged semantic shell is alive, a second normal launch must
    # fail at the product-owned interactive lock boundary rather than opening a
    # second WebView2/operator instance against either shared mutable root.
    Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_PROBE'
    # The primary UIA provider has already been queried. A synchronous second
    # Start-Process on this audit runspace stalled for the full 120s watchdog.
    # Launch from an independent runspace and wait only for process creation,
    # not for the duplicate app to exit or for its UIA dialog to appear.
    $duplicateStartResults = [System.Collections.Concurrent.ConcurrentQueue[object]]::new()
    $duplicateStartOutcomes = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
    $duplicateStartStages = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
    $duplicateStartCompleted = [System.Threading.ManualResetEventSlim]::new($false)
    # PowerShell enumerates empty queues in @(); preserve six exact object slots.
    $duplicateStartArguments = [object[]]::new(6)
    $duplicateStartArguments[0] = $exePath
    $duplicateStartArguments[1] = $launchWorkingDirectory
    $duplicateStartArguments[2] = $duplicateStartResults
    $duplicateStartArguments[3] = $duplicateStartOutcomes
    $duplicateStartArguments[4] = $duplicateStartCompleted
    $duplicateStartArguments[5] = $duplicateStartStages
    try {
        $duplicateStartWorker = Start-ThreadJob -ScriptBlock {
            param(
                [string]$exePath, [string]$launchWorkingDirectory,
                [object]$Results, [object]$Outcomes, [object]$Completion,
                [object]$Stages
            )
            try {
                $Stages.Enqueue('WORKER_ENTER')
                # Bypass Start-Process/ShellExecute's synchronous Windows
                # invocation path after a UIA provider has been queried.
                # The worker is bounded by the independent completion event.
                $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
                $startInfo.FileName = $exePath
                $startInfo.WorkingDirectory = $launchWorkingDirectory
                $startInfo.UseShellExecute = $false
                $startInfo.CreateNoWindow = $false
                $duplicateProcess = [System.Diagnostics.Process]::new()
                $duplicateProcess.StartInfo = $startInfo
                $Stages.Enqueue('PROCESS_START_PROBE')
                if (-not $duplicateProcess.Start()) {
                    $Outcomes.Enqueue('INVALID_PROCESS')
                    $Stages.Enqueue('INVALID_PROCESS')
                    $duplicateProcess.Dispose()
                } elseif ([int]$duplicateProcess.Id -gt 0) {
                    $Results.Enqueue($duplicateProcess)
                    $Outcomes.Enqueue('RETURNED')
                    $Stages.Enqueue('PROCESS_RETURNED')
                } else {
                    $Outcomes.Enqueue('INVALID_PROCESS')
                    $Stages.Enqueue('INVALID_PROCESS')
                    $duplicateProcess.Dispose()
                }
            } catch {
                # Never publish exception text, executable path, PID or command line.
                $Outcomes.Enqueue('LAUNCH_FAILED')
                $Stages.Enqueue('PROCESS_EXCEPTION')
            } finally {
                $Completion.Set()
            }
        } -ArgumentList $duplicateStartArguments -ThrottleLimit 16
    } catch {
        $report.duplicate_start_diagnostic = 'LAUNCH_FAILED'
        $report.duplicate_start_worker_stage = 'WORKER_LAUNCH_FAILED'
        throw 'Second packaged launch worker could not start'
    }
    # Read only fixed, non-sensitive worker stages; no ThreadJob State/Receive-Job
    # or Stop-Job call may reenter a blocked UIA/PowerShell provider.
    $duplicateStartTerminal = $duplicateStartCompleted.Wait(6000)
    $report.duplicate_start_worker_stage = 'NOT_ENTERED'
    [string]$duplicateWorkerStage = $null
    while ($duplicateStartStages.TryDequeue([ref]$duplicateWorkerStage)) {
        $report.duplicate_start_worker_stage = [string]$duplicateWorkerStage
    }
    if (-not $duplicateStartTerminal) {
        $report.duplicate_start_diagnostic = 'TIMED_OUT'
        throw 'Second packaged launch bounded process creation timed out'
    }
    [string]$duplicateStartOutcome = $null
    if (-not $duplicateStartOutcomes.TryDequeue([ref]$duplicateStartOutcome)) {
        $report.duplicate_start_diagnostic = 'EMPTY_RESULT'
        throw 'Second packaged launch worker returned no outcome'
    }
    if ($duplicateStartOutcome -ne 'RETURNED') {
        if ($duplicateStartOutcome -eq 'INVALID_PROCESS') {
            $report.duplicate_start_diagnostic = 'INVALID_PROCESS'
        } else {
            $report.duplicate_start_diagnostic = 'LAUNCH_FAILED'
        }
        throw 'Second packaged launch did not return a valid process'
    }
    [object]$duplicateStarted = $null
    if (-not $duplicateStartResults.TryDequeue([ref]$duplicateStarted) -or
        $null -eq $duplicateStarted) {
        $report.duplicate_start_diagnostic = 'EMPTY_RESULT'
        throw 'Second packaged launch did not return a valid process'
    }
    try {
        $duplicateProcess = [System.Diagnostics.Process]$duplicateStarted
        if ($null -eq $duplicateProcess -or [int]$duplicateProcess.Id -le 0) {
            throw 'Invalid process receipt'
        }
    } catch {
        $report.duplicate_start_diagnostic = 'INVALID_PROCESS'
        throw 'Second packaged launch returned invalid process receipt'
    }
    $report.duplicate_start_diagnostic = 'RETURNED'
    $duplicateStartCompleted.Dispose()
    Write-ExternalUiaProgress -Stage 'DUPLICATE_START_PROCESS_READY'
    # The packaged second process must be given the caller's actual bounded
    # startup allowance. Ten seconds proved insufficient on the Windows candidate
    # while the launcher was still running; the UIA gate remains fail-closed.
    # A PyInstaller one-file duplicate must unpack before Python can acquire the
    # existing workspace lock and display the native refusal dialog. Hosted
    # Windows under load exceeded the ordinary 20-second UIA phase budget.
    # Keep this phase bounded inside the independent 180-second workflow
    # watchdog without weakening any semantic dialog/exit requirement.
    $duplicateDeadline = [DateTime]::UtcNow.AddSeconds(
        [Math]::Max(90, $TimeoutSeconds)
    )
    $report.duplicate_dialog_probe_diagnostic = 'NOT_RUN'
    $duplicateDialogObserved = $false
    $duplicateFamilyIds = @([int]$duplicateProcess.Id)
    $nextDuplicateFamilyProbe = [DateTime]::MinValue
    while ([DateTime]::UtcNow -lt $duplicateDeadline) {
        if ([DateTime]::UtcNow -ge $nextDuplicateFamilyProbe) {
            Write-ExternalUiaProgress -Stage 'DUPLICATE_PROCESS_FAMILY_PROBE'
            $duplicateFamilyIds = @(
                Get-BoundedDuplicateProcessFamilyIds -RootProcessId $duplicateProcess.Id
            )
            $report.duplicate_process_family_count = [int]$duplicateFamilyIds.Count
            Write-ExternalUiaProgress -Stage 'DUPLICATE_PROCESS_FAMILY_READY'
            Write-ExternalUiaProgress -Stage 'DUPLICATE_UIA_ROOT_PROBE'
            # Process.MainWindowHandle can select a hidden bootloader owner
            # instead of the ownerless MessageBox. Reuse the bounded Win32
            # visible top-level census; never query desktop-wide UIA or child
            # HWNDs whose provider can alias the same dialog multiple times.
            $duplicateNativeHandles = @(
                foreach ($duplicateFamilyId in $duplicateFamilyIds) {
                    [AutosportNativeWindows]::VisibleTopLevelWindowsForProcess(
                        [int]$duplicateFamilyId
                    )
                }
            )
            if ($duplicateNativeHandles.Count -gt 4096) {
                throw 'Second packaged launch native dialog census exceeded limit'
            }
            $report.duplicate_visible_window_count = [int]$duplicateNativeHandles.Count
            $duplicateProbe = Invoke-IsolatedDuplicateDialogProbe `
                -ProcessIds $duplicateFamilyIds `
                -WindowHandles ([IntPtr[]]$duplicateNativeHandles)
            $report.duplicate_dialog_probe_diagnostic = [string]$duplicateProbe
            if ($duplicateProbe -ceq 'DIALOG_CLOSED') {
                $duplicateDialogObserved = $true
                $report.duplicate_launch_dialog_title = 'Автоспорт — помилка запуску'
                break
            }
            if ($duplicateProbe -cin @(
                'SECOND_SEMANTIC_SURFACE', 'AMBIGUOUS_DIALOG', 'COPY_MISMATCH',
                'APARTMENT_ERROR', 'NO_PROCESS_FAMILY', 'PROVIDER_ERROR',
                'INVALID_RESULT', 'LAUNCH_ERROR', 'UNAVAILABLE'
            )) {
                throw "Second packaged launch duplicate-dialog probe failed closed; fixed_code=$duplicateProbe"
            }
            $nextDuplicateFamilyProbe = [DateTime]::UtcNow.AddMilliseconds(1000)
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $duplicateDialogObserved) {
        $duplicateExitState = 'unavailable'
        try {
            $duplicateProcess.Refresh()
            if ($duplicateProcess.HasExited) {
                $duplicateExitState = "exited:$([int]$duplicateProcess.ExitCode)"
            } else {
                $duplicateExitState = 'running'
            }
        } catch {
            $duplicateExitState = 'unavailable'
        }
        $duplicateObservedFamilyCount = @($duplicateFamilyIds).Count
        throw "Second packaged launch did not expose the bounded duplicate-instance dialog (process=$duplicateExitState; family_count=$duplicateObservedFamilyCount; fixed_code=$($report.duplicate_dialog_probe_diagnostic))"
    }

    if (-not $duplicateProcess.WaitForExit(5000)) {
        throw "Second packaged launch did not terminate after duplicate-instance dialog dismissal"
    }
    if ($duplicateProcess.ExitCode -ne 2) {
        throw "Second packaged launch returned unexpected exit code $($duplicateProcess.ExitCode)"
    }
    $report.duplicate_launch_exit_code = [int]$duplicateProcess.ExitCode
    $report.duplicate_launch_status = 'PASS'

    $report.process_family_ids = @($lastFamilyIds | Sort-Object -Unique)
    $report.process_id = [int]$uiaRoot.Current.ProcessId
    $report.root_name = [string]$uiaRoot.Current.Name
    if ([string]::IsNullOrWhiteSpace($report.root_name)) {
        $report.failures += 'main window has no external UIA Name'
    }

    # Exercise the shipped keyboard accelerators through the real external
    # Windows input/UIA boundary. Static handler inspection alone cannot prove that
    # the packaged WebView receives the key or moves focus to the semantic target.
    try {
        $uiaRoot.SetFocus()
        [System.Windows.Forms.SendKeys]::SendWait('{F2}')
        $shortcutDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(2, $TimeoutSeconds))
        $focusedF2 = Wait-ForFocusedAutomationId -RootProcessId $process.Id -AutomationId '301' -Deadline $shortcutDeadline
        if ($null -eq $focusedF2) {
            throw "F2 did not move packaged keyboard focus to semantic screen navigation"
        }
        $report.f2_focus_automation_id = [string]$focusedF2.Current.AutomationId

        $uiaRoot.SetFocus()
        [System.Windows.Forms.SendKeys]::SendWait('{F8}')
        $shortcutDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(2, $TimeoutSeconds))
        $focusedF8 = Wait-ForFocusedAutomationId -RootProcessId $process.Id -AutomationId '204' -Deadline $shortcutDeadline
        if ($null -eq $focusedF8) {
            throw "F8 did not move packaged keyboard focus to evaluation evidence"
        }
        $report.f8_focus_automation_id = [string]$focusedF8.Current.AutomationId
        $report.keyboard_shortcuts_status = 'PASS'
    } catch {
        $report.failures += 'packaged keyboard shortcut audit failed: focus evidence unavailable'
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
            $report.failures += 'owner economic panel open failed: activation evidence unavailable'
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
        $report.failures += 'manual calculation panel open failed: activation evidence unavailable'
    }

    $lastFamilyIds = @(Get-ProcessFamilyIds -RootProcessId $process.Id)
    $report.process_family_ids = @($lastFamilyIds | Sort-Object -Unique)
    $descendants = $uiaRoot.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $report.descendant_count = $descendants.Count

    # Reuse the *already acquired* semantic tree. Do not initiate an
    # additional unbounded desktop-wide UIA FindAll: known WebView2 providers
    # can block the external watchdog. Count critical AutomationIds in the
    # real observed process-family subtree and deny duplicates/partial reads.
    $criticalIds = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($spec in $expected) {
        $null = $criticalIds.Add([string]$spec.automation_id)
    }
    $criticalCounts = [System.Collections.Generic.Dictionary[string,int]]::new(
        [System.StringComparer]::Ordinal
    )
    if ($descendants.Count -gt 4096) {
        $report.failures += 'external UIA critical identity scan exceeded bounded collection limit'
    } else {
        try {
            foreach ($candidate in $descendants) {
                if (-not ($lastFamilyIds -contains [int]$candidate.Current.ProcessId)) { continue }
                $candidateId = [string]$candidate.Current.AutomationId
                if (-not $criticalIds.Contains($candidateId)) { continue }
                if ($criticalCounts.ContainsKey($candidateId)) {
                    $criticalCounts[$candidateId] = $criticalCounts[$candidateId] + 1
                } else {
                    $criticalCounts.Add($candidateId, 1)
                }
            }
            foreach ($spec in $expected) {
                $expectedId = [string]$spec.automation_id
                if ($criticalCounts.ContainsKey($expectedId) -and
                    $criticalCounts[$expectedId] -gt 1) {
                    $report.failures += "automation_id=$($expectedId): duplicate external UIA control identity"
                }
            }
        } catch {
            $report.failures += 'external UIA critical identity scan incomplete'
        }
    }

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
            automation_id = [string]$spec.automation_id
            expected_name = $spec.name
            name = $(if ($currentName -ceq [string]$spec.name) { [string]$spec.name } else { '[redacted mismatch]' })
            control_type = $(if ($controlType -ceq [string]$spec.expected_control_type) { [string]$spec.expected_control_type } else { '[redacted mismatch]' })
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

        if ($currentName -cne [string]$spec.name) {
            $report.failures += "automation_id=$($spec.automation_id): external UIA Name mismatch"
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$spec.expected_control_type) -and $controlType -cne [string]$spec.expected_control_type) {
            $report.failures += "automation_id=$($spec.automation_id): external UIA ControlType mismatch"
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

    # Exercise the real packaged emergency STOP on this clean isolated
    # workspace. This proves the keyboard/UIA action reaches canonical durable STOP
    # authority and that the operator receives the confirmed semantic readback.
    if ($report.failures.Count -eq 0 -and -not [string]::IsNullOrWhiteSpace($Workspace)) {
        try {
            $stopJournal = Join-Path ([System.IO.Path]::GetFullPath($Workspace)) 'execution-stop.jsonl'
            $report.emergency_stop_journal_path = $stopJournal
            if (Test-Path -LiteralPath $stopJournal) {
                throw "clean packaged emergency STOP audit found a pre-existing STOP journal"
            }

            $stopButton = Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId 'emergency-stop-action'
            if ($null -eq $stopButton) {
                throw "packaged emergency STOP action disappeared before activation"
            }
            $stopStatusBefore = Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId 'emergency-stop-status'
            if ($null -eq $stopStatusBefore) {
                throw "packaged emergency STOP status disappeared before live-region subscription"
            }
            $liveRegionProbe = [AutosportExternalLiveRegionProbe]::new($stopStatusBefore)
            Invoke-ExternalAction -Element $stopButton

            $stopDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(5, $TimeoutSeconds))
            $confirmedStopText = $null
            while ([DateTime]::UtcNow -lt $stopDeadline) {
                $stopStatus = Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId 'emergency-stop-status'
                if ($null -ne $stopStatus) {
                    try {
                        $candidateText = [string]$stopStatus.Current.Name
                        if (
                            $candidateText -match '^Аварійний STOP (активовано|активний)' -and
                            $candidateText -match 'підтверджено стійкий запис ревізії'
                        ) {
                            $confirmedStopText = $candidateText
                            break
                        }
                    } catch {}
                }
                Start-Sleep -Milliseconds 50
            }
            if ([string]::IsNullOrWhiteSpace($confirmedStopText)) {
                throw "packaged emergency STOP did not expose confirmed durable status text"
            }

            $eventDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(2, $TimeoutSeconds))
            while (
                [DateTime]::UtcNow -lt $eventDeadline -and
                (
                    $liveRegionProbe.Count -lt 1 -or
                    [string]$liveRegionProbe.LastAutomationId -ne 'emergency-stop-status' -or
                    [string]$liveRegionProbe.LastName -ne $confirmedStopText
                )
            ) {
                Start-Sleep -Milliseconds 50
            }
            $report.live_region_event_count = [int]$liveRegionProbe.Count
            $report.live_region_event_automation_id = [string]$liveRegionProbe.LastAutomationId
            $report.live_region_event_text = [string]$liveRegionProbe.LastName
            if (
                $report.live_region_event_count -lt 1 -or
                $report.live_region_event_automation_id -ne 'emergency-stop-status' -or
                $report.live_region_event_text -ne $confirmedStopText
            ) {
                throw "packaged emergency STOP did not emit an externally observed LiveRegionChanged event"
            }
            $report.live_region_event_status = 'PASS'

            $stopFocusDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(2, $TimeoutSeconds))
            if ($null -eq (Wait-ForFocusedAutomationId -RootProcessId $process.Id -AutomationId 'emergency-stop-status' -Deadline $stopFocusDeadline)) {
                throw "packaged emergency STOP confirmation did not retain focus on the dedicated status"
            }
            if (-not (Test-Path -LiteralPath $stopJournal -PathType Leaf)) {
                throw "packaged emergency STOP did not create the durable STOP journal"
            }
            $stopJournalItem = Get-Item -LiteralPath $stopJournal
            if ($stopJournalItem.Length -le 0) {
                throw "packaged emergency STOP durable journal is empty"
            }

            $report.emergency_stop_status_text = $confirmedStopText
            $report.emergency_stop_activation_status = 'PASS'
        } catch {
            $report.failures += 'packaged emergency STOP activation audit failed: durable proof unavailable'
        }
    }

    # Close both expanded disclosures through external UIA and prove focus
    # returns to the corresponding trigger before the subtree disappears.
    if ($report.failures.Count -eq 0) {
        try {
            $ownerClose = Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '329'
            if ($null -eq $ownerClose) {
                throw "owner close control disappeared before focus-handoff audit"
            }
            Invoke-ExternalAction -Element $ownerClose
            $focusDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(2, $TimeoutSeconds))
            if ($null -eq (Wait-ForFocusedAutomationId -RootProcessId $process.Id -AutomationId '305' -Deadline $focusDeadline)) {
                throw "owner disclosure close did not restore focus to automation_id=305"
            }

            $manualClose = Find-UiaElementForProcessFamily -ProcessIds $lastFamilyIds -AutomationId '336'
            if ($null -eq $manualClose) {
                throw "manual close control disappeared before focus-handoff audit"
            }
            Invoke-ExternalAction -Element $manualClose
            $focusDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(2, $TimeoutSeconds))
            if ($null -eq (Wait-ForFocusedAutomationId -RootProcessId $process.Id -AutomationId '330' -Deadline $focusDeadline)) {
                throw "manual disclosure close did not restore focus to automation_id=330"
            }
        } catch {
            $report.failures += 'packaged disclosure focus-handoff audit failed: focus evidence unavailable'
        }
    }

    # A semantic/accessibility PASS must also prove that the exact packaged main
    # window can complete its ordinary product-owned close lifecycle. The cleanup
    # block below is recovery only; a later force-kill must never convert a hung
    # STOP/worker teardown into a release qualification PASS.
    if ($report.failures.Count -eq 0) {
        $closePatternObject = $null
        if ($uiaRoot.TryGetCurrentPattern(
            [System.Windows.Automation.WindowPattern]::Pattern,
            [ref]$closePatternObject
        ) -and $null -ne $closePatternObject) {
            ([System.Windows.Automation.WindowPattern]$closePatternObject).Close()
        } else {
            $uiaRoot.SetFocus()
            [System.Windows.Forms.SendKeys]::SendWait('%{F4}')
        }

        $closeDeadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(10, $TimeoutSeconds))
        $aliveAfterClose = @()
        while ([DateTime]::UtcNow -lt $closeDeadline) {
            $aliveAfterClose = @()
            foreach ($candidateId in @(Get-ProcessFamilyIds -RootProcessId $process.Id)) {
                try {
                    $candidate = Get-Process -Id $candidateId -ErrorAction Stop
                    if (-not $candidate.HasExited) { $aliveAfterClose += [int]$candidateId }
                } catch {}
            }
            if ($aliveAfterClose.Count -eq 0) { break }
            Start-Sleep -Milliseconds 100
        }
        if ($aliveAfterClose.Count -ne 0) {
            $report.failures += (
                "main packaged launch did not terminate through ordinary close lifecycle; " +
                "alive_process_ids=" + (($aliveAfterClose | Sort-Object -Unique) -join ',')
            )
        } else {
            try {
                $process.WaitForExit()
                $report.normal_close_exit_code = [int]$process.ExitCode
            } catch {
                $report.failures += "main packaged launch exit code could not be observed after ordinary close"
            }
            if ($report.normal_close_exit_code -ne 0) {
                $report.failures += "main packaged launch returned unexpected normal-close exit code $($report.normal_close_exit_code)"
            } else {
                $report.normal_close_status = 'PASS'
            }
        }
    }

    if ($report.failures.Count -eq 0) {
        $report.status = 'PASS'
        Write-ExternalUiaProgress -Stage 'AUDIT_COMPLETE'
    }
} catch {
    if ($script:lastDesktopWorkerStage -ne 'NOT_RUN') {
        $report.failures += 'external UIA audit failed: desktop semantic worker timeout'
    } elseif ($script:semanticFailureClass -eq 'NATIVE_WORKER_TIMEOUT') {
        $report.failures += 'external UIA audit failed: native semantic worker timeout'
    } elseif ($script:semanticFailureClass -eq 'DESKTOP_BUDGET_EXHAUSTED') {
        $report.failures += 'external UIA audit failed: desktop semantic budget exhausted'
    } elseif ($script:semanticFailureClass -eq 'SEMANTIC_NOT_OBSERVED') {
        $report.failures += 'external UIA audit failed: semantic readiness not observed'
    } else {
        $report.failures += 'external UIA audit failed: unexpected automation error'
    }
    # ErrorRecord-owned numeric location/category only. Never persist exception
    # text, provider properties, source excerpts, paths or arbitrary type names.
    $report.automation_error_line = [int]$_.InvocationInfo.ScriptLineNumber
    $report.automation_error_category = [int]$_.CategoryInfo.Category
    if ($null -ne $duplicateProcess) {
        $report.duplicate_python_stacks = Get-DuplicatePythonStackDiagnostics `
            -ProcessIds ([int[]]$duplicateFamilyIds) -RootProcessId ([int]$duplicateProcess.Id)
    }
    # Optional independent diagnostics may inspect cached primitive HWNDs only
    # after acceptance has already failed. They cannot set status or bypass any
    # gate, and must not reenter even a separate provider after a native worker
    # timeout has declared that provider poisoned.
    if ($report.status -ne 'PASS' -and -not $script:semanticWorkerTimedOut) {
        $script:isolatedHostHandleStatus = Get-IsolatedMtaHandleDiagnostic `
            -Window $script:failureDiagnosticHostHandle -ProcessIds ([int[]]$lastFamilyIds)
        $script:isolatedRendererHandleStatus = Get-IsolatedMtaHandleDiagnostic `
            -Window $script:failureDiagnosticRendererHandle -ProcessIds ([int[]]$lastFamilyIds)
    }
} finally {
    # Preserve a bounded count from the actual pre-cleanup UIA search. A vanished
    # launcher with child processes differs from a completely empty process family.
    # Export neither descendant PIDs nor native window names in this diagnostic.
    $report.last_observed_process_family_count = [int]@($lastFamilyIds).Count
    $report.native_semantic_candidate_buckets = $script:nativeSemanticCandidateBuckets
    $report.native_renderer_accessibility_message_status = $script:nativeRendererAccessibilityMessageStatus
    $report.native_host_message_status = $script:nativeHostMessageStatus
    $report.native_host_focus_status = $script:nativeHostFocusStatus
    $report.isolated_host_handle_status = $script:isolatedHostHandleStatus
    $report.isolated_renderer_handle_status = $script:isolatedRendererHandleStatus
    $report.semantic_desktop_worker_timeout = ($script:lastDesktopWorkerStage -ne 'NOT_RUN')
    $report.semantic_desktop_worker_last_stage = $script:lastDesktopWorkerStage
    $report.semantic_desktop_worker_route_stages = $script:lastDesktopWorkerRouteStages
    # Record bounded process-lifetime evidence BEFORE cleanup can kill a live
    # window. Do not disclose command lines, workspace paths, or environment.
    # A missing semantic automation id must remain a hard FAIL regardless.
    if ($null -ne $process) {
        try {
            $process.Refresh()
            if ($process.HasExited) {
                $report.launcher_exit_state = 'EXITED'
                $report.launcher_exit_code = [int]$process.ExitCode
            } else {
                $report.launcher_exit_state = 'RUNNING'
            }
        } catch {
            $report.launcher_exit_state = 'UNAVAILABLE'
        }
    }
    # Never reenter the UIA COM provider during failure cleanup. A Windows
    # candidate watchdog has observed a provider stall with no serialized audit
    # JSON (run 39951554204, stage SEMANTIC_NATIVE_CANDIDATES_EXHAUSTED).
    # Even if the bounded desktop worker has not marked a timeout, a separate
    # native/desktop provider read can still be poisoned. Re-reading
    # $uiaRoot.Current.Name here could hang beyond the independent watchdog.
    # Preserve only the fixed class recorded during the initial native-root
    # probe above. Unknown stays NOT_OBSERVED/OTHER_OR_UNAVAILABLE, never PASS.
    # No COM/UIA access is permitted in this cleanup path.
    if ($null -ne $liveRegionProbe) {
        try {
            $liveRegionProbe.Dispose()
        } catch {}
        $liveRegionProbe = $null
    }
    if ($null -ne $duplicateProcess) {
        try {
            $duplicateProcess.Refresh()
            if ($duplicateProcess.HasExited) {
                $report.duplicate_process_state = 'EXITED'
                $report.duplicate_process_exit_code = [int]$duplicateProcess.ExitCode
            } else {
                $report.duplicate_process_state = 'RUNNING'
            }
        } catch {
            $report.duplicate_process_state = 'UNAVAILABLE'
        }
        try {
            $duplicateCleanupIds = @(Get-ProcessFamilyIds -RootProcessId $duplicateProcess.Id | Sort-Object -Unique -Descending)
        } catch {
            $duplicateCleanupIds = @($duplicateProcess.Id)
        }
        foreach ($cleanupId in $duplicateCleanupIds) {
            try {
                $candidate = Get-Process -Id $cleanupId -ErrorAction Stop
                if (-not $candidate.HasExited) {
                    Stop-Process -Id $cleanupId -Force -ErrorAction SilentlyContinue
                }
            } catch {}
        }
    }
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
    # Project equality/presence from original in-memory observations BEFORE
    # privacy scrubbing. A nonempty '[redacted]' sentinel is never proof that
    # a path, title, or status matched the real request/product observation.
    $report.launch_working_directory_verified = (
        -not [string]::IsNullOrWhiteSpace($launchWorkingDirectory) -and
        [string]::Equals([string]$report.launch_working_directory,
            $launchWorkingDirectory, [System.StringComparison]::OrdinalIgnoreCase)
    )
    $report.duplicate_dialog_title_verified = (
        [string]$report.duplicate_launch_dialog_title -ceq 'Автоспорт — помилка запуску'
    )
    $report.emergency_stop_status_text_present = (
        -not [string]::IsNullOrWhiteSpace([string]$report.emergency_stop_status_text)
    )
    $report.live_region_event_text_present = (
        -not [string]::IsNullOrWhiteSpace([string]$report.live_region_event_text)
    )
    $report.runtime_browser_version_present = (
        -not [string]::IsNullOrWhiteSpace([string]$report.runtime_browser_version)
    )
    $report.runtime_witness_matches_workspace = $false
    $report.emergency_stop_journal_matches_workspace = $false
    if (-not [string]::IsNullOrWhiteSpace($Workspace)) {
        $bindingWorkspace = [System.IO.Path]::GetFullPath($Workspace)
        $report.runtime_witness_matches_workspace = [string]::Equals(
            [string]$report.runtime_witness_path,
            (Join-Path $bindingWorkspace 'webview2-runtime-witness.json'),
            [System.StringComparison]::OrdinalIgnoreCase
        )
        $report.emergency_stop_journal_matches_workspace = [string]::Equals(
            [string]$report.emergency_stop_journal_path,
            (Join-Path $bindingWorkspace 'execution-stop.jsonl'),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    # Keep UIA validation in memory; scrub provider-controlled names and
    # workspace paths before either PASS or FAIL is serialized to JSON/stdout.
    foreach ($field in @(
        'launch_working_directory',
        'runtime_witness_path',
        'emergency_stop_journal_path',
        'root_name',
        'duplicate_launch_dialog_title',
        'emergency_stop_status_text',
        'live_region_event_text',
        'runtime_browser_version'
    )) {
        if ($null -ne $report[$field]) {
            $report[$field] = '[redacted]'
        }
    }
    if ($null -ne $report.live_region_event_automation_id) {
        $report.live_region_event_automation_id = if (
            $report.live_region_event_automation_id -ceq 'emergency-stop-status'
        ) { 'emergency-stop-status' } else { '[redacted mismatch]' }
    }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Output -Encoding utf8
}

if ($report.status -ne 'PASS') {
    Write-Host ($report | ConvertTo-Json -Depth 8)
    exit 1
}
Write-Host "external_uia_audit=PASS controls=$($report.controls.Count) descendants=$($report.descendant_count) process=$($report.process_id)"
exit 0
