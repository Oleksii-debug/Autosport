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
import threading
import time

BUTTON_ID = "autosport-webview2-uia-control"
BUTTON_NAME = "Autosport WebView2 calibration button"
HOST_WINDOW_TITLE = "Independent WebView2 UIA control"
HTML = ("<!doctype html><html lang='en'><meta charset='utf-8'><body>"
        "<button id='" + BUTTON_ID + "'>" + BUTTON_NAME + "</button></body></html>")
CODES = {"STARTED", "WINDOWS_REQUIRED", "HOST_NOT_READY", "HOST_EXITED",
         "NO_VISIBLE_HOST_WINDOW", "FOREGROUND_NOT_CLAIMED", "PROBE_ENTERED",
         "PROBE_TIMEOUT", "PROBE_NO_REPORT", "PROBE_EXCEPTION",
         "PROBE_CLR_IMPORT_ERROR", "PROBE_UIA_LOAD_ERROR",
         "PROBE_UIA_CALL_ERROR", "PROBE_PROPERTY_ERROR",
         "NO_FOCUSED_ELEMENT", "FOREIGN_FOCUS", "SEMANTIC_MISMATCH",
         "WRONG_BUTTON_SEMANTICS", "WRONG_AUTOMATION_ID",
         "SEMANTIC_BUTTON_RETURNED", "DOM_FOCUS_NOT_CONFIRMED",
         "DOM_FOCUS_EXCEPTION", "DOM_FOCUS_TIMEOUT"}

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

FOCUS_JS = (
    "(function(){"
    "const target=document.getElementById('autosport-webview2-uia-control');"
    "if(!target||target.tagName!=='BUTTON'||target.disabled)return false;"
    "target.focus({preventScroll:true});"
    "return document.activeElement===target && "
    "document.activeElement.id==='autosport-webview2-uia-control';"
    "})()"
)

def focus_status(value: object) -> str:
    # JavaScript must return a real Boolean; truthy strings/integers are not proof.
    return "DOM_FOCUS_CONFIRMED" if value is True else "DOM_FOCUS_NOT_CONFIRMED"

def host(ready: Path, profile: Path, *, file_origin: bool = False) -> int:
    import webview
    # The product loads a local packaged HTML file, while the original
    # independent control used inline HTML. Qualify both origin modes without
    # changing any UIA identity/acceptance predicate.
    if file_origin:
        html_file = profile.parent / "control.html"
        html_file.write_text(HTML, encoding="utf-8")
        document = {"url": str(html_file)}
    else:
        document = {"html": HTML}
    window = webview.create_window(HOST_WINDOW_TITLE,
                                   width=640, height=480, **document)
    focus_request = ready.with_name("focus-request")
    focus_result = ready.with_name("focus-result")
    loaded_once = threading.Event()

    def focus_after_foreground() -> None:
        # Wait for the controller to verify native foreground BEFORE asking
        # WebView2 to focus the DOM. The old test focused first, then changed
        # OS foreground, so its READY marker was not a retained-focus witness.
        deadline = time.monotonic() + 15
        while not focus_request.is_file() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not focus_request.is_file():
            return  # Controller emits bounded DOM_FOCUS_TIMEOUT.
        try:
            result = focus_status(window.evaluate_js(FOCUS_JS))
        except Exception:
            result = "DOM_FOCUS_EXCEPTION"
        focus_result.write_text(result + "\n", encoding="ascii")

    def loaded() -> None:
        if loaded_once.is_set():
            return
        loaded_once.set()
        try:
            threading.Thread(target=focus_after_foreground, daemon=True).start()
            ready.write_text("LOADED\n", encoding="ascii")
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

def visible_window(root_pid: int, pids: set[int]) -> int:
    """Select the single exact host HWND, never an arbitrary family window.

    A renderer/utility process may expose its own visible top-level window.
    Its position in EnumWindows is not proof that it is the host to activate.
    Ambiguity fails closed; the caller retries only while the host is starting.
    """
    if root_pid <= 0 or root_pid not in pids:
        return 0
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)
    ]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    handles: list[int] = []

    def inspect(hwnd: int, _unused: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        owner = ctypes.c_ulong()
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner)):
            return True
        if int(owner.value) != root_pid:
            return True
        title_length = user32.GetWindowTextLengthW(hwnd)
        if title_length != len(HOST_WINDOW_TITLE):
            return True
        title = ctypes.create_unicode_buffer(title_length + 1)
        if user32.GetWindowTextW(hwnd, title, title_length + 1) != title_length:
            return True
        if title.value == HOST_WINDOW_TITLE:
            handles.append(int(hwnd))
        return len(handles) < 2

    callback = callback_type(inspect)
    user32.EnumWindows(callback, None)
    return handles[0] if len(handles) == 1 else 0

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

