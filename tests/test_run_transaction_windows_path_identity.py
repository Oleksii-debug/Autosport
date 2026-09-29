from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.run_transaction as run_transaction


def _stat_without_path_identity(result: os.stat_result) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=result.st_mode,
        st_nlink=result.st_nlink,
        st_size=result.st_size,
        st_mtime_ns=result.st_mtime_ns,
        st_ctime_ns=result.st_ctime_ns,
        st_ino=0,
        st_dev=0,
    )


def test_canonical_snapshot_does_not_require_path_stat_identity_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows pathname stat may omit identity fields that fstat() can supply.

    Path metadata still has to remain stable and the open handle still has to refer
    to the same file, but an unchanged canonical file must not be rejected solely
    because path-stat and handle-stat come from different Windows identity domains.
    """

    source = tmp_path / "paper_book.json"
    payload = b'{"balance":"10000"}\n'
    source.write_bytes(payload)

    real_stat = os.stat

    def stat_with_incomplete_path_identity(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if (
            not isinstance(path, int)
            and Path(path) == source
            and kwargs.get("follow_symlinks", True) is False
        ):
            return _stat_without_path_identity(result)
        return result

    monkeypatch.setattr(run_transaction.os, "stat", stat_with_incomplete_path_identity)

    snapshot = run_transaction.RunTransaction._read_canonical_file_snapshot(
        source,
        "PaperBook",
    )

    assert snapshot.payload == payload


def test_canonical_snapshot_rejects_redirected_verification_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "paper_book.json"
    replacement = tmp_path / "replacement.json"
    source.write_bytes(b"same-size-old")
    replacement.write_bytes(b"same-size-new")

    real_open = Path.open
    source_open_count = 0

    def redirected_open(self: Path, *args, **kwargs):
        nonlocal source_open_count
        if self == source:
            source_open_count += 1
            if source_open_count >= 2:
                return real_open(replacement, *args, **kwargs)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", redirected_open)

    with pytest.raises(
        run_transaction.RunTransactionError,
        match="canonical path must be a stable regular non-symlink file",
    ):
        run_transaction.RunTransaction._read_canonical_file_snapshot(
            source,
            "PaperBook",
        )
