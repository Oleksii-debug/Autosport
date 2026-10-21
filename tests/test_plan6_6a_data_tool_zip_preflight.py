"""Plan 6 §6А: reject ZIP resource abuse before portable Data-tool extraction."""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

import autosport.data_tool_package as subject


def _base_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Autosport-V1/README.txt", b"expected-bytes")
    return path


def test_portable_member_reader_preserves_small_canonical_input(tmp_path: Path) -> None:
    path = _base_zip(tmp_path / "small.zip")
    assert subject._read_members(path) == {"README.txt": b"expected-bytes"}


def test_zip_bomb_central_size_rejected_before_member_read(tmp_path: Path, monkeypatch) -> None:
    path = _base_zip(tmp_path / "oversize.zip")
    # Real ZIP central directory advertises >512 MiB for a tiny member.
    data = bytearray(path.read_bytes())
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, central + 24, 512 * 1024 * 1024 + 1)
    path.write_bytes(data)

    def forbidden_read(self, name, *args, **kwargs):
        raise AssertionError("a ZIP member was read before resource preflight")

    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden_read)
    with pytest.raises(ValueError, match="release ZIP resource limit: unsafe member size"):
        subject._read_members(path)


def test_oversized_zip_wire_is_rejected_before_snapshot(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "oversize-wire.zip"
    path.write_bytes(b"x" * 257)
    monkeypatch.setattr(subject, "_ZIP_MAX_WIRE_BYTES", 256)
    with pytest.raises(ValueError, match="release ZIP resource limit: excessive wire size"):
        subject._verified_base_members(path)


def test_wire_limit_blocks_growth_after_stat(tmp_path: Path, monkeypatch) -> None:
    """A file growing after accepted stat remains bounded on read."""
    path = tmp_path / "growing.zip"
    path.write_bytes(b"x" * 128)
    monkeypatch.setattr(subject, "_ZIP_MAX_WIRE_BYTES", 256)
    original_open = Path.open

    class GrowingStream:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def read(self, limit=-1):
            assert limit == 257
            return b"x" * 257

    def opening(self, *args, **kwargs):
        if self == path:
            return GrowingStream()
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opening)
    with pytest.raises(ValueError, match="release ZIP resource limit: excessive wire size"):
        subject._verified_base_members(path)
