"""Plan 6 §2А/§2Б: canonical WebView host pre-window fault cleanup.

These source-level regressions use the real controller/bridge and stub only
browser dependencies. They are intentionally RED until the canonical #1598
source owner repairs pre-window errors. They are not Windows UIA/NVDA evidence.
No real-money or bookmaker access is exercised.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.windows_webview_shell as shell


def _preconstructed_bridge(tmp_path: Path):
    workspace = tmp_path / "українська робоча папка з пробілами"
    controller = shell.AutosportWebController(workspace)
    bridge = shell.AutosportWebBridge(controller)
    return controller, bridge


@pytest.mark.parametrize("fault", ["missing-webview", "relative-storage", "blocked-storage"])
def test_preconstructed_controller_closed_on_pre_window_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    controller, bridge = _preconstructed_bridge(tmp_path)
    storage = tmp_path / "webview profile з пробілами"
    if fault == "missing-webview":
        monkeypatch.setitem(sys.modules, "webview", None)
    elif fault == "relative-storage":
        storage = Path("relative-profile")
    else:
        def deny_storage(_storage: Path) -> None:
            raise shell.WindowsWebViewUnavailable(
                "WebView storage unavailable", reason="storage"
            )
        monkeypatch.setattr(shell, "_probe_webview_storage_writable", deny_storage)

    with pytest.raises(shell.WindowsWebViewUnavailable) as caught:
        shell.launch_windows_shell(bridge, storage_path=storage)

    assert controller._closing is True
    assert controller._close_complete is True
    if fault == "missing-webview":
        assert "pywebview is unavailable" in str(caught.value)
        assert isinstance(caught.value.__cause__, ImportError)


def test_preconstructed_controller_closed_when_runtime_witness_unlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, bridge = _preconstructed_bridge(tmp_path)
    # Only the native browser is substituted; the real controller and bridge
    # still own all application state and host teardown authority.
    monkeypatch.setitem(
        sys.modules, "webview",
        SimpleNamespace(settings={"WEBVIEW2_RUNTIME_PATH": None,
                                  "REMOTE_DEBUGGING_PORT": None}),
    )
    original_unlink = Path.unlink

    def deny_witness_unlink(path: Path, *args, **kwargs):
        if path.name == shell._WEBVIEW2_RUNTIME_WITNESS_FILENAME:
            raise PermissionError("SECRET_STALE_WITNESS_PATH")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_witness_unlink)
    with pytest.raises(shell.WindowsWebViewUnavailable) as caught:
        shell.launch_windows_shell(
            bridge, storage_path=tmp_path / "webview profile"
        )

    assert "could not invalidate" in str(caught.value)
    assert "SECRET_STALE_WITNESS_PATH" not in str(caught.value)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert controller._closing is True
    assert controller._close_complete is True
