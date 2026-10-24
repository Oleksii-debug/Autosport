"""Disposable packaged WebView2/UIA provider falsifier. Never Autosport acceptance."""
from __future__ import annotations
import base64
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

BUTTON_ID = "autosport-webview2-uia-control"
BUTTON_NAME = "Autosport WebView2 calibration button"
HTML = ("<!doctype html><html lang='en'><meta charset='utf-8'><body>"
        "<button id='" + BUTTON_ID + "'>" + BUTTON_NAME + "</button></body></html>")
CODES = {"STARTED", "WINDOWS_REQUIRED", "HOST_NOT_READY", "HOST_EXITED",
         "NO_VISIBLE_HOST_WINDOW", "FOREGROUND_NOT_CLAIMED", "PROBE_ENTERED",
         "PROBE_TIMEOUT", "PROBE_NO_REPORT", "PROBE_EXCEPTION",
         "PROBE_CLR_IMPORT_ERROR", "PROBE_UIA_LOAD_ERROR",
         "PROBE_UIA_CALL_ERROR", "PROBE_PROPERTY_ERROR",
         "NO_FOCUSED_ELEMENT", "FOREIGN_FOCUS", "SEMANTIC_MISMATCH",
         "SEMANTIC_BUTTON_RETURNED"}

def emit(path: Path, code: str, *, exact_id: bool = False) -> None:
    if code not in CODES:
        raise ValueError("unknown diagnostic code")
    data = {"schema_version": 1, "status": code,
            "scope": "independent packaged WebView2 diagnostic, not product",
            "exact_automation_id": bool(exact_id),
            "autosport_product_uia_pass": False,
            "real_money_execution": False, "human_tested": False,
            "nvda_verified": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".pending")
    tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)

def command(mode: str, *args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, mode, *args]
    return [sys.executable, str(Path(__file__).resolve()), mode, *args]

def host(ready: Path, profile: Path) -> int:
    import webview
    window = webview.create_window("Independent WebView2 UIA control",
                                   html=HTML, width=640, height=480)
    def loaded() -> None:
        try:
            window.evaluate_js("document.getElementById('" + BUTTON_ID + "').focus()")
            ready.write_text("READY\n", encoding="ascii")
        except Exception:
            pass  # Missing readiness is failure, not a synthetic PASS.
    window.events.loaded += loaded
    webview.start(gui="edgechromium", storage_path=str(profile))
    return 0

def family(root_pid: int) -> set[int]:
    import psutil
    try:
        proc = psutil.Process(root_pid)
        return {root_pid, *(p.pid for p in proc.children(recursive=True))}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return set()

def visible_window(pids: set[int]) -> int:
    if not pids:
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                                 ctypes.POINTER(ctypes.c_ulong)]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    handles: list[int] = []
    def inspect(hwnd: int, _unused: int) -> bool:
        owner = ctypes.c_ulong()
        if user32.IsWindowVisible(hwnd):
            if user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner)):
                if int(owner.value) in pids:
                    handles.append(int(hwnd))
        return len(handles) < 256
    callback = callback_type(inspect)
    user32.EnumWindows(callback, None)
    return handles[0] if handles else 0

def foreground(hwnd: int) -> bool:
    if not hwnd:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    if user32.GetForegroundWindow() == hwnd:
        return True
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd

def classify(pid: int, allowed: set[int], role: str, name: str) -> str:
    if pid not in allowed:
        return "FOREIGN_FOCUS"
    if role != "ControlType.Button" or name != BUTTON_NAME:
        return "SEMANTIC_MISMATCH"
    return "SEMANTIC_BUTTON_RETURNED"

# PowerShell 7's separate MTA process is the known-good UIA client used by the
# existing WinForms calibration. Pythonnet's UIAutomationClient assembly lookup
# is not reliable in a frozen PyInstaller probe (exact failure #40062303139).
# Only fixed codes cross stdout; no UIA names, HWNDs, PIDs or raw exceptions.
PS_PROBE = r"""
$ErrorActionPreference = 'Stop'
if ([System.Threading.Thread]::CurrentThread.GetApartmentState() -ne [System.Threading.ApartmentState]::MTA) {
    Write-Output 'PROBE_UIA_LOAD_ERROR|0'; exit 1
}
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
} catch {
    Write-Output 'PROBE_UIA_LOAD_ERROR|0'; exit 1
}
try {
    $element = [System.Windows.Automation.AutomationElement]::FocusedElement
} catch {
    Write-Output 'PROBE_UIA_CALL_ERROR|0'; exit 1
}
if ($null -eq $element) { Write-Output 'NO_FOCUSED_ELEMENT|0'; exit 1 }
try {
    $current = $element.Current
    $allowed = @($env:AUTOSPORT_UIA_ALLOWED_PIDS.Split(',') | ForEach-Object {
        [int]::Parse($_, [System.Globalization.CultureInfo]::InvariantCulture)
    })
    $actualPid = [int]$current.ProcessId
    $role = [string]$current.ControlType.ProgrammaticName
    $name = [string]$current.Name
    $exactId = if ([string]$current.AutomationId -ceq 'autosport-webview2-uia-control') { '1' } else { '0' }
    if ($allowed -notcontains $actualPid) {
        Write-Output ('FOREIGN_FOCUS|' + $exactId); exit 1
    }
    if ($role -cne 'ControlType.Button' -or $name -cne 'Autosport WebView2 calibration button') {
        Write-Output ('SEMANTIC_MISMATCH|' + $exactId); exit 1
    }
    Write-Output ('SEMANTIC_BUTTON_RETURNED|' + $exactId); exit 0
} catch {
    Write-Output 'PROBE_PROPERTY_ERROR|0'; exit 1
}
"""

