from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .integrity import durable_path_lock


_WORKSPACE_PROBE_PAYLOAD = b"autosport workspace atomic publish probe\n"
_WEBVIEW_PROBE_PAYLOAD = b"autosport webview storage write probe\n"


def _absolute_root(value: str | Path, *, label: str) -> Path:
    root = Path(value)
    if not root.is_absolute():
        raise ValueError(f"{label} storage probe root must be absolute")
    return root


def probe_workspace_writable(workspace: str | Path) -> None:
    """Prove canonical durable state can use the product's atomic publication boundary."""

    root = _absolute_root(workspace, label="workspace")
    source: Path | None = None
    destination: Path | None = None
    lock_path: Path | None = None

    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=root,
            prefix=".autosport-write-probe-source-",
            suffix=".tmp",
            delete=False,
        ) as probe:
            source = Path(probe.name)
            probe.write(_WORKSPACE_PROBE_PAYLOAD)
            probe.flush()
            os.fsync(probe.fileno())

        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=root,
            prefix=".autosport-write-probe-destination-",
            suffix=".tmp",
            delete=False,
        ) as published_probe:
            destination = Path(published_probe.name)
        lock_path = destination.with_name(f".{destination.name}.lock")

        with durable_path_lock(destination):
            os.replace(source, destination)
            source = None

        if destination.read_bytes() != _WORKSPACE_PROBE_PAYLOAD:
            raise OSError("workspace atomic replace did not publish expected probe bytes")
    finally:
        for candidate in (source, destination, lock_path):
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
