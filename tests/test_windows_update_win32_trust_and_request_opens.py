"""The installed trust root and operator request use the same no-follow leaf opener.

Portable injection tests must reject *before* a Windows CRT path-following open.
Native NTFS junction/symlink behavior remains a separate Windows CI gate.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from autosport import windows_update_cli as cli
from autosport import windows_update_installed_trust as trust
from autosport import windows_update_snapshot as snapshot
from autosport.windows_update_admission import WindowsUpdateDecision
from autosport.windows_update_signature import VerifiedWindowsUpdate


def _manifest():
    return (json.dumps({
        "format": "autosport-installed-update-authority-v1",
        "product": "Autosport", "version": "1.1.0", "schema": 1,
        "package_sha256": "a" * 64,
        "trusted_ed25519_public_key_hex": "01" * 32,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _request():
    return {
        "package_path": "пакет.zip",
        "expected_package_sha256": "a" * 64,
        "installed_manifest_path": "встановлений стан.json",
        "expected_installed_manifest_sha256": "b" * 64,
        "signature_hex": "c" * 128,
        "candidate_version": "1.2.3", "candidate_schema": 2,
        "expected_source_sha": "d" * 40,
        "supported_schema_migrations": [[1, 2]],
    }


def _accepted():
    return VerifiedWindowsUpdate(
        decision=WindowsUpdateDecision(
            "ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.1.0", "1.2.3", 1, 2
        ),
        package_sha256="a" * 64, source_sha="d" * 40,
        version="1.2.3", schema=2,
    )


@pytest.mark.parametrize("reject_open_number", [1, 2])
def test_installed_manifest_each_open_requires_no_follow(tmp_path, monkeypatch, reject_open_number):
    path = tmp_path / "встановлений стан.json"
    raw = _manifest()
    path.write_bytes(raw)
    original = snapshot._open_archive_fd
    seen = []

    def reject_at_boundary(candidate):
        seen.append(candidate)
        if len(seen) == reject_open_number:
            raise snapshot.WindowsUpdateSnapshotError("native reparse handle rejected")
        return original(candidate)

    monkeypatch.setattr(snapshot, "_open_archive_fd", reject_at_boundary)
    with pytest.raises(trust.InstalledWindowsTrustError):
        trust.load_pinned_installed_update_authority(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
        )
    assert seen == [path] * reject_open_number
    assert path.read_bytes() == raw


@pytest.mark.parametrize("reject_open_number", [1, 2, 3, 4])
def test_operator_request_all_open_boundaries_use_no_follow(
    tmp_path, monkeypatch, reject_open_number
):
    path = tmp_path / "запит з пробілами.json"
    raw = (json.dumps(_request(), ensure_ascii=False) + "\n").encode("utf-8")
    path.write_bytes(raw)
    original = snapshot._open_archive_fd
    seen = []

    def reject_at_boundary(candidate):
        seen.append(candidate)
        if len(seen) == reject_open_number:
            raise snapshot.WindowsUpdateSnapshotError("native reparse handle rejected")
        return original(candidate)

    monkeypatch.setattr(snapshot, "_open_archive_fd", reject_at_boundary)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: _accepted())
    code, report = cli.run(path)
    assert code == 2
    assert report == {
        "status": "DIAGNOSTIC_REJECTED", "install_authorized": False,
        "independent_trust_root_verified": False,
        "human_tested": False, "nvda_verified": False,
    }
    assert seen == [path] * reject_open_number
    assert path.read_bytes() == raw


def test_installed_manifest_unicode_success_after_native_dispatch(tmp_path, monkeypatch):
    path = tmp_path / "стан з пробілами.json"
    raw = _manifest()
    path.write_bytes(raw)
    original = snapshot._open_archive_fd
    seen = []

    def counted(candidate):
        seen.append(candidate)
        return original(candidate)

    monkeypatch.setattr(snapshot, "_open_archive_fd", counted)
    result = trust.load_pinned_installed_update_authority(
        path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
    )
    assert result.installed.version == "1.1.0"
    assert result.install_authorized is False
    assert seen == [path, path]


def test_operator_request_unicode_success_after_native_dispatch(tmp_path, monkeypatch):
    path = tmp_path / "запит з пробілами.json"
    path.write_text(json.dumps(_request(), ensure_ascii=False), encoding="utf-8")
    original = snapshot._open_archive_fd
    seen = []

    def counted(candidate):
        seen.append(candidate)
        return original(candidate)

    monkeypatch.setattr(snapshot, "_open_archive_fd", counted)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: _accepted())
    code, report = cli.run(path)
    assert code == 0
    assert report["status"] == "DIAGNOSTIC_PASS_UNTRUSTED_ROOT"
    assert report["install_authorized"] is False
    assert seen == [path] * 4
