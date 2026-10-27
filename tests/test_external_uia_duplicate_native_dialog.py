"""Exercise the shipped duplicate-dialog probe against real Windows UIA.

Native MessageBox fixtures qualify only the audit helper, never the product,
physical NVDA, or a human workflow. The packaged workflow remains mandatory.
"""

import base64
import ctypes
from ctypes import wintypes
import os
import json
import runpy
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


def _family_ids(root_pid: int) -> set[int]:
    """Reuse the shipped OS census, bounded by a killable PowerShell process."""
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("function Get-ProcessFamilyIds {")
    end = source.index("# The duplicate-instance process census", start)
    command = (
        "$ErrorActionPreference='Stop'\n" + source[start:end]
        + f"\nConvertTo-Json -InputObject @(Get-ProcessFamilyIds -RootProcessId {root_pid}) -Compress\n"
    )
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, "Canonical process-family census failed"
    family = json.loads(result.stdout)
    assert type(family) is list and 1 <= len(family) <= 512
    assert all(type(pid) is int and pid > 0 for pid in family)
    assert root_pid in family
    return set(family)


def _visible_handle(process: subprocess.Popen, *, timeout: int = 15,
                    process_family: bool = False, single: bool = True) -> tuple[int, int]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    deadline = time.monotonic() + timeout
    next_family_refresh = 0.0
    allowed = {process.pid}
    while time.monotonic() < deadline:
        if process_family and time.monotonic() >= next_family_refresh:
            allowed = _family_ids(process.pid)
            next_family_refresh = time.monotonic() + 1
        handles = []

        @callback_type
        def visit(handle, _state):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value in allowed and user32.IsWindowVisible(handle):
                handles.append((int(owner.value), int(handle)))
            return True

        assert user32.EnumWindows(visit, 0), "Native test window census failed"
        if handles:
            if single:
                assert len(handles) == 1
            return handles[0]
        assert process.poll() is None, "Native dialog fixture exited before UIA"
        time.sleep(0.05)
    pytest.fail("Native dialog fixture did not expose a visible top-level HWND")


def _run_probe(owner: int, handle: int) -> str:
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
    return result.stdout.strip()



def _run_stack_probe(child_pid: int) -> dict:
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("function Get-DuplicatePythonStackDiagnostics {")
    end = source.index("# Diagnostic only: isolate FromHandle", start)
    command = (
        "$ErrorActionPreference='Stop'\n" + source[start:end]
        + f"\nGet-DuplicatePythonStackDiagnostics -ProcessIds @({os.getpid()},{child_pid}) "
        + f"-RootProcessId {os.getpid()} "
        + "-CaptureScript $env:UIA_CAPTURE_SCRIPT -SourceRoot $env:UIA_CAPTURE_ROOT "
        + "| ConvertTo-Json -Depth 8 -Compress\n"
    )
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=30, check=False,
        env=dict(os.environ,
            UIA_CAPTURE_SCRIPT=str(SCRIPT.parent / "capture_duplicate_python_diagnostic.py"),
            UIA_CAPTURE_ROOT=str(SCRIPT.parents[1] / "src" / "autosport")),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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
        window_owner, handle = _visible_handle(child)
        owner = os.getpid() if foreign_family else window_owner
        assert _run_probe(owner, handle) == expected
        if expected == "DIALOG_CLOSED":
            assert child.wait(timeout=10) == 2
        else:
            assert child.poll() is None, "Rejected dialog must not be dismissed"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


