from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import verify_source_checkout


class WindowsBuildSourceSymlinkTests(unittest.TestCase):
    @staticmethod
    def _run_git(root: Path, *args: str, input_bytes: bytes | None = None) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            input=input_bytes,
        )
        return completed.stdout.decode("utf-8").strip()

    @classmethod
    def _repo_with_tracked_symlink_mode(cls, root: Path) -> str:
        cls._run_git(root, "init")
        cls._run_git(root, "config", "user.email", "autosport-tests@example.invalid")
        cls._run_git(root, "config", "user.name", "Autosport Tests")

        external_target = root.parent / "external-build-input.py"
        external_target.write_bytes(b"EXTERNAL = True\n")
        link_payload = os.fsencode(str(external_target))
        tracked_path = root / "tracked-build-input.py"
        tracked_path.write_bytes(link_payload)

        blob_sha = cls._run_git(root, "hash-object", "-w", "--stdin", input_bytes=link_payload)
        cls._run_git(root, "update-index", "--add", "--cacheinfo", "120000", blob_sha, tracked_path.name)
        cls._run_git(root, "commit", "-m", "tracked symlink mode fixture")
        return cls._run_git(root, "rev-parse", "HEAD")

    @staticmethod
    def _without_github_event_environment():
        return patch.dict(
            os.environ,
            {"GITHUB_EVENT_PATH": "", "GITHUB_REPOSITORY": "", "GITHUB_SHA": ""},
            clear=False,
        )

    def test_pristine_verifier_rejects_tracked_symlink_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._repo_with_tracked_symlink_mode(root)

            with self._without_github_event_environment():
                with self.assertRaisesRegex(ValueError, "unsupported tracked mode.*120000"):
                    verify_source_checkout.verify_source_checkout(source_sha, repo_root=root)

    def test_late_boundary_rejects_tracked_symlink_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_sha = self._repo_with_tracked_symlink_mode(root)

            with self._without_github_event_environment():
                with self.assertRaisesRegex(ValueError, "unsupported tracked mode.*120000"):
                    verify_source_checkout.verify_source_checkout(
                        source_sha,
                        repo_root=root,
                        late_build_boundary=True,
                    )


if __name__ == "__main__":
    unittest.main()
