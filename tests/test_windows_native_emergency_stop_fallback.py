from __future__ import annotations

import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport import windows_entry
from autosport.windows_emergency_stop import WindowsEmergencyStopBridge


class _NativeDialog:
    def __init__(self, responses: tuple[int, ...]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, int]] = []

    def MessageBoxW(self, _parent: object, message: str, title: str, flags: int) -> int:
        self.calls.append((message, title, flags))
        return self.responses.pop(0) if self.responses else 2


class _DialogUser32Proxy:
    """Mock MessageBoxW only; retain every other real Windows user32 API."""

    def __init__(self, original: object | None, dialog: _NativeDialog) -> None:
        self._original = original
        self._dialog = dialog

    def MessageBoxW(self, parent: object, message: str, title: str, flags: int) -> int:
        return self._dialog.MessageBoxW(parent, message, title, flags)

    def __getattr__(self, name: str) -> object:
        if self._original is None:
            raise AttributeError(name)
        return getattr(self._original, name)


class _DialogWinDLLProxy:
    """Preserve shell32 needed by the real durable STOP authority on Windows."""

    def __init__(self, original: object | None, dialog: _NativeDialog) -> None:
        self._original = original
        existing_user32 = getattr(original, "user32", None) if original is not None else None
        self.user32 = _DialogUser32Proxy(existing_user32, dialog)

    def __getattr__(self, name: str) -> object:
        if self._original is None:
            raise AttributeError(name)
        return getattr(self._original, name)


def _dialogs(monkeypatch: pytest.MonkeyPatch, *responses: int) -> _NativeDialog:
    native = _NativeDialog(tuple(responses))
    monkeypatch.setattr(
        ctypes, "windll", _DialogWinDLLProxy(getattr(ctypes, "windll", None), native),
        raising=False,
    )
    return native


def test_dialog_fixture_keeps_real_shell32_and_other_win32_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell32 = object()
    kernel32 = object()
    other_user32 = object()
    original = SimpleNamespace(
        shell32=shell32,
        kernel32=kernel32,
        user32=SimpleNamespace(GetWindowLongW=other_user32),
    )
    monkeypatch.setattr(ctypes, "windll", original, raising=False)
    dialog = _dialogs(monkeypatch, 7)
    assert ctypes.windll.shell32 is shell32
    assert ctypes.windll.kernel32 is kernel32
    assert ctypes.windll.user32.GetWindowLongW is other_user32
    assert ctypes.windll.user32.MessageBoxW(None, "Повідомлення", "Тема", 0x104) == 7
    assert len(dialog.calls) == 1


def test_webview_failure_native_choice_defaults_to_no_and_does_not_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native = _dialogs(monkeypatch, 7)  # IDNO
    def forbidden(_cls: type, _workspace: Path) -> object:
        raise AssertionError("STOP must not be published without explicit YES")
    monkeypatch.setattr(
        WindowsEmergencyStopBridge, "for_workspace", classmethod(forbidden)
    )
    windows_entry._offer_native_emergency_stop(tmp_path, "Помилка WebView2.")
    assert len(native.calls) == 1
    text, _title, flags = native.calls[0]
    assert "WebView2" in text
    assert "стійкий журнал" in text
    assert flags & 0x00000004  # native Yes/No buttons
    assert flags & 0x00000100  # default is the second (No) button


@pytest.mark.parametrize("non_yes", [0, 2, 7, -1])
def test_native_fallback_rejects_cancel_escape_and_unknown_dialog_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, non_yes: int
) -> None:
    native = _dialogs(monkeypatch, non_yes)
    monkeypatch.setattr(
        WindowsEmergencyStopBridge,
        "for_workspace",
        classmethod(lambda _cls, _path: pytest.fail("unexpected STOP activation")),
    )
    windows_entry._offer_native_emergency_stop(tmp_path, "Помилка.")
    assert len(native.calls) == 1