@pytest.mark.parametrize("packaged", (False, True), ids=("source", "pinned-frozen"))
def test_real_product_lock_refusal_reaches_native_uia(tmp_path: Path, packaged: bool) -> None:
    """Do not substitute a synthetic MessageBox for the actual startup path."""
    from autosport.workspace_lock import WorkspaceInteractiveLock

    packaged_exe = os.environ.get("AUTOSPORT_NATIVE_PRODUCT_EXE")
    if packaged and not packaged_exe:
        pytest.skip("Pinned independently verified packaged product was not supplied")
    if packaged:
        assert Path(packaged_exe).is_absolute() and Path(packaged_exe).is_file()

    workspace = tmp_path / "workspace-пробіл"
    local_app_data = tmp_path / "localappdata-пробіл"
    workspace.mkdir()
    local_app_data.mkdir()
    env = dict(os.environ, AUTOSPORT_WORKSPACE=str(workspace), LOCALAPPDATA=str(local_app_data))
    # Diagnostic workflow imports the exact checkout, not another installed tree.
    env["PYTHONPATH"] = str(SCRIPT.parents[1] / "src")
    command = [sys.executable, "-c", "from autosport.windows_entry import main; raise SystemExit(main([]))"]
    cwd = None
    if packaged:
        command = [packaged_exe]
        env.pop("PYTHONPATH", None)
        cwd = str(Path(os.environ["SystemRoot"]) / "System32")
    with WorkspaceInteractiveLock(workspace):
        child = subprocess.Popen(
            command, env=env, cwd=cwd,
        )
        try:
            window_owner, handle = _visible_handle(
                child, timeout=90 if packaged else 15, process_family=packaged,
            )
            if os.environ.get("AUTOSPORT_UIA_STACK_SAMPLER") and not packaged:
                stacks = _run_stack_probe(child.pid)
                assert stacks["status"] == "FAILURE_ONLY"
                assert len(stacks["samples"]) == 1
                sample = stacks["samples"][0]
                assert sample["status"] == "CAPTURED"
                assert any(frame["module"] == "windows_entry.py" for frame in sample["locations"])
                assert sample["human_tested"] is False and sample["nvda_verified"] is False
            assert _run_probe(window_owner, handle) == "DIALOG_CLOSED"
            assert child.wait(timeout=10) == 2
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def _failure_stack_snapshot(primary, duplicate):
    """Reuse the redacting sampler from this already-running host; no new Python."""
    samples = []
    sampler = os.environ.get("AUTOSPORT_UIA_STACK_SAMPLER")
    if sampler:
        try:
            capture = runpy.run_path(str(SCRIPT.parent / "capture_duplicate_python_diagnostic.py"))["capture"]
            for role, process in (("PRIMARY", primary), ("DUPLICATE", duplicate)):
                if process is None or process.poll() is not None:
                    continue
                for pid in sorted(_family_ids(process.pid) - {process.pid})[:8]:
                    value = capture(sampler, pid, SCRIPT.parents[1] / "src" / "autosport")
                    samples.append({"role": role, "sample": value})
                    if value.get("status") == "CAPTURED":
                        break
        except Exception:
            # A diagnostic failure must never replace the original test failure.
            samples.append({"status": "UNAVAILABLE"})
    print(json.dumps({
        "evidence_class": "FAILURE_ONLY_OWNED_FAMILY_STACK_LOCATIONS_NOT_ACCEPTANCE",
        "human_tested": False, "nvda_verified": False,
        "whole_product_complete": False, "real_money_execution": False,
        "samples": samples,
    }, sort_keys=True))


def test_packaged_primary_then_duplicate_without_primary_uia(tmp_path: Path):
    """Two real frozen instances, no AutomationElement access to the primary."""
    executable = os.environ.get("AUTOSPORT_NATIVE_PRODUCT_EXE")
    if not executable:
        pytest.skip("Pinned independently verified packaged product was not supplied")
    assert Path(executable).is_absolute() and Path(executable).is_file()
    workspace = tmp_path / "two-instance-workspace"
    local_app_data = tmp_path / "two-instance-localappdata"
    workspace.mkdir()
    local_app_data.mkdir()
    env = dict(os.environ, AUTOSPORT_WORKSPACE=str(workspace), LOCALAPPDATA=str(local_app_data))
    env.pop("PYTHONPATH", None)
    hostile = str(Path(os.environ["SystemRoot"]) / "System32")
    primary = subprocess.Popen([executable], cwd=hostile, env=env)
    duplicate = None
    try:
        startup_deadline = time.monotonic() + 20
        _visible_handle(primary, timeout=20, process_family=True, single=False)
        witness = workspace / "webview2-runtime-witness.json"
        while not witness.is_file() and time.monotonic() < startup_deadline:
            assert primary.poll() is None, "Primary exited before native runtime witness"
            time.sleep(0.1)
        assert witness.is_file(), "Primary native runtime witness missing"
        observed = json.loads(witness.read_text(encoding="utf-8"))
        assert observed["observation_source"] == "native_core_webview2_environment"
        assert observed["real_money_execution"] is False
        assert observed["human_tested"] is False and observed["nvda_verified"] is False
        # Allow the ordinary trusted-loaded callback to run before the second
        # launch; do not query the primary provider or inject any product API.
        time.sleep(1)
        assert primary.poll() is None, "Primary exited before second launch"
        duplicate = subprocess.Popen([executable], cwd=hostile, env=env)
        owner, handle = _visible_handle(duplicate, timeout=90, process_family=True)
        assert _run_probe(owner, handle) == "DIALOG_CLOSED"
        assert duplicate.wait(timeout=10) == 2
    except (AssertionError, pytest.fail.Exception, subprocess.SubprocessError):
        _failure_stack_snapshot(primary, duplicate)
        raise
    finally:
        # Disposable, explicitly spawned fixture roots only; never another app.
        for process in (duplicate, primary):
            if process is not None and process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False)
                process.wait(timeout=10)
