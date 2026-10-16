# AS-08 Plan 6 §7Б: independently qualify actual packaged native keyboard STOP.
# This is a Windows-only acceptance gate, not a WebView2 UIA / NVDA / financial claim.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Exe,
    [Parameter(Mandatory)][string]$Output,
    [int]$TimeoutSeconds = 40
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not [IO.Path]::IsPathFullyQualified($Exe) -or -not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
    throw 'Native keyboard STOP qualification requires an existing absolute packaged executable'
}
if (-not [IO.Path]::IsPathFullyQualified($Output)) {
    throw 'Native keyboard STOP evidence path must be absolute'
}
if ($TimeoutSeconds -lt 15 -or $TimeoutSeconds -gt 120) { throw 'Invalid bounded test timeout' }
$outputDir = [IO.Path]::GetDirectoryName($Output)
if (-not (Test-Path -LiteralPath $outputDir -PathType Container)) { throw 'Output directory must already exist' }
$Exe = [IO.Path]::GetFullPath($Exe)
$executableSha256 = (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash.ToLowerInvariant()
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class AutosportStopKeyboardWindow {
  delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr extra);
  [DllImport("user32.dll", CharSet=CharSet.Unicode, ExactSpelling=true)] static extern int GetWindowTextW(IntPtr hWnd, StringBuilder buffer, int length);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true, ExactSpelling=true)]
  static extern IntPtr SendMessageTimeoutW(
    IntPtr hWnd, uint message, IntPtr wParam, IntPtr lParam,
    uint flags, uint timeoutMs, out IntPtr result);
  [DllImport("user32.dll", CharSet=CharSet.Unicode, ExactSpelling=true)]
  static extern int GetClassNameW(IntPtr hWnd, StringBuilder name, int maxCount);
  public static IntPtr Find(uint[] trustedIds, string title) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((window, unused) => {
      uint candidate;
      GetWindowThreadProcessId(window, out candidate);
      if (Array.IndexOf(trustedIds, candidate) < 0 || !IsWindowVisible(window)) return true;
      StringBuilder nativeClass = new StringBuilder(64);
      GetClassNameW(window, nativeClass, nativeClass.Capacity);
      if (!String.Equals(nativeClass.ToString(), "#32770", StringComparison.Ordinal)) return true;
      StringBuilder text = new StringBuilder(200);
      GetWindowTextW(window, text, text.Capacity);
      if (String.Equals(text.ToString(), title, StringComparison.Ordinal)) { found=window; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
  public static bool Focus(IntPtr hwnd) {
    if (hwnd==IntPtr.Zero) return false;
    SetForegroundWindow(hwnd);
    return GetForegroundWindow()==hwnd;
  }
  public static int DefaultButtonId(IntPtr hwnd) {
    const int DM_GETDEFID = 0x0400;
    IntPtr result;
    IntPtr sent=SendMessageTimeoutW(hwnd, DM_GETDEFID, IntPtr.Zero, IntPtr.Zero, 0x0002, 1000, out result);
    if (sent==IntPtr.Zero) return -1;
    long raw=result.ToInt64();
    if (((raw >> 16) & 0xFFFF) != 0x534B) return -1;
    return (int)(raw & 0xFFFF);
  }
}
'@

$root = Join-Path ([IO.Path]::GetDirectoryName($Output)) 'AS08-native-stop-test-state'
if (Test-Path -LiteralPath $root) { throw 'Refusing to overwrite a pre-existing native STOP test root' }
New-Item -ItemType Directory -Path $root | Out-Null
$originalWorkspace = $env:AUTOSPORT_WORKSPACE
$hadWorkspace = Test-Path Env:AUTOSPORT_WORKSPACE
$originalLocal = $env:LOCALAPPDATA
$hadLocal = Test-Path Env:LOCALAPPDATA
$originalWebView = $env:WEBVIEW2_BROWSER_EXECUTABLE_FOLDER
$hadWebView = Test-Path Env:WEBVIEW2_BROWSER_EXECUTABLE_FOLDER
$promptTitle = 'Автоспорт — резервне аварійне керування'
$resultTitle = 'Автоспорт — результат аварійного STOP'
$rows = @()

# One-file PyInstaller may use a same-image child to host the MessageBoxW.
# Only launch-descendant PIDs with the identical executable path are trusted.
function Get-TrustedPackagedProcessFamily {
  param([int]$RootPid, [string]$PackagedExe)
  $ids = [Collections.Generic.List[uint32]]::new()
  $queued = [Collections.Generic.Queue[int]]::new()
  $seen = [Collections.Generic.HashSet[int]]::new()
  $queued.Enqueue($RootPid)
  while ($queued.Count -gt 0) {
    if ($seen.Count -ge 16) { throw 'Packaged process ancestry exceeded bounded limit' }
    $pidToCheck = $queued.Dequeue()
    if (-not $seen.Add($pidToCheck)) { continue }
    $record = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $pidToCheck" -ErrorAction Stop
    if ($null -eq $record -or [string]::IsNullOrWhiteSpace($record.ExecutablePath)) { continue }
    $observedExe = [IO.Path]::GetFullPath([string]$record.ExecutablePath)
    if (-not [string]::Equals($observedExe, $PackagedExe, [StringComparison]::OrdinalIgnoreCase)) {
      if ($pidToCheck -eq $RootPid) { throw 'Packaged native STOP root executable path changed' }
      continue
    }
    $ids.Add([uint32]$pidToCheck)
    $children = @(Get-CimInstance -ClassName Win32_Process -Filter "ParentProcessId = $pidToCheck" -ErrorAction Stop)
    foreach ($subprocess in $children) {
      if ($subprocess.ProcessId -gt 0 -and -not $seen.Contains([int]$subprocess.ProcessId)) {
        $queued.Enqueue([int]$subprocess.ProcessId)
      }
    }
  }
  if ($ids.Count -eq 0) { throw 'No trustworthy packaged native STOP process exists' }
  return $ids.ToArray()
}
function Wait-Window {
  param([int]$RootPid, [string]$ExpectedTitle, [datetime]$Deadline, $Child)
  while ([DateTime]::UtcNow -lt $Deadline) {
    $Child.Refresh()
    if ($Child.HasExited) { throw 'Packaged native emergency STOP exited before showing required dialog' }
    $trusted = [uint32[]]@(Get-TrustedPackagedProcessFamily -RootPid $RootPid -PackagedExe $Exe)
    $window = [AutosportStopKeyboardWindow]::Find($trusted, $ExpectedTitle)
    if ($window -ne [IntPtr]::Zero) { return $window }
    Start-Sleep -Milliseconds 100
  }
  throw 'Timed out waiting for process-owned native emergency STOP dialog'
}
function Send-RealKeyboard {
  param([IntPtr]$Hwnd, [string]$Keys)
  $focused = $false
  for ($attempt = 0; $attempt -lt 12; $attempt++) {
    if ([AutosportStopKeyboardWindow]::Focus($Hwnd)) { $focused = $true; break }
    Start-Sleep -Milliseconds 100
  }
  if (-not $focused) { throw 'Native stop dialog could not be foregrounded; refusing to send keys to a foreign window' }
  [System.Windows.Forms.SendKeys]::SendWait($Keys)
}
function Assert-ProcessExit {
  param($Child, [int]$Expected, [datetime]$Deadline)
  $remaining = [int][Math]::Max(1, [Math]::Ceiling(($Deadline - [DateTime]::UtcNow).TotalMilliseconds))
  if (-not $Child.WaitForExit($remaining)) { throw 'Packaged native STOP did not terminate within bound' }
  if ($Child.ExitCode -ne $Expected) { throw "Packaged native STOP exit mismatch (expected $Expected)" }
}
function Invoke-OneStopCase {
  param([string]$CaseName, [bool]$Accept)
  $workspaceName = if ($Accept) { 'Профіль з пробілами' } else { 'declined' }
  $localName = if ($Accept) { 'local-yes' } else { 'local-no' }
  $workspace = Join-Path -Path $root -ChildPath $workspaceName
  $localApp = Join-Path -Path $root -ChildPath $localName
  New-Item -ItemType Directory -Path $workspace,$localApp -Force | Out-Null
  $env:AUTOSPORT_WORKSPACE = $workspace
  $env:LOCALAPPDATA = $localApp
  $env:WEBVIEW2_BROWSER_EXECUTABLE_FOLDER = Join-Path $root 'NO_WEBVIEW2_RUNTIME_PRESENT'
  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  $child = $null
  $verifiedFamily = @()
  try {
    $child = Start-Process -FilePath $Exe -ArgumentList '--native-emergency-stop' -WorkingDirectory ([IO.Path]::GetDirectoryName($Exe)) -PassThru
    $prompt = Wait-Window -RootPid $child.Id -ExpectedTitle $promptTitle -Deadline $deadline -Child $child
    $verifiedFamily = @(Get-TrustedPackagedProcessFamily -RootPid $child.Id -PackagedExe $Exe)
    # IDNO=7 must be the actual native dialog default, not only a label claim.
    $defaultIsNo = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
      if ([AutosportStopKeyboardWindow]::DefaultButtonId($prompt) -eq 7) { $defaultIsNo = $true; break }
      Start-Sleep -Milliseconds 50
    }
    if (-not $defaultIsNo) { throw 'Native STOP prompt does not default to No (IDNO=7)' }
    if ($Accept) {
      Send-RealKeyboard -Hwnd $prompt -Keys '{LEFT}{ENTER}'
      $result = Wait-Window -RootPid $child.Id -ExpectedTitle $resultTitle -Deadline $deadline -Child $child
      Send-RealKeyboard -Hwnd $result -Keys '{ENTER}'
      Assert-ProcessExit -Child $child -Expected 0 -Deadline $deadline
      if (-not (Test-Path -LiteralPath (Join-Path $workspace 'execution-stop.jsonl') -PathType Leaf)) {
        throw 'Native STOP reported success without durable journal'
      }
      $env:AUTOSPORT_WORKSPACE = $workspace
      & python -c 'from autosport.windows_emergency_stop import WindowsEmergencyStopBridge; from autosport.paths import default_workspace; s=WindowsEmergencyStopBridge.for_workspace(default_workspace()).status(); assert s.execution_blocked and s.integrity_confirmed and s.initialized and s.revision is not None and s.revision >= 1' 2>&1 | Out-Null
      if ($LASTEXITCODE -ne 0) { throw 'Independent STOP-authority reread did not prove blocked execution' }
      $status = 'PASS_NATIVE_KEYBOARD_STOP_DURABLE'
    } else {
      Send-RealKeyboard -Hwnd $prompt -Keys '{ENTER}'
      Assert-ProcessExit -Child $child -Expected 3 -Deadline $deadline
      if (Test-Path -LiteralPath (Join-Path $workspace 'execution-stop.jsonl')) {
        throw 'Default-No native STOP unexpectedly wrote a durable journal'
      }
      $status = 'PASS_NATIVE_KEYBOARD_DEFAULT_NO_INERT'
    }
    return [pscustomobject]@{ case = $CaseName; result = $status }
  } finally {
    if ($null -ne $child) {
      # Preserve already-observed same-image child PIDs if bootloader exited.
      $nowTrusted = @()
      try { $nowTrusted = @(Get-TrustedPackagedProcessFamily -RootPid $child.Id -PackagedExe $Exe) } catch {}
      foreach ($trustedPid in @($nowTrusted + $verifiedFamily | Select-Object -Unique)) {
        try {
          $record = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $trustedPid" -ErrorAction SilentlyContinue
          if ($null -ne $record -and [string]::Equals([string]$record.ExecutablePath, $Exe, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Process -Id ([int]$trustedPid) -Force -ErrorAction SilentlyContinue
          }
        } catch {}
      }
      $child.Dispose()
    }
  }
}
try {
  $rows += Invoke-OneStopCase -CaseName 'default_no' -Accept $false
  $rows += Invoke-OneStopCase -CaseName 'explicit_yes' -Accept $true
  if ((Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $executableSha256) {
    throw 'Packaged STOP executable bytes changed during qualification'
  }
  $report = [ordered]@{ status = 'PASS'; evidence_class = 'REAL_WINDOWS_PACKAGED_NATIVE_KEYBOARD'; packaged_executable_sha256 = $executableSha256; case_results = $rows; webview_uia = 'NOT_TESTED'; physical_nvda = $false; real_money_execution = $false }
  $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Output -Encoding utf8
} catch {
  $report = [ordered]@{ status = 'FAIL'; evidence_class = 'REAL_WINDOWS_PACKAGED_NATIVE_KEYBOARD'; packaged_executable_sha256 = $executableSha256; completed_case_results = $rows; webview_uia = 'NOT_TESTED'; physical_nvda = $false; real_money_execution = $false; diagnostic = 'Native keyboard path did not complete its strict Windows gate' }
  $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Output -Encoding utf8
  throw
} finally {
  if ($hadWorkspace) { $env:AUTOSPORT_WORKSPACE = $originalWorkspace } else { Remove-Item Env:AUTOSPORT_WORKSPACE -ErrorAction SilentlyContinue }
  if ($hadLocal) { $env:LOCALAPPDATA = $originalLocal } else { Remove-Item Env:LOCALAPPDATA -ErrorAction SilentlyContinue }
  if ($hadWebView) { $env:WEBVIEW2_BROWSER_EXECUTABLE_FOLDER = $originalWebView } else { Remove-Item Env:WEBVIEW2_BROWSER_EXECUTABLE_FOLDER -ErrorAction SilentlyContinue }
}