def test_explicit_yes_publishes_once_and_announces_confirmed_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    native = _dialogs(monkeypatch, 6, 1)  # Yes, then OK
    calls: list[str] = []
    class Stop:
        def activate(self) -> SimpleNamespace:
            calls.append("activate")
            return SimpleNamespace(stopped=True, message_uk="STOP підтверджено: ревізія 3.")
    monkeypatch.setattr(
        WindowsEmergencyStopBridge, "for_workspace",
        classmethod(lambda _cls, workspace: Stop() if workspace == tmp_path else pytest.fail("wrong workspace")),
    )
    windows_entry._offer_native_emergency_stop(tmp_path, "Збій WebView2.")
    assert calls == ["activate"]
    assert len(native.calls) == 2
    assert "STOP підтверджено" in native.calls[1][0]
    assert native.calls[1][2] == 0x00000040


@pytest.mark.parametrize("failure", ["rejected", "exception"])
def test_unknown_or_failed_stop_never_claims_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    native = _dialogs(monkeypatch, 6, 1)
    class Stop:
        def activate(self) -> SimpleNamespace:
            if failure == "exception":
                raise OSError("secret filesystem detail")
            return SimpleNamespace(stopped=False, message_uk="непідтверджено")
    monkeypatch.setattr(
        WindowsEmergencyStopBridge, "for_workspace",
        classmethod(lambda _cls, _workspace: Stop()),
    )
    windows_entry._offer_native_emergency_stop(tmp_path, "Збій WebView2.")
    assert len(native.calls) == 2
    assert "НЕ ПІДТВЕРДЖЕНО" in native.calls[1][0]
    assert "Не вважайте нові виконання заблокованими" in native.calls[1][0]
    assert "secret filesystem detail" not in native.calls[1][0]
    assert native.calls[1][2] == 0x00000010


def test_missing_native_api_falls_back_to_native_error_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delattr(ctypes, "windll", raising=False)
    seen: list[str] = []
    monkeypatch.setattr(windows_entry, "_show_startup_error", seen.append)
    windows_entry._offer_native_emergency_stop(tmp_path, "Рушій недоступний.")
    assert seen == ["Рушій недоступний."]


def test_unavailable_webview_environment_invokes_native_fallback_before_return(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from autosport import webview2_release_environment

    monkeypatch.setattr(
        webview2_release_environment,
        "active_webview2_environment_overrides",
        lambda: ("hostile_renderer_override",),
    )
    seen: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        windows_entry,
        "_offer_native_emergency_stop",
        lambda workspace, reason: seen.append((workspace, reason)),
    )
    storage = tmp_path / "webview-storage"
    assert windows_entry._run_owned_interactive_gui(tmp_path, storage) == 3
    assert seen == [(tmp_path, windows_entry._WEBVIEW2_STARTUP_ERROR)]


@pytest.mark.parametrize(
    "fault_stage",
    ("controller-import", "shell-import", "controller", "bridge", "renderer"),
)
def test_broken_packaged_shell_keeps_native_stop_choice_without_secret_leak(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault_stage: str
) -> None:
    """Missing modules and unexpected UI exceptions must never remove native STOP."""
    import sys
    import types

    from autosport import webview2_release_environment, webview2_runtime_deployment

    secret = "API_KEY=SECRET123456789"
    monkeypatch.setattr(
        webview2_release_environment,
        "active_webview2_environment_overrides",
        lambda: (),
    )
    monkeypatch.setattr(
        webview2_runtime_deployment,
        "ensure_webview2_runtime",
        lambda: SimpleNamespace(available=True),
    )
    offered: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        windows_entry,
        "_offer_native_emergency_stop",
        lambda workspace, message: offered.append((workspace, message)),
    )

    class Controller:
        def __init__(self, workspace: Path) -> None:
            if fault_stage == "controller":
                raise RuntimeError(secret)
            self.workspace = workspace

    class Bridge:
        def __init__(self, controller: Controller) -> None:
            if fault_stage == "bridge":
                raise RuntimeError(secret)
            self.controller = controller

    class WebViewUnavailable(RuntimeError):
        pass

    def launch(_bridge: Bridge, *, storage_path: Path) -> int:
        assert storage_path == tmp_path / "webview-storage"
        if fault_stage == "renderer":
            raise RuntimeError(secret)
        raise AssertionError("a fault is required for this test")

    controller_module = types.ModuleType("autosport.windows_webview_emergency_stop")
    controller_module.EmergencyStopWebController = Controller
    shell_module = types.ModuleType("autosport.windows_webview_shell")
    shell_module.AutosportWebBridge = Bridge
    shell_module.WindowsWebViewUnavailable = WebViewUnavailable
    shell_module.launch_windows_shell = launch

    monkeypatch.setitem(
        sys.modules,
        "autosport.windows_webview_emergency_stop",
        None if fault_stage == "controller-import" else controller_module,
    )
    monkeypatch.setitem(
        sys.modules,
        "autosport.windows_webview_shell",
        None if fault_stage == "shell-import" else shell_module,
    )
    assert (
        windows_entry._run_owned_interactive_gui(
            tmp_path, tmp_path / "webview-storage"
        )
        == 3
    )
    assert offered == [(tmp_path, windows_entry._WEBVIEW2_STARTUP_ERROR)]
    assert secret not in offered[0][1]


