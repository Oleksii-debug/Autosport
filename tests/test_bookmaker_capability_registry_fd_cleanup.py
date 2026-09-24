from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.bookmaker_capability_registry as registry_module
from autosport.bookmaker_capability_registry import BookmakerCapabilityRegistry


def test_registry_closes_raw_mkstemp_descriptor_when_fdopen_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "registry.json"
    registry = BookmakerCapabilityRegistry(path)
    created: dict[str, object] = {}
    real_mkstemp = registry_module.tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, temp_name = real_mkstemp(*args, **kwargs)
        created["fd"] = fd
        created["temp_name"] = temp_name
        return fd, temp_name

    def fail_fdopen(*_args, **_kwargs):
        raise OSError(errno.EMFILE, "synthetic fdopen failure")

    os_proxy = SimpleNamespace(
        close=os.close,
        fdopen=fail_fdopen,
        fsync=os.fsync,
        replace=os.replace,
    )
    monkeypatch.setattr(registry_module.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(registry_module, "os", os_proxy)

    with pytest.raises(OSError, match="synthetic fdopen failure"):
        registry._write_document([], [])

    fd = created["fd"]
    assert type(fd) is int
    with pytest.raises(OSError) as closed:
        os.fstat(fd)
    assert closed.value.errno == errno.EBADF

    temp_name = created["temp_name"]
    assert type(temp_name) is str
    assert not Path(temp_name).exists()
    assert not path.exists()
