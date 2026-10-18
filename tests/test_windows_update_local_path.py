"""Portable, deterministic falsifiers for the offline update path policy."""
from __future__ import annotations

import os

import pytest

from autosport.windows_update_local_path import UnsafeUpdatePath, require_local_update_path


@pytest.mark.parametrize("value", [
    "", "x\x00.json", "\\\\server\\share\\file", "//server/share/file",
    "\\\\?\\C:\\temp\\file", "C:relative.zip", "updates/NUL",
    "updates/CON.txt", "updates/con .txt", "updates/COM¹.txt",
    "updates/LPT9", "updates/a.zip:stream", "updates/b?.zip",
    "updates/trailing.", "updates/trailing ", "updates/control\x1f.zip",
    "updates/../candidate.zip", "../candidate.zip",
    "updates/./candidate.zip", "updates//candidate.zip", "updates\\\\candidate.zip",
])
def test_unsafe_operator_path_is_rejected(value):
    with pytest.raises(UnsafeUpdatePath):
        require_local_update_path(value)


def test_non_string_and_bool_are_rejected():
    for value in (None, False, 123, b"file"):
        with pytest.raises(UnsafeUpdatePath):
            require_local_update_path(value)


def test_local_unicode_and_spaces_are_preserved():
    value = "Оновлення/Новий пакет 2.zip"
    assert require_local_update_path(value) == value


def test_posix_absolute_local_file_is_allowed():
    if os.name != "nt":
        assert require_local_update_path("/tmp/новий пакет.zip") == "/tmp/новий пакет.zip"


def test_foreign_drive_rejected_off_windows():
    if os.name != "nt":
        with pytest.raises(UnsafeUpdatePath):
            require_local_update_path("C:\\Temp\\update.zip")
