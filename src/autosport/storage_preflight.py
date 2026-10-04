from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from .integrity import atomic_write_json


_WORKSPACE_PROBE_PAYLOAD = {
    "probe": "autosport workspace atomic publish probe",
    "schema_version": 1,
}
_WEBVIEW_PROBE_PAYLOAD = b"autosport webview storage write probe\n"


def _absolute_root(value: str | Path, *, label: str) -> Path:
    root = Path(value)
    if not root.is_absolute():
        raise ValueError(f"{label} storage probe root must be absolute")
    return root


def probe_workspace_writable(workspace: str | Path) -> None:
    """Prove canonical durable state can use the product's atomic publication boundary."""

    root = _absolute_root(workspace, label="workspace")
    probe_directory: Path | None = None

    root.mkdir(parents=True, exist_ok=True)
    try:
        probe_directory = Path(
            tempfile.mkdtemp(
                dir=root,
                prefix=".autosport-workspace-write-probe-",
            )
        )
        destination = probe_directory / "probe.json"
        atomic_write_json(destination, _WORKSPACE_PROBE_PAYLOAD)

        try:
            published = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OSError(
                "workspace canonical atomic publication probe could not be read back"
            ) from exc
        if published != _WORKSPACE_PROBE_PAYLOAD:
            raise OSError(
                "workspace canonical atomic publication probe readback did not match"
            )
    finally:
        if probe_directory is not None and probe_directory.exists():
            shutil.rmtree(probe_directory)


def probe_webview_storage_writable(storage_path: str | Path) -> None:
    """Prove the host process can durably write/read/delete inside the canonical UDF root."""

    root = _absolute_root(storage_path, label="WebView")
    probe_path: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=root,
            prefix=".autosport-webview-write-probe-",
            suffix=".tmp",
            delete=False,
        ) as probe:
            probe_path = Path(probe.name)
            probe.write(_WEBVIEW_PROBE_PAYLOAD)
            probe.flush()
            os.fsync(probe.fileno())
        if probe_path.read_bytes() != _WEBVIEW_PROBE_PAYLOAD:
            raise OSError("WebView storage probe readback did not match")
        probe_path.unlink()
        probe_path = None
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink()
            except FileNotFoundError:
                pass
