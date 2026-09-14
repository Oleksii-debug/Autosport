from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


# NamedTemporaryFile isolates serialization for concurrent writers, but Windows can
# still reject simultaneous os.replace() calls that publish to the same destination.
# Keep only the final same-process publication syscall serialized; JSON encoding,
# flushing, and fsync remain independent/concurrent and cross-process RMW ownership
# stays with the higher-level durable-state locks.
_ATOMIC_JSON_PUBLISH_LOCK = threading.Lock()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_durable_file(path: str | Path) -> None:
    """Create an empty file when absent and fsync its current bytes without rewriting existing content."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if destination.exists() else "wb"
    with destination.open(mode) as handle:
        handle.flush()
        os.fsync(handle.fileno())


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        with _ATOMIC_JSON_PUBLISH_LOCK:
            os.replace(temporary, destination)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
