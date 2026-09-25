from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from scripts import package_windows


def _git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def test_write_exact_git_blob_closes_raw_fd_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = b"package payload"
    object_sha = _git_blob_sha(data)
    captured: dict[str, object] = {}
    real_mkstemp = package_windows.tempfile.mkstemp
    real_fstat = os.fstat

    def tracking_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        captured["fd"] = fd
        captured["name"] = name
        return fd, name

    def fail_fdopen(*args: object, **kwargs: object) -> object:
        raise OSError("fdopen boom")

    monkeypatch.setattr(package_windows, "_exact_git_bytes", lambda *_args: data)
    monkeypatch.setattr(package_windows.tempfile, "mkstemp", tracking_mkstemp)
    monkeypatch.setattr(package_windows.os, "fdopen", fail_fdopen)

    destination = tmp_path / "payload.bin"
    with pytest.raises(OSError, match="fdopen boom"):
        package_windows._write_exact_git_blob(tmp_path, object_sha, destination)

    fd = captured["fd"]
    assert isinstance(fd, int)
    with pytest.raises(OSError):
        real_fstat(fd)

    temporary_name = captured["name"]
    assert isinstance(temporary_name, str)
    assert not Path(temporary_name).exists()
    assert not destination.exists()
