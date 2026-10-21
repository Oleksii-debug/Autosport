"""Plan 6 §6А: concurrent quarantine creation cannot be overwritten on uninstall."""
from __future__ import annotations

from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from autosport import offline_release_lifecycle as lifecycle
from test_offline_release_lifecycle import InstalledReleaseLifecycleTests


def test_uninstall_refuses_competing_quarantine_without_moving_product():
    harness = InstalledReleaseLifecycleTests()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        destination, digest = harness._installed(root)
        normal_publish = lifecycle._publish_directory_no_replace
        created = []

        def create_quarantine_race(source, target):
            if Path(target).name == "verified-product":
                Path(target).mkdir()
                owner_file = Path(target) / "unrelated-owner-file"
                owner_file.write_bytes(b"never replace")
                created.append(owner_file)
            return normal_publish(source, target)

        with patch.object(
            lifecycle, "_publish_directory_no_replace",
            side_effect=create_quarantine_race,
        ):
            with pytest.raises(lifecycle.OfflineInstallError, match="destination appeared"):
                harness._uninstall(destination, digest)

        assert len(created) == 1
        assert created[0].read_bytes() == b"never replace"
        assert (destination / "Autosport.exe").is_file()
        assert harness._verify(destination, digest)["status"] == "VERIFIED"


def test_uninstall_uses_canonical_no_replace_primitive():
    source = Path(__file__).resolve().parents[1] / "src/autosport/offline_release_lifecycle.py"
    body = source.read_text(encoding="utf-8")
    uninstall = body.split("def uninstall_verified_release(", 1)[1]
    assert "_publish_directory_no_replace(root, moved)" in uninstall
    assert "os.rename(root, moved)" not in uninstall
