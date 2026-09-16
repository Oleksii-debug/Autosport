from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from autosport.release_package import build_windows_package


class ReleasePackageChecksumBytesTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40

    def test_checksum_writer_avoids_platform_text_newline_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exe = root / "Autosport.exe"
            start = root / "WINDOWS_START_HERE.txt"
            example = root / "example"
            diagnostic = root / "diagnostic.json"
            accessibility = root / "accessibility.json"
            keyboard = root / "keyboard.json"
            restart = root / "restart.json"
            package = root / "candidate.zip"

            exe.write_bytes(b"autosport-exe")
            start.write_bytes(b"start\n")
            example.mkdir()
            (example / "market.jsonl").write_bytes(b"{}\n")
            for evidence in (diagnostic, accessibility, keyboard, restart):
                evidence.write_bytes(b"{}\n")

            with mock.patch.object(
                Path,
                "write_text",
                side_effect=AssertionError(
                    "canonical checksum serialization must not use platform text mode"
                ),
            ):
                build_windows_package(
                    exe,
                    start,
                    example,
                    diagnostic,
                    accessibility,
                    keyboard,
                    restart,
                    package,
                    self.SOURCE_SHA,
                )

            with zipfile.ZipFile(package, "r") as archive:
                checksum_bytes = archive.read("Autosport-V1/SHA256SUMS.txt")

            self.assertTrue(checksum_bytes.endswith(b"\n"))
            self.assertNotIn(b"\r", checksum_bytes)
            self.assertEqual(
                checksum_bytes,
                b"\n".join(checksum_bytes.splitlines()) + b"\n",
            )


if __name__ == "__main__":
    unittest.main()
