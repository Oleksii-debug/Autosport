from __future__ import annotations

from pathlib import Path

from .integrity import atomic_write_json
from .paths import (
    default_webview_storage_path,
    default_workspace,
    validate_product_storage_roots,
)
from .storage_preflight import (
    probe_webview_storage_writable,
    probe_workspace_writable,
)


def run_first_run_storage_audit(output_path: str | Path) -> int:
    """Project packaged first-run storage identity without opening runtime state."""

    destination = Path(output_path)
    try:
        workspace = default_workspace()
        webview_storage = default_webview_storage_path()
        launch_cwd = Path.cwd()

        if not workspace.is_absolute():
            raise ValueError("workspace identity is not absolute")
        if not webview_storage.is_absolute():
            raise ValueError("WebView storage identity is not absolute")
        if not launch_cwd.is_absolute():
            raise ValueError("launch working directory is not absolute")
        validate_product_storage_roots(workspace, webview_storage)
        probe_workspace_writable(workspace)
        probe_webview_storage_writable(webview_storage)

        payload = {
            "status": "PASS",
            "workspace": str(workspace),
            "webview_storage": str(webview_storage),
            "launch_cwd": str(launch_cwd),
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        atomic_write_json(destination, payload)
        return 0
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "error": "first-run storage path resolution failed",
            "error_type": type(exc).__name__,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        atomic_write_json(destination, payload)
        return 1
