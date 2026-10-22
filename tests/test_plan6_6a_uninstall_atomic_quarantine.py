"""Plan 6 §6А: competing quarantine cannot be overwritten on uninstall.

A real release/receipt installation is independently pinned. Both a racing
empty quarantine and a modified foreign-file quarantine are never replaced.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import tempfile
import zipfile
from unittest.mock import patch

import pytest

from autosport import offline_release_install as install
from autosport import offline_release_lifecycle as lifecycle


SOURCE_SHA = "a" * 40


def _installed(root: Path) -> tuple[Path, str]:
    archive = root / "original.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("Autosport-V1/Autosport.exe", b"test-executable")
        package.writestr("Autosport-V1/BUILD_INFO.json", b"{}")
        package.writestr("Autosport-V1/examples/demo.json", b"{}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    receipt = {"status": "PASS", "source_sha": SOURCE_SHA, "package_sha256": digest}
    target = root / "installed"
    with patch.object(install.release_package, "verify_windows_package", return_value=receipt):
        install.install_verified_release(
            archive, target, expected_source_sha=SOURCE_SHA,
            expected_package_sha256=digest,
        )
    return target, digest


def _trusted(digest: str):
    return patch.object(
        lifecycle.release_package, "verify_windows_package",
        return_value={"status": "PASS", "source_sha": SOURCE_SHA,
                      "package_sha256": digest},
    )


def test_uninstall_refuses_competing_quarantine_without_moving_product():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        destination, digest = _installed(root)
        normal_publish = lifecycle._publish_directory_no_replace
        created = []

        def create_quarantine_race(source, target):
            if Path(target).name == "verified-product":
                Path(target).mkdir()
                owner_file = Path(target) / "unrelated-owner-file"
                owner_file.write_bytes(b"never replace")
                created.append(owner_file)
            return normal_publish(source, target)

        with _trusted(digest):
            with patch.object(
                lifecycle, "_publish_directory_no_replace",
                side_effect=create_quarantine_race,
            ):
                with pytest.raises(install.OfflineInstallError, match="destination appeared"):
                    lifecycle.uninstall_verified_release(
                        destination, archive=root / "original.zip",
                        expected_source_sha=SOURCE_SHA,
                        expected_package_sha256=digest,
                    )

        assert len(created) == 1
        assert created[0].read_bytes() == b"never replace"
        assert (destination / "Autosport.exe").is_file()
        with _trusted(digest):
            proof = lifecycle.inspect_installed_release(
                destination, archive=root / "original.zip",
                expected_source_sha=SOURCE_SHA,
                expected_package_sha256=digest,
            )
        assert proof["status"] == "VERIFIED"



def test_windows_quarantine_collision_is_domain_error_without_mutation():
    """WinError 183 must fail closed like Linux EEXIST, preserving both trees."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "release"
        destination = root / "competing-quarantine"
        source.mkdir()
        destination.mkdir()
        (source / "Autosport.exe").write_bytes(b"intact product")
        (destination / "owner-file").write_bytes(b"intact private data")
        with patch.object(install.os, "name", "nt"), patch.object(
            install.os, "rename", side_effect=FileExistsError(17, "target already exists"),
        ) as rename:
            with pytest.raises(install.OfflineInstallError, match="destination appeared"):
                install._publish_directory_no_replace(source, destination)
            rename.assert_called_once_with(source, destination)
        assert (source / "Autosport.exe").read_bytes() == b"intact product"
        assert (destination / "owner-file").read_bytes() == b"intact private data"

def test_uninstall_uses_canonical_no_replace_primitive():
    source = Path(__file__).resolve().parents[1] / "src/autosport/offline_release_lifecycle.py"
    body = source.read_text(encoding="utf-8")
    uninstall = body.split("def uninstall_verified_release(", 1)[1]
    assert "_publish_directory_no_replace(root, moved)" in uninstall
    assert "os.rename(root, moved)" not in uninstall
