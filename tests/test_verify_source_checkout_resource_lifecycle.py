from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts import verify_source_checkout


def test_materialize_trusted_verifier_snapshot_closes_raw_fd_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data = b"trusted verifier bytes"
    object_sha = verify_source_checkout._git_blob_sha1(data)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    destination_parent = tmp_path / "out"
    destination_parent.mkdir()
    destination = destination_parent / "verify_source_checkout.py"

    captured: dict[str, object] = {}
    real_mkstemp = verify_source_checkout.tempfile.mkstemp
    real_fstat = os.fstat

    def tracking_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        captured["fd"] = fd
        captured["name"] = name
        return fd, name

    def fail_fdopen(*args: object, **kwargs: object) -> object:
        raise OSError("fdopen boom")

    monkeypatch.setattr(
        verify_source_checkout,
        "_source_tree_entries",
        lambda *_args: {
            b"scripts/verify_source_checkout.py": (
                b"100644",
                object_sha.encode("ascii"),
            )
        },
    )
    monkeypatch.setattr(verify_source_checkout, "_git_bytes", lambda *_args: data)
    monkeypatch.setattr(
        verify_source_checkout.tempfile,
        "mkstemp",
        tracking_mkstemp,
    )
    monkeypatch.setattr(verify_source_checkout.os, "fdopen", fail_fdopen)

    with pytest.raises(OSError, match="fdopen boom"):
        verify_source_checkout.materialize_trusted_verifier_snapshot(
            repo_root,
            "a" * 40,
            destination,
        )

    fd = captured["fd"]
    assert isinstance(fd, int)
    with pytest.raises(OSError):
        real_fstat(fd)

    temporary_name = captured["name"]
    assert isinstance(temporary_name, str)
    assert not Path(temporary_name).exists()
    assert not destination.exists()
