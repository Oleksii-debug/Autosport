from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.evidence_export as evidence_export


def _stat_without_path_identity(result: os.stat_result) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=result.st_mode,
        st_size=result.st_size,
        st_mtime_ns=result.st_mtime_ns,
        st_ctime_ns=result.st_ctime_ns,
        st_ino=0,
        st_dev=0,
    )


def test_path_binding_does_not_require_path_stat_identity_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper_book.json"
    source.write_bytes(b'{"balance":"10000"}\n')

    real_stat = os.stat
    expected = _stat_without_path_identity(real_stat(source, follow_symlinks=False))

    def stat_with_incomplete_path_identity(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if (
            not isinstance(path, int)
            and Path(path) == source
            and kwargs.get("follow_symlinks", True) is False
        ):
            return _stat_without_path_identity(result)
        return result

    descriptor = os.open(source, evidence_export._read_only_open_flags())
    try:
        monkeypatch.setattr(evidence_export.os, "stat", stat_with_incomplete_path_identity)

        assert evidence_export._path_still_matches_open_file(
            source,
            descriptor,
            expected,
        ) is True
    finally:
        os.close(descriptor)


def test_path_binding_rejects_different_open_file_with_equal_path_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper_book.json"
    replacement = tmp_path / "replacement.json"
    source.write_bytes(b"same-size-old")
    replacement.write_bytes(b"same-size-new")

    expected = os.stat(source, follow_symlinks=False)
    real_open = os.open
    descriptor = real_open(source, evidence_export._read_only_open_flags())

    def redirected_verification_open(path, flags, *args, **kwargs):
        if not isinstance(path, int) and Path(path) == source:
            return real_open(replacement, flags, *args, **kwargs)
        return real_open(path, flags, *args, **kwargs)

    try:
        monkeypatch.setattr(evidence_export.os, "open", redirected_verification_open)

        assert evidence_export._path_still_matches_open_file(
            source,
            descriptor,
            expected,
        ) is False
    finally:
        os.close(descriptor)
