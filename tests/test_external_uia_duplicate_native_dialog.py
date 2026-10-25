"""Exercise the shipped duplicate-dialog probe against real Windows UIA.

Native MessageBox fixtures qualify only the audit helper, never the product,
physical NVDA, or a human workflow. The packaged workflow remains mandatory.
"""

import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "external_uia_audit.ps1"
TITLE = "Автоспорт — помилка запуску"
COPY = "Автоспорт уже відкритий. Economic і live state не змінено."
pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None,
    reason="Real native MessageBox and System.Windows.Automation require Windows",
)


def _probe_source() -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("function Invoke-IsolatedDuplicateDialogProbe {")
    end = source.index("function Find-UiaRootWithElementForProcessFamily {", start)
    return source[start:end]


def _visible_handle(process: subprocess.Popen) -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        handles = []

        @callback_type
        def visit(handle, _state):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value == process.pid and user32.IsWindowVisible(handle):
                handles.append(int(handle))
            return True

        assert user32.EnumWindows(visit, 0), "Native test window census failed"
        if handles:
            assert len(handles) == 1
            return handles[0]
        assert process.poll() is None, "Native dialog fixture exited before UIA"
        time.sleep(0.05)
    pytest.fail("Native dialog fixture did not expose a visible top-level HWND")


@pytest.mark.parametrize(
    ("copy", "foreign_family", "expected"),
    [(COPY, False, "DIALOG_CLOSED"), (COPY, True, "NO_DIALOG"),
     ("Wrong recovery message", False, "COPY_MISMATCH")],
)
def test_real_native_dialog_probe(copy: str, foreign_family: bool, expected: str) -> None:
    child_code = (
        "import ctypes,sys; "
        f"ctypes.windll.user32.MessageBoxW(None, {copy!r}, {TITLE!r}, 0x10); "
        "sys.exit(2)"
    )
    child = subprocess.Popen([sys.executable, "-c", child_code])
    try:
        handle = _visible_handle(child)
        owner = os.getpid() if foreign_family else child.pid
        command = (
            "$ErrorActionPreference='Stop'\n" + _probe_source()
            + f"\nInvoke-IsolatedDuplicateDialogProbe -ProcessIds @({owner}) "
            + f"-WindowHandles @([IntPtr]::new({handle}))\n"
        )
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected
        if expected == "DIALOG_CLOSED":
            assert child.wait(timeout=10) == 2
        else:
            assert child.poll() is None, "Rejected dialog must not be dismissed"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
