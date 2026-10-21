"""A hostile release cannot exhaust disk before its ZIP directory is inspected."""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosport import release_package


class ReleaseWireBudgetTests(unittest.TestCase):
    SHA = "a" * 40

    def test_oversize_wire_fails_before_zip_parser_or_spooled_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "hostile.zip"
            archive.write_bytes(b"not a zip" * 500)
            with (
                patch.object(release_package, "_ZIP_MAX_WIRE_BYTES", 128),
                patch.object(zipfile, "ZipFile", side_effect=AssertionError("parsed oversized archive")),
            ):
                with self.assertRaisesRegex(ValueError, "wire byte limit"):
                    release_package.verify_windows_package(archive, expected_source_sha=self.SHA)

    def test_wire_limit_is_enforced_after_multiple_stream_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "hostile-large.zip"
            archive.write_bytes(b"x" * (1024 * 1024 + 17))
            with (
                patch.object(release_package, "_ZIP_MAX_WIRE_BYTES", 1024 * 1024 + 16),
                patch.object(zipfile, "ZipFile", side_effect=AssertionError("parsed oversized archive")),
            ):
                with self.assertRaisesRegex(ValueError, "wire byte limit"):
                    release_package.verify_windows_package(archive, expected_source_sha=self.SHA)

    def test_exact_size_reaches_existing_zip_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "small.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("Autosport-V1/example.txt", b"example")
            with patch.object(release_package, "_ZIP_MAX_WIRE_BYTES", archive.stat().st_size):
                # Invalid release identity is rejected by the existing verifier,
                # not by the new raw-wire guard.
                with self.assertRaises(ValueError) as caught:
                    release_package.verify_windows_package(archive, expected_source_sha=self.SHA)
                self.assertNotIn("wire byte limit", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
