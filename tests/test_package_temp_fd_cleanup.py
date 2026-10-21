from __future__ import annotations

import json
import os
from pathlib import Path
import zipfile

import pytest

import autosport.data_tool_package as data_tool_package


def _base_package(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "Autosport-V1/BUILD_INFO.json",
            json.dumps({"source_sha": "0" * 40}),
        )
        archive.writestr("Autosport-V1/PACKAGE_MANIFEST.json", "{}")
        archive.writestr("Autosport-V1/SHA256SUMS.txt", "")
        archive.writestr("Autosport-V1/Autosport.exe", b"MZ")


def _assert_closed(fd: int) -> None:
    with pytest.raises(OSError):
        os.fstat(fd)


def test_data_tool_verifier_closes_raw_temp_descriptor_when_fdopen_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "base.zip"
    _base_package(package)

    real_mkstemp = data_tool_package.tempfile.mkstemp
    captured: dict[str, object] = {}

    def recording_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        captured["fd"] = fd
        captured["path"] = Path(name)
        return fd, name

    def fail_fdopen(fd: int, *args: object, **kwargs: object) -> object:
        assert fd == captured["fd"]
        raise OSError("fdopen-primary")

    monkeypatch.setattr(data_tool_package.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(data_tool_package.os, "fdopen", fail_fdopen)

    with pytest.raises(OSError, match="fdopen-primary"):
        data_tool_package._verified_base_members(package)

    fd = captured["fd"]
    path = captured["path"]
    assert isinstance(fd, int)
    assert isinstance(path, Path)
    _assert_closed(fd)
    assert not path.exists()


def test_data_tool_verifier_cleanup_failures_do_not_shadow_primary_fdopen_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "base.zip"
    _base_package(package)

    real_mkstemp = data_tool_package.tempfile.mkstemp
    real_close = os.close
    real_unlink = Path.unlink
    captured: dict[str, object] = {}

    def recording_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        captured["fd"] = fd
        captured["path"] = Path(name)
        return fd, name

    def fail_fdopen(fd: int, *args: object, **kwargs: object) -> object:
        assert fd == captured["fd"]
        raise OSError("fdopen-primary")

    def fail_close(fd: int) -> None:
        assert fd == captured["fd"]
        raise OSError("close-secondary")

    def fail_unlink(path: Path, *args: object, **kwargs: object) -> None:
        assert path == captured["path"]
        raise PermissionError("unlink-secondary")

    monkeypatch.setattr(data_tool_package.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(data_tool_package.os, "fdopen", fail_fdopen)
    monkeypatch.setattr(data_tool_package.os, "close", fail_close)
    monkeypatch.setattr(Path, "unlink", fail_unlink)

    try:
        with pytest.raises(OSError, match="fdopen-primary") as raised:
            data_tool_package._verified_base_members(package)

        notes = tuple(getattr(raised.value, "__notes__", ()))
        assert any("close-secondary" in note for note in notes)
        assert any("unlink-secondary" in note for note in notes)

        fd = captured["fd"]
        path = captured["path"]
        assert isinstance(fd, int)
        assert isinstance(path, Path)
        assert path.exists()
    finally:
        fd = captured.get("fd")
        path = captured.get("path")
        if isinstance(fd, int):
            try:
                real_close(fd)
            except OSError:
                pass
        if isinstance(path, Path) and path.exists():
            real_unlink(path)
