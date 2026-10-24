from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/windows-webview2-uia-control.yml"


def test_windowed_control_uses_terminal_semantic_report_not_bootloader_exit() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    step = source.split(
        "- name: Probe minimal WebView2 with killable independent UIA client", 1
    )[1].split("- name: Preserve fixed diagnostic codes even on failure", 1)[0]
    assert "& ./dist/Autosport-WebView2-UIA-Control.exe run $reportPath" in step
    assert "$launcherExit" not in step
    assert "$observed -cne 'SEMANTIC_BUTTON_RETURNED'" in step
    assert "$evidence.exact_automation_id -ne $true" in step
    assert "$evidence.autosport_product_uia_pass -ne $false" in step
    assert "$evidence.human_tested -ne $false" in step
    assert "$evidence.nvda_verified -ne $false" in step
    assert "$evidence.real_money_execution -ne $false" in step


def test_minimal_uia_semantic_success_requires_exact_button_identity() -> None:
    """Name/role alone must never make a diagnostic semantic result green."""
    import importlib.util

    source_path = ROOT / "scripts/windows_webview2_uia_control.py"
    spec = importlib.util.spec_from_file_location("autosport_uia_control_identity", source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.classify(7, {7}, "ControlType.Button", module.BUTTON_NAME, module.BUTTON_ID) == "SEMANTIC_BUTTON_RETURNED"
    assert module.classify(7, {7}, "ControlType.Button", module.BUTTON_NAME, "wrong-id") == "WRONG_AUTOMATION_ID"
    assert module.classify(7, {7}, "ControlType.Text", module.BUTTON_NAME, module.BUTTON_ID) == "WRONG_BUTTON_SEMANTICS"
    assert module.classify(7, {7}, "ControlType.Button", "wrong", module.BUTTON_ID) == "WRONG_BUTTON_SEMANTICS"
    source = source_path.read_text(encoding="utf-8")
    assert "if ($exactId -ne '1')" in source
    assert "Write-Output 'WRONG_AUTOMATION_ID|0'" in source
    assert "Write-Output ('WRONG_BUTTON_SEMANTICS|' + $exactId)" in source
    assert "if code not in CODES:" in source
    assert 'code == "SEMANTIC_BUTTON_RETURNED" and exact == "1"' in source


def test_control_host_hwnd_is_exact_and_ambiguous_windows_fail_closed(monkeypatch) -> None:
    """Native window enumeration cannot silently select a child or wrong host."""
    import ctypes
    import importlib.util

    source_path = ROOT / "scripts/windows_webview2_uia_control.py"
    spec = importlib.util.spec_from_file_location("autosport_uia_control_hwnd", source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeFunction:
        def __init__(self, fn):
            self.fn = fn

        def __call__(self, *args):
            return self.fn(*args)

    class FakeUser32:
        def __init__(self, windows):
            self.windows = windows
            self.EnumWindows = FakeFunction(self.enum)
            self.GetWindowThreadProcessId = FakeFunction(self.pid)
            self.IsWindowVisible = FakeFunction(self.visible)
            self.GetWindowTextLengthW = FakeFunction(self.title_length)
            self.GetWindowTextW = FakeFunction(self.title)

        def enum(self, callback, _context):
            for hwnd in self.windows:
                if not callback(hwnd, None):
                    break
            return True

        def pid(self, hwnd, output):
            output._obj.value = self.windows[hwnd][0]
            return 1

        def visible(self, hwnd):
            return self.windows[hwnd][2]

        def title_length(self, hwnd):
            return len(self.windows[hwnd][1])

        def title(self, hwnd, output, capacity):
            value = self.windows[hwnd][1]
            assert capacity > len(value)
            output.value = value
            return len(value)

    monkeypatch.setattr(module.ctypes, "WINFUNCTYPE", lambda *_: lambda fn: fn, raising=False)
    monkeypatch.setattr(module.ctypes, "WinDLL", lambda *_args, **_kw: user32, raising=False)
    user32 = FakeUser32({
        101: (71, module.HOST_WINDOW_TITLE, True),  # renderer/foreign child
        102: (70, "Unrelated application", True),  # same PID, wrong title
        103: (70, module.HOST_WINDOW_TITLE, True),  # sole genuine host
    })
    assert module.visible_window(70, {70, 71}) == 103
    assert module.visible_window(70, {71}) == 0
    assert module.visible_window(0, {0}) == 0
    user32.windows[104] = (70, module.HOST_WINDOW_TITLE, True)
    assert module.visible_window(70, {70, 71}) == 0  # ambiguous exact host
    user32.windows[103] = (70, module.HOST_WINDOW_TITLE, False)
    assert module.visible_window(70, {70, 71}) == 104
