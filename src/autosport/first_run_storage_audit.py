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
from .webview2_release_environment import active_webview2_environment_overrides


def run_first_run_storage_audit(output_path: str | Path) -> int:
    """Project packaged first-run storage identity without opening runtime state."""

    destination = Path(output_path)
    failure_stage = "workspace_identity"
    try:
        workspace = default_workspace()
        failure_stage = "webview_storage_identity"
        webview_storage = default_webview_storage_path()
        failure_stage = "launch_cwd_identity"
        launch_cwd = Path.cwd()

        if not workspace.is_absolute():
            raise ValueError("workspace identity is not absolute")
        if not webview_storage.is_absolute():
            raise ValueError("WebView storage identity is not absolute")
        if not launch_cwd.is_absolute():
            raise ValueError("launch working directory is not absolute")
        failure_stage = "storage_root_separation"
        validate_product_storage_roots(workspace, webview_storage)
        failure_stage = "webview_release_environment"
        if active_webview2_environment_overrides():
            raise ValueError("release-sensitive WebView2 environment override is active")
        failure_stage = "workspace_writability"
        probe_workspace_writable(workspace)
        failure_stage = "webview_storage_writability"
        probe_webview_storage_writable(webview_storage)

        payload = {
            "status": "PASS",
            "workspace": str(workspace),
            "webview_storage": str(webview_storage),
            "launch_cwd": str(launch_cwd),
            "webview_environment_overrides_clear": True,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        atomic_write_json(destination, payload)
        return 0
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "error": "first-run storage preflight failed",
            "failure_stage": failure_stage,
            "error_type": type(exc).__name__,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        atomic_write_json(destination, payload)
        return 1
