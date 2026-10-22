"""Windows path/descriptor file metadata must be independently stable.

An open file handle and its path can expose different but stable ctime
precision. We still reject either path replacement or metadata mutation.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport import release_package


class _StablePathStat:
    def __init__(self, native: os.stat_result, *, offset_ns: int):
        self.native = native
        self.offset_ns = offset_ns

    def __getattr__(self, name: str):
        if name == "st_ctime_ns":
            return self.native.st_ctime_ns + self.offset_ns
        return getattr(self.native, name)


def test_stable_windows_path_stat_view_does_not_invalidate_pinned_copy(tmp_path: Path) -> None:
    source = tmp_path / "source.exe"
    destination = tmp_path / "copied.exe"
    contents = b"portable executable payload for copy provenance"
    source.write_bytes(contents)

    native_lstat = Path.lstat

    def stable_path_lstat(self: Path):
        stat_result = native_lstat(self)
        if self == source:
            return _StablePathStat(stat_result, offset_ns=10_000)
        return stat_result

    with patch.object(Path, "lstat", stable_path_lstat):
        expected = release_package._release_source_fingerprint(source.lstat())
        release_package._copy_release_regular_source(
            source, destination, expected_fingerprint=expected,
        )

    assert destination.read_bytes() == contents


def test_path_metadata_change_during_copy_still_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source.exe"
    destination = tmp_path / "copied.exe"
    source.write_bytes(b"unchanged on-disk bytes")
    native_lstat = Path.lstat
    path_checks = 0

    def drifting_path_lstat(self: Path):
        nonlocal path_checks
        stat_result = native_lstat(self)
        if self == source:
            path_checks += 1
            return _StablePathStat(stat_result, offset_ns=10_000 if path_checks == 1 else 20_000)
        return stat_result

    with patch.object(Path, "lstat", drifting_path_lstat):
        with pytest.raises(ValueError, match="unsafe release source changed during copy"):
            release_package._copy_release_regular_source(source, destination)

    assert path_checks >= 2
