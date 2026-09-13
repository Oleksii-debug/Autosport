from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from autosport.release_package import _validate_windows_member, verify_windows_package


class WindowsReleaseArchivePathTruthTests(unittest.TestCase):
    def _assert_rejected(self, members: list[str], message: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "candidate.zip"
            with zipfile.ZipFile(package, "w") as archive:
                for index, member in enumerate(members):
                    archive.writestr(member, f"payload-{index}".encode("utf-8"))
            with self.assertRaisesRegex(ValueError, message):
                verify_windows_package(package, expected_source_sha="a" * 40)

    def test_case_insensitive_member_collision_fails_closed(self) -> None:
        self._assert_rejected(
            ["Autosport-V1/Autosport.exe", "Autosport-V1/AUTOSPORT.EXE"],
            "Windows path collision",
        )

    def test_backslash_member_fails_closed(self) -> None:
        # zipfile normalizes arcname backslashes to '/' on Windows before writing,
        # so exercise the verifier's raw member-path contract directly here.
        with self.assertRaisesRegex(ValueError, "Windows backslash member"):
            _validate_windows_member(r"Autosport-V1/examples\tt_demo\market.jsonl")

    def test_noncanonical_member_fails_closed(self) -> None:
        self._assert_rejected(
            ["Autosport-V1/examples//market.jsonl"],
            "non-canonical member path",
        )

    def test_trailing_dot_member_fails_closed(self) -> None:
        self._assert_rejected(
            ["Autosport-V1/examples/market.jsonl."],
            "Windows trailing space or dot",
        )

    def test_reserved_device_member_fails_closed(self) -> None:
        self._assert_rejected(
            ["Autosport-V1/examples/CON.json"],
            "reserved Windows device name",
        )

    def test_windows_invalid_character_member_fails_closed(self) -> None:
        self._assert_rejected(
            ["Autosport-V1/examples/market:stream.jsonl"],
            "Windows-invalid path character",
        )


if __name__ == "__main__":
    unittest.main()
