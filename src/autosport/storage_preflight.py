from __future__ import annotations

import json
import os
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
    """Prove canonical durable state can publish directly in the workspace root."""

    root = _absolute_root(workspace, label="workspace")
    destination: Path | None = None
    lock_path: Path | None = None

    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=root,
            prefix=".autosport-workspace-write-probe-",
            suffix=".json",
            delete=False,
        ) as reservation:
            destination = Path(reservation.name)
        destination.unlink()
        lock_path = destination.with_name(f".{destination.name}.lock")

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
        for candidate in (destination, lock_path):
            if candidate is None:
                continue
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


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
