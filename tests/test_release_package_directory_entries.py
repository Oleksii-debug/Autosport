from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from autosport.release_package import verify_windows_package


class WindowsReleaseArchiveDirectoryTruthTests(unittest.TestCase):
    def test_explicit_directory_entries_fail_closed_before_payload_validation(self) -> None:
        for member in (
            "Autosport-V1/examples/",
            "Autosport-V1/../escape/",
        ):
            with self.subTest(member=member), tempfile.TemporaryDirectory() as temporary:
                package = Path(temporary) / "candidate.zip"
                with zipfile.ZipFile(package, "w") as archive:
                    archive.writestr(member, b"")
                with self.assertRaisesRegex(
                    ValueError,
                    "release package contains unsupported directory entries",
                ):
                    verify_windows_package(package, expected_source_sha="a" * 40)


if __name__ == "__main__":
    unittest.main()
