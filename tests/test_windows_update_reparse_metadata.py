"""Windows reparse-point attribute falsifiers for update trust boundaries.

POSIX filesystems cannot create NTFS reparse points. Inject the actual
Windows FILE_ATTRIBUTE_REPARSE_POINT metadata bit into an otherwise ordinary
file's lstat witness to exercise fail-closed admission deterministically.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autosport.windows_update_snapshot import (
    WindowsUpdateSnapshotError, pinned_windows_update_package,
)
from autosport.windows_update_installed_trust import (
    InstalledWindowsTrustError, load_pinned_installed_update_authority,
)
from autosport.windows_update_cli import run


class _ReparseStat:
    st_file_attributes = 0x400  # Windows FILE_ATTRIBUTE_REPARSE_POINT

    def __init__(self, original):
        self._original = original

    def __getattr__(self, name):
        return getattr(self._original, name)


def _mark_reparse(monkeypatch, selected: Path) -> None:
    original_lstat = Path.lstat
    target = selected.absolute()

    def reparse_lstat(self, *args, **kwargs):
        original = original_lstat(self, *args, **kwargs)
        if self.absolute() == target:
            return _ReparseStat(original)
        return original

    monkeypatch.setattr(Path, "lstat", reparse_lstat)


def test_regular_looking_reparse_package_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "candidate.zip"
    raw = b"not a ZIP, but identity must be rejected before parsing"
    path.write_bytes(raw)
    _mark_reparse(monkeypatch, path)
    with pytest.raises(WindowsUpdateSnapshotError):
        with pinned_windows_update_package(path, expected_sha256=hashlib.sha256(raw).hexdigest()):
            pytest.fail("reparse package reached the verifier")


def test_regular_looking_reparse_installed_manifest_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "installed.json"
    raw = (json.dumps({
        "format": "autosport-installed-update-authority-v1",
        "product": "Autosport", "version": "1.2.3", "schema": 2,
        "package_sha256": "a" * 64,
        "trusted_ed25519_public_key_hex": "01" * 32,
    }, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    path.write_bytes(raw)
    _mark_reparse(monkeypatch, path)
    with pytest.raises(InstalledWindowsTrustError):
        load_pinned_installed_update_authority(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
        )


def test_regular_looking_reparse_diagnostic_request_is_rejected(tmp_path, monkeypatch):
    from autosport import windows_update_cli as cli
    path = tmp_path / "request.json"
    request = {
        "package_path": "candidate.zip", "expected_package_sha256": "a" * 64,
        "installed_manifest_path": "installed.json",
        "expected_installed_manifest_sha256": "b" * 64,
        "signature_hex": "c" * 128, "candidate_version": "1.2.3",
        "candidate_schema": 2, "expected_source_sha": "d" * 40,
        "supported_schema_migrations": [[1, 2]],
    }
    path.write_text(json.dumps(request) + "\n", encoding="utf-8")
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **kwargs: pytest.fail("reparse request reached verifier"))
    _mark_reparse(monkeypatch, path)
    status, report = run(path)
    assert status == 2
    assert report["status"] == "DIAGNOSTIC_REJECTED"
    assert report["install_authorized"] is False


def test_private_snapshot_reparse_witness_is_rejected_at_recheck(tmp_path, monkeypatch):
    path = tmp_path / "candidate.zip"
    raw = b"small stable candidate"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(WindowsUpdateSnapshotError):
        with pinned_windows_update_package(path, expected_sha256=digest) as snapshot:
            _mark_reparse(monkeypatch, snapshot.path)
            snapshot.recheck()


def test_installed_manifest_reparse_after_parse_is_rejected(tmp_path, monkeypatch):
    from autosport import windows_update_installed_trust as module
    path = tmp_path / "installed.json"
    raw = (json.dumps({
        "format": "autosport-installed-update-authority-v1",
        "product": "Autosport", "version": "1.2.3", "schema": 2,
        "package_sha256": "a" * 64,
        "trusted_ed25519_public_key_hex": "01" * 32,
    }, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    path.write_bytes(raw)
    original_loads = module.json.loads
    original_lstat = Path.lstat
    switched = False

    def switch_after_parse(*args, **kwargs):
        nonlocal switched
        result = original_loads(*args, **kwargs)
        switched = True
        return result

    def reparse_after_parse(self, *args, **kwargs):
        info = original_lstat(self, *args, **kwargs)
        return _ReparseStat(info) if switched and self.absolute() == path else info

    monkeypatch.setattr(module.json, "loads", switch_after_parse)
    monkeypatch.setattr(Path, "lstat", reparse_after_parse)
    with pytest.raises(InstalledWindowsTrustError):
        load_pinned_installed_update_authority(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
        )
    assert switched
