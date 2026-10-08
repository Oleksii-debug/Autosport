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


def _dialogs(monkeypatch: pytest.MonkeyPatch, *responses: int) -> _NativeDialog:
    native = _NativeDialog(tuple(responses))
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(user32=native), raising=False
    )
    return native


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
