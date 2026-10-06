from __future__ import annotations

import unittest
from pathlib import Path

from autosport.release_package import (
    _require_git_commit_sha,
    build_windows_package,
    verify_windows_package,
)


class ReleasePackageSourceIdentityTests(unittest.TestCase):
    def test_canonical_git_commit_sha_is_accepted(self) -> None:
        source_sha = "d3d0e64496732411d46cc84ae6c29778215c73f4"
        self.assertEqual(
            _require_git_commit_sha(source_sha, field="source_sha"),
            source_sha,
        )

    def test_build_rejects_malformed_source_identity_before_filesystem_work(self) -> None:
        for source_sha in ("", "a" * 39, "a" * 41, "A" * 40, "g" * 40, "not-a-git-sha"):
            with self.subTest(source_sha=source_sha):
                with self.assertRaisesRegex(ValueError, "canonical 40-character lowercase hexadecimal"):
                    build_windows_package(
                        "missing.exe",
                        "missing.txt",
                        "missing-example",
                        "missing-diagnostic.json",
                        "missing-accessibility.json",
                        "missing-keyboard.json",
                        "missing-restart.json",
                        "missing.zip",
                        source_sha,
                    )

    def test_verifier_rejects_malformed_expected_head_before_opening_archive(self) -> None:
        for source_sha in ("", "a" * 39, "A" * 40, "z" * 40, "main"):
            with self.subTest(source_sha=source_sha):
                with self.assertRaisesRegex(ValueError, "canonical 40-character lowercase hexadecimal"):
                    verify_windows_package(
                        Path("does-not-exist.zip"),
                        expected_source_sha=source_sha,
                    )


if __name__ == "__main__":
    unittest.main()