def classify(pid: int, allowed: set[int], role: str, name: str, automation_id: str) -> str:
    if pid not in allowed:
        return "FOREIGN_FOCUS"
    if role != "ControlType.Button" or name != BUTTON_NAME:
        return "WRONG_BUTTON_SEMANTICS"
    if automation_id != BUTTON_ID:
        return "WRONG_AUTOMATION_ID"
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
        Write-Output ('WRONG_BUTTON_SEMANTICS|' + $exactId); exit 1
    }
    if ($exactId -ne '1') { Write-Output 'WRONG_AUTOMATION_ID|0'; exit 1 }
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
            ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Mta", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
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
        child.returncode == 0 and code == "SEMANTIC_BUTTON_RETURNED" and exact == "1"
    ) else 1

def run(report: Path, *, file_origin: bool = False) -> int:
    if os.name != "nt":
        emit(report, "WINDOWS_REQUIRED")
        return 1
    emit(report, "STARTED")
    with tempfile.TemporaryDirectory(
        prefix="autosport-webview2-uia-", ignore_cleanup_errors=True
    ) as root:
        directory = Path(root)
        ready = directory / "ready"
        focus_request = directory / "focus-request"
        focus_result = directory / "focus-result"
        child = subprocess.Popen(command("host-file" if file_origin else "host", str(ready), str(directory / "profile")),
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
                hwnd = visible_window(child.pid, family(child.pid))
                if not hwnd:
                    time.sleep(0.2)
            if not hwnd:
                emit(report, "NO_VISIBLE_HOST_WINDOW")
                return 1
            if not foreground(hwnd):
                emit(report, "FOREGROUND_NOT_CLAIMED")
                return 1
            focus_request.write_text("FOCUS\n", encoding="ascii")
            focus_deadline = time.monotonic() + 18
            while not focus_result.is_file() and time.monotonic() < focus_deadline:
                if child.poll() is not None:
                    emit(report, "HOST_EXITED")
                    return 1
                time.sleep(0.1)
            if not focus_result.is_file():
                emit(report, "DOM_FOCUS_TIMEOUT")
                return 1
            try:
                focus_code = focus_result.read_text(encoding="ascii").strip()
            except OSError:
                focus_code = "DOM_FOCUS_NOT_CONFIRMED"
            if focus_code != "DOM_FOCUS_CONFIRMED":
                emit(report, focus_code if focus_code in {
                    "DOM_FOCUS_NOT_CONFIRMED", "DOM_FOCUS_EXCEPTION"
                } else "DOM_FOCUS_NOT_CONFIRMED")
                return 1
            emit(report, "PROBE_ENTERED")
            # The genuine COM client already runs in its own bounded pwsh MTA
            # process. Avoid a second windowed PyInstaller bootloader whose
            # exit code can differ from the completed child probe's result.
            probe_exit = probe(child.pid, report)
            try:
                data = json.loads(report.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                emit(report, "PROBE_NO_REPORT")
                return 1
            if data.get("status") == "PROBE_ENTERED":
                emit(report, "PROBE_NO_REPORT")
                return 1
            return 0 if (probe_exit == 0 and
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
    assert focus_status(True) == "DOM_FOCUS_CONFIRMED"
    assert all(focus_status(v) == "DOM_FOCUS_NOT_CONFIRMED" for v in (False, None, 1, "true"))
    assert classify(7, {7, 8}, "ControlType.Button", BUTTON_NAME, BUTTON_ID) == "SEMANTIC_BUTTON_RETURNED"
    assert classify(9, {7, 8}, "ControlType.Button", BUTTON_NAME, BUTTON_ID) == "FOREIGN_FOCUS"
    assert classify(7, {7}, "ControlType.Text", BUTTON_NAME, BUTTON_ID) == "WRONG_BUTTON_SEMANTICS"
    assert classify(7, {7}, "ControlType.Button", "wrong", BUTTON_ID) == "WRONG_BUTTON_SEMANTICS"
    assert classify(7, {7}, "ControlType.Button", BUTTON_NAME, "wrong-id") == "WRONG_AUTOMATION_ID"
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
    if len(sys.argv) == 4 and sys.argv[1] in {"host", "host-file"}:
        return host(Path(sys.argv[2]), Path(sys.argv[3]),
                    file_origin=sys.argv[1] == "host-file")
    if len(sys.argv) == 4 and sys.argv[1] == "probe":
        return probe(int(sys.argv[2]), Path(sys.argv[3]))
    if len(sys.argv) == 3 and sys.argv[1] in {"run", "run-file"}:
        return run(Path(sys.argv[2]).resolve(),
                   file_origin=sys.argv[1] == "run-file")
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
