"""Plan 6 §6А: Windows path-stat and opened-handle timestamp views differ.

Only timestamps from the SAME filesystem API may be compared to each other.
Identity, size, NT change-time, and pathname/descriptor rechecks still fail
closed on a swap or mutation. These portable tests simulate that discrepancy.
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile

import pytest

from autosport import windows_update_snapshot as snapshot


_SOURCE_SHA = "a" * 40


class _HandleStatView:
    """Expose a Windows-style fstat ctime different from lstat's ctime."""

    def __init__(self, original, *, delta_ns: int = 3_000_000_000) -> None:
        self._original = original
        self._delta_ns = delta_ns

    def __getattr__(self, name: str):
        value = getattr(self._original, name)
        if name == "st_ctime_ns":
            return value + self._delta_ns
        return value


def _fixture(tmp_path):
    package = tmp_path / "candidate.zip"
    build_info = {
        "product": "Autosport",
        "source_sha": _SOURCE_SHA,
        "version": "1.2.3",
        "update_version": "1.2.3",
        "update_schema": 2,
    }
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            "Autosport-V1/BUILD_INFO.json",
            json.dumps(build_info, sort_keys=True),
        )
    return package, hashlib.sha256(package.read_bytes()).hexdigest()


def _patch_handle_view(monkeypatch, *, delta_ns=lambda: 3_000_000_000):
    native_fstat = os.fstat

    def handle_stat(fd):
        return _HandleStatView(native_fstat(fd), delta_ns=delta_ns())

    monkeypatch.setattr(snapshot.os, "fstat", handle_stat)


def test_bounded_zip_accepts_different_path_and_handle_ctime_views(
    tmp_path, monkeypatch
):
    package, _digest = _fixture(tmp_path)
    with monkeypatch.context() as scope:
        _patch_handle_view(scope)
        snapshot._require_bounded_zip_members(package)


def test_pinned_embedded_identity_accepts_distinct_stable_handle_ctime_view(
    tmp_path, monkeypatch
):
    package, digest = _fixture(tmp_path)
    with snapshot.pinned_windows_update_package(
        package, expected_sha256=digest
    ) as pinned:
        with monkeypatch.context() as scope:
            _patch_handle_view(scope)
            identity = snapshot._verified_embedded_update_identity(
                pinned, expected_source_sha=_SOURCE_SHA
            )
        assert identity == {"update_version": "1.2.3", "update_schema": 2}


def test_pinned_embedded_identity_rejects_handle_mutation_during_read(
    tmp_path, monkeypatch
):
    package, digest = _fixture(tmp_path)
    with snapshot.pinned_windows_update_package(
        package, expected_sha256=digest
    ) as pinned:
        mutation_visible = [False]
        original_zip = zipfile.ZipFile

        class MutatingReadZip(original_zip):
            def read(self, *args, **kwargs):
                data = super().read(*args, **kwargs)
                mutation_visible[0] = True
                return data

        with monkeypatch.context() as scope:
            _patch_handle_view(
                scope,
                delta_ns=lambda: (
                    4_000_000_000 if mutation_visible[0] else 3_000_000_000
                ),
            )
            scope.setattr(snapshot.zipfile, "ZipFile", MutatingReadZip)
            with pytest.raises(
                snapshot.WindowsUpdateSnapshotError,
                # The canonical parser deliberately redacts all internal
                # ZIP/descriptor rejection messages at this trust boundary.
                # mutation_visible below proves the injected read occurred.
                match="^embedded update identity is invalid$",
            ):
                snapshot._verified_embedded_update_identity(
                    pinned, expected_source_sha=_SOURCE_SHA
                )
        assert mutation_visible[0] is True