def probe(root_pid: int, report: Path) -> int:
    allowed = family(root_pid)
    if not allowed:
        emit(report, "FOREIGN_FOCUS")
        return 1
    env = os.environ.copy()
    env["AUTOSPORT_UIA_ALLOWED_PIDS"] = ",".join(str(p) for p in sorted(allowed))
    encoded = base64.b64encode(PS_PROBE.encode("utf-16le")).decode("ascii")
    try:
        child = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=9, env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        emit(report, "PROBE_TIMEOUT")
        return 1
    except OSError:
        emit(report, "PROBE_EXCEPTION")
        return 1
    lines = child.stdout.strip().splitlines()
    if len(lines) != 1 or "|" not in lines[0]:
        emit(report, "PROBE_EXCEPTION")
        return 1
    code, exact = lines[0].split("|", 1)
    if code not in CODES or exact not in {"0", "1"}:
        emit(report, "PROBE_EXCEPTION")
        return 1
    emit(report, code, exact_id=exact == "1")
    return 0 if (
        child.returncode == 0 and code == "SEMANTIC_BUTTON_RETURNED"
    ) else 1

def run(report: Path) -> int:
    if os.name != "nt":
        emit(report, "WINDOWS_REQUIRED")
        return 1
    emit(report, "STARTED")
    with tempfile.TemporaryDirectory(
        prefix="autosport-webview2-uia-", ignore_cleanup_errors=True
    ) as root:
        directory = Path(root)
        ready = directory / "ready"
        child = subprocess.Popen(command("host", str(ready), str(directory / "profile")),
                                 cwd=root, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 35
            while not ready.is_file() and time.monotonic() < deadline:
                if child.poll() is not None:
                    emit(report, "HOST_EXITED")
                    return 1
                time.sleep(0.2)
            if not ready.is_file():
                emit(report, "HOST_NOT_READY")
                return 1
            hwnd = 0
            deadline = time.monotonic() + 12
            while not hwnd and time.monotonic() < deadline:
                hwnd = visible_window(family(child.pid))
                if not hwnd:
                    time.sleep(0.2)
            if not hwnd:
                emit(report, "NO_VISIBLE_HOST_WINDOW")
                return 1
            if not foreground(hwnd):
                emit(report, "FOREGROUND_NOT_CLAIMED")
                return 1
            emit(report, "PROBE_ENTERED")
            try:
                result = subprocess.run(command("probe", str(child.pid), str(report)),
                                        cwd=root, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=12, check=False)
            except subprocess.TimeoutExpired:
                emit(report, "PROBE_TIMEOUT")
                return 1
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                emit(report, "PROBE_NO_REPORT")
                return 1
            if data.get("status") == "PROBE_ENTERED":
                emit(report, "PROBE_NO_REPORT")
                return 1
            return 0 if (result.returncode == 0 and
                         data.get("status") == "SEMANTIC_BUTTON_RETURNED") else 1
        finally:
            try:
                import psutil
                descendants = psutil.Process(child.pid).children(recursive=True)
                for desc in reversed(descendants):
                    try:
                        desc.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                psutil.wait_procs(descendants, timeout=5)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            # A WebView2 browser may retain a file handle briefly after the
            # host exits. TemporaryDirectory ignores only cleanup errors on
            # this disposable CI runner; it must not override probe evidence.

def selftest() -> int:
    assert classify(7, {7, 8}, "ControlType.Button", BUTTON_NAME) == "SEMANTIC_BUTTON_RETURNED"
    assert classify(9, {7, 8}, "ControlType.Button", BUTTON_NAME) == "FOREIGN_FOCUS"
    assert classify(7, {7}, "ControlType.Text", BUTTON_NAME) == "SEMANTIC_MISMATCH"
    assert classify(7, {7}, "ControlType.Button", "wrong") == "SEMANTIC_MISMATCH"
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "report.json"
        emit(path, "SEMANTIC_BUTTON_RETURNED", exact_id=True)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["autosport_product_uia_pass"] is False
        assert data["human_tested"] is False and data["nvda_verified"] is False
    return 0

def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "selftest":
        return selftest()
    if len(sys.argv) == 4 and sys.argv[1] == "host":
        return host(Path(sys.argv[2]), Path(sys.argv[3]))
    if len(sys.argv) == 4 and sys.argv[1] == "probe":
        return probe(int(sys.argv[2]), Path(sys.argv[3]))
    if len(sys.argv) == 3 and sys.argv[1] == "run":
        return run(Path(sys.argv[2]).resolve())
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
