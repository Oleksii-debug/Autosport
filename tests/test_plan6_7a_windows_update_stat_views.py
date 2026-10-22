"""Windows update package snapshot must not confuse path and fd timestamp views.

A consistent descriptor view may disagree with the path view on Windows;
inode/digest/change-time and same-view drift detection remain mandatory.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from autosport import windows_update_snapshot as snapshot


class _DescriptorStatView:
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        if name == "st_ctime_ns":
            return self.original.st_ctime_ns + 1_000_000_000
        return getattr(self.original, name)


def _shift_descriptor_stat_view(monkeypatch):
    original = os.fstat

    def shifted(fd):
        return _DescriptorStatView(original(fd))

    monkeypatch.setattr(snapshot.os, "fstat", shifted)


def test_pinned_source_accepts_stable_distinct_descriptor_ctime_view(tmp_path, monkeypatch):
    source = tmp_path / "release.zip"
    source.write_bytes(b"offline paper candidate")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    _shift_descriptor_stat_view(monkeypatch)
    with snapshot.pinned_windows_update_package(source, expected_sha256=expected) as pinned:
        assert pinned.sha256 == expected
        pinned.recheck()


def test_distinct_descriptor_ctime_view_does_not_mask_path_replacement(tmp_path, monkeypatch):
    source = tmp_path / "release.zip"
    source.write_bytes(b"offline paper candidate")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    _shift_descriptor_stat_view(monkeypatch)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError):
        with snapshot.pinned_windows_update_package(source, expected_sha256=expected) as pinned:
            replacement = pinned.path.with_name("replacement.zip")
            replacement.write_bytes(source.read_bytes())
            os.replace(replacement, pinned.path)
