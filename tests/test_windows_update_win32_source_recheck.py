"""Regression: all update trust-boundary opens use the native no-follow leaf opener.

The rejection-injection tests are portable and must fail against the pre-fix
Windows CRT open paths. Real NTFS reparse semantics remain a Windows CI gate.
"""
from __future__ import annotations

import hashlib

import pytest

from autosport import windows_update_snapshot as snapshot


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_source_pin_uses_no_follow_opener_before_read(tmp_path, monkeypatch):
    source = tmp_path / "джерело з пробілами.zip"
    source.write_bytes(b"valid source bytes")
    seen = []

    def reject(path):
        seen.append(path)
        raise snapshot.WindowsUpdateSnapshotError("native opener rejected source")

    monkeypatch.setattr(snapshot, "_open_archive_fd", reject)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="native opener rejected source"):
        with snapshot.pinned_windows_update_package(source, expected_sha256=_digest(source.read_bytes())):
            pytest.fail("source opened without no-follow validation")
    assert seen == [source]


def test_private_snapshot_recheck_uses_no_follow_opener(tmp_path, monkeypatch):
    source = tmp_path / "source.zip"
    source.write_bytes(b"trusted pinned bytes")
    with snapshot.pinned_windows_update_package(source, expected_sha256=_digest(source.read_bytes())) as pinned:
        seen = []

        def reject(path):
            seen.append(path)
            raise snapshot.WindowsUpdateSnapshotError("native opener rejected snapshot")

        with monkeypatch.context() as patch:
            patch.setattr(snapshot, "_open_archive_fd", reject)
            with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="native opener rejected snapshot"):
                pinned.recheck()
        assert seen == [pinned.path]
        pinned.recheck()


def test_source_pin_and_recheck_success_with_unicode_path(tmp_path):
    source = tmp_path / "оновлення пакет.zip"
    source.write_bytes(b"signed package fixture")
    with snapshot.pinned_windows_update_package(source, expected_sha256=_digest(source.read_bytes())) as pinned:
        pinned.recheck()
        assert pinned.sha256 == _digest(source.read_bytes())


def test_embedded_identity_reopen_uses_no_follow_opener(tmp_path, monkeypatch):
    import io
    import zipfile

    source = tmp_path / "package.zip"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    source.write_bytes(stream.getvalue())
    with snapshot.pinned_windows_update_package(source, expected_sha256=_digest(source.read_bytes())) as pinned:
        original = snapshot._open_archive_fd
        seen = []

        def reject_embedded(path):
            seen.append(path)
            # First call is the mandatory snapshot.recheck; the second is
            # the embedded BUILD_INFO descriptor open.
            if len(seen) == 2:
                raise snapshot.WindowsUpdateSnapshotError("native opener rejected embedded")
            return original(path)

        with monkeypatch.context() as patch:
            patch.setattr(snapshot, "_open_archive_fd", reject_embedded)
            with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="embedded update identity is invalid"):
                snapshot._verified_embedded_update_identity(pinned, expected_source_sha="a" * 40)
        assert seen == [pinned.path, pinned.path]
        pinned.recheck()
