from __future__ import annotations

import json
import os
from pathlib import Path
import zipfile

import pytest

import autosport.data_tool_package as data_tool_package
import autosport.release_package as release_package
from scripts import package_windows


def _assert_closed(fd: int) -> None:
    with pytest.raises(OSError):
        os.fstat(fd)


def test_release_package_closes_raw_temp_descriptor_when_fdopen_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_mkstemp = release_package.tempfile.mkstemp
    captured: dict[str, object] = {}

    def recording_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        captured["fd"] = fd
        captured["path"] = Path(name)
        return fd, name

    def fail_fdopen(fd: int, *args: object, **kwargs: object) -> object:
        assert fd == captured["fd"]
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(release_package.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(release_package.os, "fdopen", fail_fdopen)

    with pytest.raises(OSError, match="injected fdopen failure"):
        release_package._write_canonical_zip(
            tmp_path / "Autosport.zip",
            {"Autosport-V1/probe.txt": b"probe"},
        )

    fd = captured["fd"]
    path = captured["path"]
    assert isinstance(fd, int)
    assert isinstance(path, Path)
    _assert_closed(fd)
    assert not path.exists()


def test_data_tool_verifier_closes_raw_temp_descriptor_when_fdopen_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "base.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "Autosport-V1/BUILD_INFO.json",
            json.dumps({"source_sha": "0" * 40}),
        )
        archive.writestr("Autosport-V1/PACKAGE_MANIFEST.json", "{}")
        archive.writestr("Autosport-V1/SHA256SUMS.txt", "")
        archive.writestr("Autosport-V1/Autosport.exe", b"MZ")

    real_mkstemp = data_tool_package.tempfile.mkstemp
    captured: dict[str, object] = {}

    def recording_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        captured["fd"] = fd
        captured["path"] = Path(name)
        return fd, name

    def fail_fdopen(fd: int, *args: object, **kwargs: object) -> object:
        assert fd == captured["fd"]
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(data_tool_package.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(data_tool_package.os, "fdopen", fail_fdopen)

    with pytest.raises(OSError, match="injected fdopen failure"):
        data_tool_package._verified_base_members(package)

    fd = captured["fd"]
    path = captured["path"]
    assert isinstance(fd, int)
    assert isinstance(path, Path)
    _assert_closed(fd)
    assert not path.exists()


def test_windows_package_blob_writer_closes_raw_temp_descriptor_when_fdopen_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "payload.bin"
    real_mkstemp = package_windows.tempfile.mkstemp
    captured: dict[str, object] = {}

    def recording_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        captured["fd"] = fd
        captured["path"] = Path(name)
        return fd, name

    def fail_fdopen(fd: int, *args: object, **kwargs: object) -> object:
        assert fd == captured["fd"]
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(package_windows, "_exact_git_bytes", lambda *args: b"payload")
    monkeypatch.setattr(package_windows.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(package_windows.os, "fdopen", fail_fdopen)

    with pytest.raises(OSError, match="injected fdopen failure"):
        package_windows._write_exact_git_blob(
            tmp_path,
            "47d05ff6403c8e6c3cf635ea6eb9263738432773",
            destination,
        )

    fd = captured["fd"]
    path = captured["path"]
    assert isinstance(fd, int)
    assert isinstance(path, Path)
    _assert_closed(fd)
    assert not path.exists()
    assert not destination.exists()
