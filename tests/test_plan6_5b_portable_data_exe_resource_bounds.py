"""Plan 6 §5Б: bound the packaged Data executable before reading it into memory."""
from __future__ import annotations

from pathlib import Path

import pytest

import autosport.data_tool_package as subject


def test_oversize_data_exe_refused_before_open_or_base_archive_read(
    tmp_path: Path, monkeypatch,
) -> None:
    data_exe = tmp_path / "Autosport-Data.exe"
    data_exe.write_bytes(b"x" * 257)
    monkeypatch.setattr(subject, "_ZIP_MAX_MEMBER_UNCOMPRESSED", 256)

    original_open = Path.open

    def forbidden_executable_open(self, *args, **kwargs):
        if self == data_exe:
            raise AssertionError("oversize executable was opened before size preflight")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbidden_executable_open)
    with pytest.raises(ValueError, match="release ZIP resource limit: unsafe member size"):
        subject.bind_portable_data_tool(tmp_path / "unused-base.zip", data_exe)


def test_data_exe_growth_after_stat_is_bounded_before_base_verification(
    tmp_path: Path, monkeypatch,
) -> None:
    data_exe = tmp_path / "Autosport-Data.exe"
    data_exe.write_bytes(b"x" * 128)
    monkeypatch.setattr(subject, "_ZIP_MAX_MEMBER_UNCOMPRESSED", 256)
    original_open = Path.open

    class GrowingStream:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, limit=-1):
            assert limit == 257
            return b"x" * 257

    def open_with_growth(self, *args, **kwargs):
        if self == data_exe:
            return GrowingStream()
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_with_growth)
    with pytest.raises(ValueError, match="release ZIP resource limit: unsafe member size"):
        subject.bind_portable_data_tool(tmp_path / "unused-base.zip", data_exe)


def test_empty_data_exe_still_refused_before_base_verification(tmp_path: Path) -> None:
    data_exe = tmp_path / "Autosport-Data.exe"
    data_exe.write_bytes(b"")
    with pytest.raises(ValueError, match="portable data tool executable is empty"):
        subject.bind_portable_data_tool(tmp_path / "unused-base.zip", data_exe)
