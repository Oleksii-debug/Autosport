"""Installed update authority must bind path and descriptor stat views separately."""
from __future__ import annotations
import hashlib
import json
import os
import pytest
from autosport import windows_update_installed_trust as trust

class _DescriptorStatView:
    def __init__(self, original):
        self.original = original
    def __getattr__(self, name):
        if name == "st_ctime_ns":
            return self.original.st_ctime_ns + 1_000_000_000
        return getattr(self.original, name)

def _shift_fd_view(monkeypatch):
    original = os.fstat
    monkeypatch.setattr(trust.os, "fstat", lambda fd: _DescriptorStatView(original(fd)))

def _manifest():
    data = {"format": "autosport-installed-update-authority-v1",
            "product": "Autosport", "version": "1.2.3", "schema": 2,
            "package_sha256": "a" * 64, "trusted_ed25519_public_key_hex": "01" * 32}
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")

def test_consistent_fd_ctime_view_can_differ_from_path_view(tmp_path, monkeypatch):
    file = tmp_path / "installed.json"
    raw = _manifest()
    file.write_bytes(raw)
    _shift_fd_view(monkeypatch)
    result = trust.load_pinned_installed_update_authority(
        file, expected_manifest_sha256=hashlib.sha256(raw).hexdigest())
    assert result.installed.version == "1.2.3"
    assert result.install_authorized is False

def test_fd_ctime_view_cannot_hide_same_byte_path_swap(tmp_path, monkeypatch):
    file = tmp_path / "installed.json"
    raw = _manifest()
    file.write_bytes(raw)
    alternate = tmp_path / "other.json"
    alternate.write_bytes(raw)
    _shift_fd_view(monkeypatch)
    original = trust._snapshot._open_archive_fd
    did_swap = False
    def substituted(path):
        nonlocal did_swap
        if not did_swap:
            did_swap = True
            os.replace(alternate, file)
        return original(path)
    monkeypatch.setattr(trust._snapshot, "_open_archive_fd", substituted)
    with pytest.raises(trust.InstalledWindowsTrustError):
        trust.load_pinned_installed_update_authority(
            file, expected_manifest_sha256=hashlib.sha256(raw).hexdigest())
    assert did_swap
