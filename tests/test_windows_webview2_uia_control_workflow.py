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


def test_file_origin_differential_preserves_strict_real_uia_and_truth_labels() -> None:
    """A local-file document must not silently inherit inline control success."""
    source = (ROOT / "scripts/windows_webview2_uia_control.py").read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'html_file = profile.parent / "control.html"' in source
    assert 'html_file.write_text(HTML, encoding="utf-8")' in source
    assert 'document = {"url": str(html_file)}' in source
    assert 'document = {"html": HTML}' in source
    assert 'file_origin=sys.argv[1] == "host-file"' in source
    assert 'file_origin=sys.argv[1] == "run-file"' in source
    assert '"host-file" if file_origin else "host"' in source
    assert 'child.returncode == 0 and code == "SEMANTIC_BUTTON_RETURNED"' in source
    assert 'and exact == "1"' in source
    assert '- name: Probe minimal WebView2 with killable independent UIA client' in workflow
    assert '- name: Probe local-file WebView2 origin with independent UIA client' in workflow
    assert '& ./dist/Autosport-WebView2-UIA-Control.exe run-file $reportPath' in workflow
    assert 'webview2-uia-control-file.json' in workflow
    file_probe = workflow.split(
        '- name: Probe local-file WebView2 origin with independent UIA client', 1
    )[1].split('- name: Preserve fixed diagnostic codes even on failure', 1)[0]
    assert "if ($observed -cne 'SEMANTIC_BUTTON_RETURNED')" in file_probe
    for guard in (
        '$evidence.exact_automation_id -ne $true',
        '$evidence.autosport_product_uia_pass -ne $false',
        '$evidence.human_tested -ne $false',
        '$evidence.nvda_verified -ne $false',
        '$evidence.real_money_execution -ne $false',
    ):
        assert guard in file_probe



def test_diagnostic_artifact_paths_are_real_yaml_block_lines() -> None:
    """A literal backslash-n in a YAML block indicator prevented any Windows job."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "path: |\\n" not in workflow
    assert (
        "          path: |\n"
        "            webview2-uia-control.json\n"
        "            webview2-uia-control-file.json\n"
    ) in workflow
    artifact_step = workflow.split("- name: Preserve fixed diagnostic codes even on failure", 1)[1]
    assert artifact_step.count("webview2-uia-control-file.json") == 1

def test_dom_focus_acknowledgment_is_real_boolean_and_after_native_foreground() -> None:
    """Neither loaded nor a truthy JS value is semantic/DOM focus evidence."""
    import importlib.util

    source_path = ROOT / "scripts/windows_webview2_uia_control.py"
    spec = importlib.util.spec_from_file_location("autosport_uia_control_focus", source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.focus_status(True) == "DOM_FOCUS_CONFIRMED"
    for value in (False, None, 0, 1, "true", {}, []):
        assert module.focus_status(value) == "DOM_FOCUS_NOT_CONFIRMED"
    assert "document.activeElement===target" in module.FOCUS_JS
    assert "document.activeElement.id==='autosport-webview2-uia-control'" in module.FOCUS_JS
    assert "DOM_FOCUS_TIMEOUT" in module.CODES
    assert "DOM_FOCUS_NOT_CONFIRMED" in module.CODES
    assert "DOM_FOCUS_EXCEPTION" in module.CODES
    source = source_path.read_text(encoding="utf-8")
    run_body = source.split("def run(", 1)[1].split("def selftest(", 1)[0]
    assert run_body.index("if not foreground(hwnd):") < run_body.index('focus_request.write_text("FOCUS')
    assert run_body.index('focus_code != "DOM_FOCUS_CONFIRMED"') < run_body.index('probe_exit = probe(child.pid, report)')
    assert 'ready.write_text("LOADED' in source
    assert 'window.evaluate_js(FOCUS_JS)' in source
    assert 'window.evaluate_js(VERIFY_JS)' in source
    assert 'publish_focus_status(result)' in source
    assert 'os.replace(pending, focus_result)' in source
    assert source.index('initial = focus_status(window.evaluate_js(FOCUS_JS))') < source.index('def run(')
    assert 'focus_status(window.evaluate_js(VERIFY_JS))' in source
    assert 'window.evaluate_js("document.getElementById(' not in source


def test_native_webview_focus_transfer_is_explicit_and_fail_closed() -> None:
    """DOM focus cannot substitute for the WinForms WebView2 focus transfer."""
    import importlib.util

    source_path = ROOT / "scripts/windows_webview2_uia_control.py"
    spec = importlib.util.spec_from_file_location("autosport_uia_control_native_focus", source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Control:
        def __init__(self, result: object, *, contains_focus: object = False) -> None:
            self.result = result
            self.ContainsFocus = contains_focus
            self.calls = 0

        def Focus(self) -> object:
            self.calls += 1
            return self.result

    class Browser:
        def __init__(self, control: Control) -> None:
            self.webview = control

    class Native:
        InvokeRequired = False

        def __init__(self, control: Control) -> None:
            self.browser = Browser(control)

    class Window:
        def __init__(self, control: Control) -> None:
            self.native = Native(control)

    accepted = Control(True)
    child_accepted = Control(False, contains_focus=True)
    rejected = Control(False)
    assert module.focus_native_webview(Window(accepted)) == "NATIVE_WEBVIEW_FOCUS_CONFIRMED"
    assert accepted.calls == 1
    assert module.focus_native_webview(Window(child_accepted)) == "NATIVE_WEBVIEW_FOCUS_CONFIRMED"
    assert child_accepted.calls == 1
    assert module.focus_native_webview(Window(rejected)) == "NATIVE_WEBVIEW_FOCUS_REJECTED"
    assert rejected.calls == 1
    assert module.focus_native_webview(object()) == "NATIVE_WEBVIEW_FOCUS_EXCEPTION"

    source = source_path.read_text(encoding="utf-8")
    host_body = source.split("def host(", 1)[1].split("def family(", 1)[0]
    assert host_body.index("focus_native_webview(window)") < host_body.index(
        "window.evaluate_js(VERIFY_JS)"
    )
    run_body = source.split("def run(", 1)[1].split("def selftest(", 1)[0]
    assert run_body.index('focus_code != "DOM_FOCUS_CONFIRMED"') < run_body.index(
        "probe_exit = probe(child.pid, report)"
    )
    assert "NATIVE_WEBVIEW_FOCUS_REJECTED" in module.CODES
    assert "NATIVE_WEBVIEW_FOCUS_EXCEPTION" in module.CODES