def test_independent_native_stop_works_with_webview_unavailable_and_unicode_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An independently launched executable must STOP while the GUI is unusable."""
    from autosport import paths, webview2_runtime_deployment

    workspace = tmp_path / "Профіль з пробілами"
    workspace.mkdir()
    monkeypatch.setattr(paths, "default_workspace", lambda: workspace)
    monkeypatch.setattr(
        windows_entry,
        "_run_interactive_gui",
        lambda: pytest.fail("independent native STOP must not open or lock the GUI"),
    )
    monkeypatch.setattr(
        webview2_runtime_deployment,
        "ensure_webview2_runtime",
        lambda: pytest.fail("independent native STOP must not require WebView2"),
    )
    native = _dialogs(monkeypatch, 6, 1)  # keyboard-operable native Yes, then OK
    assert windows_entry.main(["--native-emergency-stop"]) == 0
    assert len(native.calls) == 2
    prompt, _title, flags = native.calls[0]
    assert "без браузера та WebView2" in prompt
    assert "WebView2 недоступний" not in prompt
    assert flags & 0x00000004
    assert flags & 0x00000100  # No remains the default
    assert "підтверджено" in native.calls[1][0]
    assert WindowsEmergencyStopBridge.for_workspace(workspace).status().execution_blocked
    assert (workspace / "execution-stop.jsonl").is_file()


@pytest.mark.parametrize("non_yes", [0, 2, 7, -1])
def test_independent_native_stop_no_cancel_and_unknown_do_not_mutate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, non_yes: int
) -> None:
    from autosport import paths

    monkeypatch.setattr(paths, "default_workspace", lambda: tmp_path)
    native = _dialogs(monkeypatch, non_yes)
    monkeypatch.setattr(
        WindowsEmergencyStopBridge,
        "for_workspace",
        classmethod(lambda _cls, _path: pytest.fail("NO must not open STOP authority")),
    )
    assert windows_entry.main(["--native-emergency-stop"]) == 3
    assert len(native.calls) == 1
    assert not (tmp_path / "execution-stop.jsonl").exists()


@pytest.mark.parametrize("failure", ["uncertain", "exception"])
def test_independent_native_stop_never_reports_unconfirmed_write_as_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    from autosport import paths

    monkeypatch.setattr(paths, "default_workspace", lambda: tmp_path)
    native = _dialogs(monkeypatch, 6, 1)

    class FailedBridge:
        def activate(self) -> SimpleNamespace:
            if failure == "exception":
                raise OSError("secret=unsafe diagnostic text")
            return SimpleNamespace(stopped=False, message_uk="непідтверджено")

    monkeypatch.setattr(
        WindowsEmergencyStopBridge,
        "for_workspace",
        classmethod(lambda _cls, _path: FailedBridge()),
    )
    assert windows_entry.main(["--native-emergency-stop"]) == 3
    assert len(native.calls) == 2
    assert "НЕ ПІДТВЕРДЖЕНО" in native.calls[1][0]
    assert "secret=unsafe" not in native.calls[1][0]
    assert native.calls[1][2] == 0x00000010


def test_independent_native_stop_rejects_invalid_workspace_and_arity(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from autosport import paths

    monkeypatch.setattr(paths, "default_workspace", lambda: Path("relative"))
    errors: list[str] = []
    monkeypatch.setattr(windows_entry, "_show_workspace_configuration_error", errors.append)
    assert windows_entry.main(["--native-emergency-stop", "unexpected"]) == 2
    assert errors == []
    assert windows_entry.main(["--native-emergency-stop"]) == 2
    assert errors == [""]
